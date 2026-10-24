//! Transaction history, read from the views zcash_client_sqlite maintains.
//!
//! Everything here is local: `v_transactions` and `v_tx_outputs` already hold what the
//! wallet learned while scanning and what it recorded when it built its own transactions,
//! including the memos it sent. Listing history never contacts the server.
//!
//! Memo contents are confidential: they are exposed as attributes, never in a repr.

use std::collections::HashMap;

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use rusqlite::{Connection, OpenFlags};
use zcash_primitives::transaction::TxId;
use zcash_protocol::memo::{Memo, MemoBytes};

use crate::error::{self, WalletError};

/// One output of a wallet transaction: received, change, or sent to someone else.
#[pyclass(module = "zecpy._native", frozen)]
#[derive(Clone)]
pub struct TxOutput {
    /// "transparent", "sapling", "orchard" or "ironwood".
    #[pyo3(get)]
    pub pool: &'static str,
    #[pyo3(get)]
    pub output_index: u32,
    /// "received", "change" or "sent".
    #[pyo3(get)]
    pub direction: &'static str,
    #[pyo3(get)]
    pub value: u64,
    /// The recipient recorded when the wallet built the output, or the wallet's own
    /// receiving address. None for wallet-internal outputs.
    #[pyo3(get)]
    pub address: Option<String>,
    /// Diversifier index of the wallet address that received it, if any.
    #[pyo3(get)]
    pub diversifier_index: Option<u128>,
    /// The memo, when it is text.
    #[pyo3(get)]
    pub memo: Option<String>,
    /// The raw 512-byte memo, when it is present but not text (ZIP 302 binary memos).
    pub memo_bytes: Option<Vec<u8>>,
}

#[pymethods]
impl TxOutput {
    #[getter]
    fn memo_bytes<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.memo_bytes.as_deref().map(|b| PyBytes::new(py, b))
    }

    fn __repr__(&self) -> String {
        let memo = if self.memo.is_some() || self.memo_bytes.is_some() { "<hidden>" } else { "None" };
        format!(
            "TxOutput(pool={:?}, direction={:?}, value={}, memo={memo})",
            self.pool, self.direction, self.value
        )
    }
}

/// A transaction that touched the wallet.
#[pyclass(module = "zecpy._native", frozen, get_all)]
pub struct Transaction {
    pub txid: String,
    /// "mined", "pending" (broadcast or seen, not yet mined) or "expired" (never mined
    /// and past its expiry height: its inputs are spendable again).
    pub status: &'static str,
    pub mined_height: Option<u32>,
    /// Blocks on top of the mined block plus one, as far as the wallet has scanned.
    pub confirmations: u32,
    /// Block time in Unix seconds, for mined transactions.
    pub block_time: Option<i64>,
    pub expiry_height: Option<u32>,
    /// Change in the wallet's balance, in zatoshis: negative for payments out.
    pub net_value: i64,
    /// The fee, when the wallet knows it (always for transactions it sent).
    pub fee: Option<u64>,
    /// A wallet-internal transfer between shielded pools. The crossing value is public on
    /// chain; this is that value, or None when the transaction is not such a transfer.
    pub pool_crossing_value: Option<u64>,
    pub memo_count: u32,
    /// The wallet received a note here whose memo is not known yet: compact blocks do not
    /// carry memos, and `fetch_memos` has not fetched this transaction.
    pub memo_pending: bool,
    pub outputs: Vec<TxOutput>,
}

#[pymethods]
impl Transaction {
    fn __repr__(&self) -> String {
        format!(
            "Transaction(txid={:?}, status={:?}, mined_height={}, net_value={}, fee={})",
            self.txid,
            self.status,
            py_option(self.mined_height),
            self.net_value,
            py_option(self.fee)
        )
    }
}

/// An optional number as Python prints it.
fn py_option<T: std::fmt::Display>(value: Option<T>) -> String {
    value.map_or_else(|| "None".to_string(), |v| v.to_string())
}

fn pool_name(code: i64) -> Option<&'static str> {
    match code {
        0 => Some("transparent"),
        2 => Some("sapling"),
        3 => Some("orchard"),
        4 => Some("ironwood"),
        _ => None,
    }
}

fn txid_string(bytes: Vec<u8>) -> PyResult<String> {
    let bytes: [u8; 32] = bytes
        .try_into()
        .map_err(|_| WalletError::new_err("malformed txid in wallet database"))?;
    Ok(TxId::from_bytes(bytes).to_string())
}

/// The stored (internal byte order) form of a txid in its usual reversed-hex display form.
fn txid_bytes(hex: &str) -> Option<Vec<u8>> {
    if hex.len() != 64 || !hex.is_ascii() {
        return None;
    }
    let mut bytes = (0..32)
        .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).ok())
        .collect::<Option<Vec<u8>>>()?;
    bytes.reverse();
    Some(bytes)
}

/// Splits a stored memo into text or raw bytes. An empty memo (0xF6) is no memo.
fn decode_memo(raw: Option<Vec<u8>>) -> (Option<String>, Option<Vec<u8>>) {
    let Some(raw) = raw else { return (None, None) };
    match MemoBytes::from_bytes(&raw).ok().map(Memo::try_from) {
        Some(Ok(Memo::Empty)) => (None, None),
        Some(Ok(Memo::Text(text))) => (Some(String::from(text)), None),
        _ => (None, Some(raw)),
    }
}

fn status(mined_height: Option<u32>, expired_unmined: bool) -> &'static str {
    match (mined_height, expired_unmined) {
        (Some(_), _) => "mined",
        (None, true) => "expired",
        (None, false) => "pending",
    }
}

/// Lists the wallet's transactions first seen at or after `since` (or one transaction,
/// when `txid` is given), newest first; unmined transactions come before mined ones.
pub fn list(db_path: &str, since: u32, txid: Option<&str>) -> PyResult<Vec<Transaction>> {
    let txid_filter = txid
        .map(|t| txid_bytes(t).ok_or_else(|| pyo3::exceptions::PyValueError::new_err("txid must be 64 hex characters")))
        .transpose()?;
    let conn = Connection::open_with_flags(db_path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(error::wallet("cannot open wallet database read-only"))?;
    let tip: Option<u32> = conn
        .query_row("SELECT MAX(height) FROM blocks", [], |row| row.get(0))
        .map_err(error::wallet("cannot read scanned height"))?;

    let mut outputs = outputs(&conn, since, txid_filter.as_deref())?;
    let mut stmt = conn
        .prepare(
            "SELECT v.txid, v.mined_height, v.block_time, v.expiry_height, v.account_balance_delta,
                    v.fee_paid, v.pool_crossing_value, v.memo_count, v.expired_unmined,
                    t.raw IS NULL AND EXISTS (
                        SELECT 1 FROM v_received_outputs r
                        WHERE r.transaction_id = t.id_tx AND r.pool IN (2, 3, 4) AND r.memo IS NULL
                    )
             FROM v_transactions v
             JOIN transactions t ON t.txid = v.txid
             WHERE COALESCE(t.mined_height, t.min_observed_height) >= :since
               AND (:txid IS NULL OR v.txid = :txid)
             ORDER BY v.mined_height IS NOT NULL, v.mined_height DESC, t.min_observed_height DESC, v.txid",
        )
        .map_err(error::wallet("cannot query transactions"))?;
    let rows = stmt
        .query_map(rusqlite::named_params! {":since": since, ":txid": txid_filter}, |row| {
            Ok((
                row.get::<_, Vec<u8>>(0)?,
                row.get::<_, Option<u32>>(1)?,
                row.get::<_, Option<i64>>(2)?,
                row.get::<_, Option<u32>>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, Option<i64>>(5)?,
                row.get::<_, Option<i64>>(6)?,
                row.get::<_, u32>(7)?,
                row.get::<_, Option<bool>>(8)?,
                row.get::<_, bool>(9)?,
            ))
        })
        .map_err(error::wallet("cannot query transactions"))?;

    let mut out = Vec::new();
    for row in rows {
        let (raw_txid, mined_height, block_time, expiry_height, net_value, fee, crossing, memo_count, expired, memo_pending) =
            row.map_err(error::wallet("cannot read transaction"))?;
        let txid = txid_string(raw_txid)?;
        let confirmations = match (mined_height, tip) {
            (Some(h), Some(tip)) if tip >= h => tip - h + 1,
            _ => 0,
        };
        let non_negative = |v: Option<i64>| v.map(|v| u64::try_from(v).unwrap_or(0));
        out.push(Transaction {
            outputs: outputs.remove(&txid).unwrap_or_default(),
            txid,
            status: status(mined_height, expired.unwrap_or(false)),
            mined_height,
            confirmations,
            block_time,
            // librustzcash stores 0 for "no expiry".
            expiry_height: expiry_height.filter(|h| *h > 0),
            net_value,
            fee: non_negative(fee),
            pool_crossing_value: non_negative(crossing),
            memo_count,
            memo_pending,
        });
    }
    Ok(out)
}

/// Outputs of the selected transactions, grouped by txid, in pool and index order.
fn outputs(conn: &Connection, since: u32, txid: Option<&[u8]>) -> PyResult<HashMap<String, Vec<TxOutput>>> {
    let mut stmt = conn
        .prepare(
            "SELECT o.txid, o.output_pool, o.output_index, o.to_account_uuid IS NOT NULL, o.is_change,
                    o.value, o.to_address, o.diversifier_index_be, o.memo
             FROM v_tx_outputs o
             JOIN transactions t ON t.id_tx = o.transaction_id
             WHERE COALESCE(t.mined_height, t.min_observed_height) >= :since
               AND (:txid IS NULL OR o.txid = :txid)
             ORDER BY o.output_pool, o.output_index",
        )
        .map_err(error::wallet("cannot query transaction outputs"))?;
    let rows = stmt
        .query_map(rusqlite::named_params! {":since": since, ":txid": txid}, |row| {
            Ok((
                row.get::<_, Vec<u8>>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, u32>(2)?,
                row.get::<_, bool>(3)?,
                row.get::<_, bool>(4)?,
                row.get::<_, i64>(5)?,
                row.get::<_, Option<String>>(6)?,
                row.get::<_, Option<Vec<u8>>>(7)?,
                row.get::<_, Option<Vec<u8>>>(8)?,
            ))
        })
        .map_err(error::wallet("cannot query transaction outputs"))?;

    let mut by_tx: HashMap<String, Vec<TxOutput>> = HashMap::new();
    for row in rows {
        let (raw_txid, pool, output_index, to_wallet, is_change, value, address, diversifier, memo) =
            row.map_err(error::wallet("cannot read transaction output"))?;
        let Some(pool) = pool_name(pool) else { continue };
        let (memo, memo_bytes) = decode_memo(memo);
        let direction = match (to_wallet, is_change) {
            (true, true) => "change",
            (true, false) => "received",
            (false, _) => "sent",
        };
        by_tx.entry(txid_string(raw_txid)?).or_default().push(TxOutput {
            pool,
            output_index,
            direction,
            value: u64::try_from(value).map_err(|_| WalletError::new_err("negative output value"))?,
            address,
            diversifier_index: diversifier.as_deref().and_then(crate::received::diversifier_from_be),
            memo,
            memo_bytes,
        });
    }
    Ok(by_tx)
}

#[cfg(test)]
mod tests {
    use super::{decode_memo, status, txid_bytes, txid_string};

    #[test]
    fn txid_hex_round_trips_through_stored_bytes() {
        let stored: Vec<u8> = (0u8..32).collect();
        let shown = txid_string(stored.clone()).unwrap();
        assert!(shown.starts_with("1f1e"), "display order is reversed: {shown}");
        assert_eq!(txid_bytes(&shown), Some(stored));
        assert_eq!(txid_bytes("zz"), None);
        assert_eq!(txid_bytes(&"g".repeat(64)), None);
    }

    #[test]
    fn memos_decode_to_text_bytes_or_nothing() {
        assert_eq!(decode_memo(None), (None, None));
        let mut empty = vec![0u8; 512];
        empty[0] = 0xF6;
        assert_eq!(decode_memo(Some(empty)), (None, None));
        let mut text = vec![0u8; 512];
        text[..5].copy_from_slice(b"hello");
        assert_eq!(decode_memo(Some(text)), (Some("hello".into()), None));
        let mut binary = vec![0u8; 512];
        binary[0] = 0xFF;
        binary[1] = 7;
        let (t, b) = decode_memo(Some(binary.clone()));
        assert_eq!(t, None);
        assert_eq!(b, Some(binary));
    }

    #[test]
    fn status_prefers_mined_over_expired() {
        assert_eq!(status(Some(10), true), "mined");
        assert_eq!(status(None, true), "expired");
        assert_eq!(status(None, false), "pending");
    }
}
