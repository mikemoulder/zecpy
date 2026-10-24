//! Read-only queries over the wallet database for incoming payments.
//!
//! The Rust API reports received outputs without the address they arrived at, but the
//! database records it: every received note links to an `addresses` row carrying its
//! diversifier index. That index is what ties a payment to an invoice, with no memo and no
//! extra request to the server.

use pyo3::prelude::*;
use rusqlite::{Connection, OpenFlags};
use zcash_primitives::transaction::TxId;

use crate::error;

/// A payment the wallet received (change and self-transfers excluded).
#[pyclass(module = "zecpy._native", frozen, get_all)]
pub struct ReceivedOutput {
    pub txid: String,
    /// "sapling", "orchard" or "ironwood".
    pub pool: &'static str,
    pub output_index: u32,
    pub value: u64,
    /// None while unmined, including after a reorg removed the block that mined it.
    pub mined_height: Option<u32>,
    /// Diversifier index of the receiving address, if the wallet generated it.
    pub diversifier_index: Option<u128>,
}

fn pool_name(code: i64) -> Option<&'static str> {
    match code {
        2 => Some("sapling"),
        3 => Some("orchard"),
        4 => Some("ironwood"),
        _ => None,
    }
}

fn diversifier_from_be(bytes: &[u8]) -> Option<u128> {
    (bytes.len() <= 16).then(|| bytes.iter().fold(0u128, |acc, b| (acc << 8) | u128::from(*b)))
}

/// Lists received shielded outputs whose transaction was observed at or after `since`.
pub fn list(db_path: &str, since: u32) -> PyResult<Vec<ReceivedOutput>> {
    let conn = Connection::open_with_flags(db_path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(error::wallet("cannot open wallet database read-only"))?;
    let mut stmt = conn
        .prepare(
            "SELECT t.txid, r.pool, r.output_index, r.value, t.mined_height, a.diversifier_index_be
             FROM v_received_outputs r
             JOIN transactions t ON t.id_tx = r.transaction_id
             LEFT JOIN addresses a ON a.id = r.address_id
             WHERE r.is_change = 0
               AND r.pool IN (2, 3, 4)
               AND COALESCE(t.mined_height, t.min_observed_height) >= :since
             ORDER BY COALESCE(t.mined_height, t.min_observed_height), t.txid, r.pool, r.output_index",
        )
        .map_err(error::wallet("cannot query received outputs"))?;
    let rows = stmt
        .query_map(rusqlite::named_params! {":since": since}, |row| {
            let txid: Vec<u8> = row.get(0)?;
            let pool: i64 = row.get(1)?;
            let diversifier: Option<Vec<u8>> = row.get(5)?;
            Ok((txid, pool, row.get::<_, u32>(2)?, row.get::<_, i64>(3)?, row.get::<_, Option<u32>>(4)?, diversifier))
        })
        .map_err(error::wallet("cannot query received outputs"))?;

    let mut out = Vec::new();
    for row in rows {
        let (txid, pool, output_index, value, mined_height, diversifier) =
            row.map_err(error::wallet("cannot read received output"))?;
        let txid: [u8; 32] = txid
            .try_into()
            .map_err(|_| error::WalletError::new_err("malformed txid in wallet database"))?;
        out.push(ReceivedOutput {
            txid: TxId::from_bytes(txid).to_string(),
            pool: pool_name(pool).expect("query selects shielded pools only"),
            output_index,
            value: u64::try_from(value).map_err(|_| error::WalletError::new_err("negative note value"))?,
            mined_height,
            diversifier_index: diversifier.as_deref().and_then(diversifier_from_be),
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::diversifier_from_be;

    #[test]
    fn diversifier_index_is_big_endian() {
        assert_eq!(diversifier_from_be(&[0; 11]), Some(0));
        let mut be = [0u8; 11];
        be[10] = 1;
        assert_eq!(diversifier_from_be(&be), Some(1));
        be[5] = 1;
        assert_eq!(diversifier_from_be(&be), Some((1u128 << 40) | 1));
    }
}
