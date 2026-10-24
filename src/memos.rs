//! Fetching received memos without telling the server which transaction is ours.
//!
//! Compact blocks carry only the first 52 bytes of each note ciphertext, so a memo needs
//! the full transaction, fetched by txid with `GetTransaction`. Tor hides who asks, but not
//! what is asked: a server that sees one txid fetched shortly after it was mined learns that
//! some light wallet received in it. ZecPy therefore never fetches a txid alone. Each real
//! fetch is one of `k + 1` fetches of nearby shielded transactions (decoys taken from the
//! compact blocks the wallet already downloaded), on a Tor circuit of its own, in random
//! order. The decoys are a window of consecutive candidates in height order whose offset is
//! chosen so the real transaction's rank inside the window is uniform: the server cannot
//! pick it out as the median or the edge of the set.
//!
//! Only transactions holding a shielded note the wallet received with an unknown memo are
//! fetched. librustzcash also queues the funding transactions of transparent inputs for
//! retrieval; fetching those would tell the server about the sender's coins, so ZecPy
//! leaves them alone.
//!
//! Candidates are recorded during sync, which costs no request, in `memo_decoys.txt` next
//! to the wallet database. It holds public txids plus the txids awaiting a fetch, so it is
//! as sensitive as the wallet database itself.

use std::collections::{BTreeMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};

use pyo3::prelude::*;
use rand::{Rng, RngExt, rand_core::UnwrapErr, rngs::SysRng, seq::SliceRandom};
use rusqlite::{Connection, OpenFlags};
use zcash_client_backend::data_api::{TransactionStatus, WalletWrite, wallet::decrypt_and_store_transaction};
use zcash_client_backend::proto::compact_formats::CompactBlock;
use zcash_client_backend::proto::service::TxFilter;
use zcash_primitives::transaction::{Transaction, TxId};
use zcash_protocol::consensus::{BlockHeight, BranchId};

use crate::error::{self, WalletError};
use crate::net::Client;
use crate::network::Network;
use crate::wallet::Db;

type Txid = [u8; 32];

/// Decoys are taken from at most this many blocks away from the real transaction.
const MAX_DISTANCE: u32 = 100;
/// Candidates kept per pending fetch.
const CANDIDATES_PER_FETCH: usize = 32;
/// Recently seen shielded transactions kept to serve future fetches.
const RECENT_POOL: usize = 512;

/// Per block: its height and the txids of its transactions that create shielded outputs.
pub type BlockTxids = Vec<(u32, Vec<Txid>)>;

/// Decoys must look like a payment someone could have received: a non-coinbase
/// transaction with shielded outputs. A coinbase (always the first transaction) would be
/// ruled out by the server at once.
fn plausible_payment(tx: &zcash_client_backend::proto::compact_formats::CompactTx) -> bool {
    tx.index != 0 && !(tx.outputs.is_empty() && tx.actions.is_empty() && tx.ironwood_actions.is_empty())
}

pub fn shielded_txids(blocks: &[CompactBlock]) -> BlockTxids {
    blocks
        .iter()
        .map(|b| {
            let txids = b
                .vtx
                .iter()
                .filter(|tx| plausible_payment(tx))
                .filter_map(|tx| tx.txid.as_slice().try_into().ok())
                .collect();
            (u32::try_from(b.height).expect("block heights fit in u32"), txids)
        })
        .collect()
}

#[derive(Default, Debug, PartialEq)]
struct Book {
    recent: VecDeque<(u32, Txid)>,
    /// Real txid -> (its height, decoy candidates with their heights).
    pending: BTreeMap<Txid, (u32, Vec<(u32, Txid)>)>,
}

fn book_path(db_path: &str) -> PathBuf {
    Path::new(db_path).with_file_name("memo_decoys.txt")
}

fn hex(t: &Txid) -> String {
    t.iter().map(|b| format!("{b:02x}")).collect()
}

fn unhex(s: &str) -> Option<Txid> {
    if s.len() != 64 || !s.is_ascii() {
        return None;
    }
    (0..32)
        .map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).ok())
        .collect::<Option<Vec<u8>>>()?
        .try_into()
        .ok()
}

fn entry(s: &str) -> Option<(u32, Txid)> {
    let (h, t) = s.split_once(':')?;
    Some((h.parse().ok()?, unhex(t)?))
}

impl Book {
    /// Lines `recent <height>:<txid>` and `pending <txid> <height> <height>:<txid>...`.
    /// A missing or damaged file is an empty book: it only delays fetches.
    fn load(path: &Path) -> Self {
        let mut book = Book::default();
        let Ok(text) = std::fs::read_to_string(path) else { return book };
        for line in text.lines() {
            let mut words = line.split(' ');
            match words.next() {
                Some("recent") => book.recent.extend(words.next().and_then(entry)),
                Some("pending") => {
                    let (Some(real), Some(height)) =
                        (words.next().and_then(unhex), words.next().and_then(|h| h.parse().ok()))
                    else {
                        continue;
                    };
                    book.pending.insert(real, (height, words.filter_map(entry).collect()));
                }
                _ => {}
            }
        }
        book
    }

    fn save(&self, path: &Path) -> PyResult<()> {
        let mut text = String::new();
        for (h, t) in &self.recent {
            text.push_str(&format!("recent {h}:{}\n", hex(t)));
        }
        for (real, (height, cands)) in &self.pending {
            text.push_str(&format!("pending {} {height}", hex(real)));
            for (h, t) in cands {
                text.push_str(&format!(" {h}:{}", hex(t)));
            }
            text.push('\n');
        }
        let tmp = path.with_extension("tmp");
        std::fs::write(&tmp, text).map_err(error::wallet("cannot save memo decoys"))?;
        std::fs::rename(&tmp, path).map_err(error::wallet("cannot save memo decoys"))
    }

    /// Adds a scanned batch: new candidates for every pending fetch, new pending fetches
    /// for `needed`, and the batch's transactions to the recent pool. `own` are the
    /// wallet's transactions, which are never decoys.
    fn add(&mut self, blocks: &BlockTxids, needed: &[(Txid, u32)], own: &HashSet<Txid>) {
        let fresh: Vec<(u32, Txid)> = blocks
            .iter()
            .flat_map(|(h, txids)| txids.iter().map(move |t| (*h, *t)))
            .filter(|(_, t)| !own.contains(t))
            .collect();
        for (real, height) in needed {
            self.pending.entry(*real).or_insert_with(|| (*height, Vec::new()));
        }
        self.pending.retain(|real, _| needed.iter().any(|(t, _)| t == real));
        for (real, (height, cands)) in self.pending.iter_mut() {
            let known: HashSet<Txid> = cands.iter().map(|(_, t)| *t).collect();
            let mut extra: Vec<(u32, Txid)> = self
                .recent
                .iter()
                .chain(fresh.iter())
                .filter(|(h, t)| h.abs_diff(*height) <= MAX_DISTANCE && t != real && !known.contains(t) && !own.contains(t))
                .copied()
                .collect();
            extra.sort_by_key(|(h, t)| (h.abs_diff(*height), *t));
            extra.dedup();
            cands.extend(extra.into_iter().take(CANDIDATES_PER_FETCH.saturating_sub(cands.len())));
        }
        self.recent.extend(fresh);
        while self.recent.len() > RECENT_POOL {
            self.recent.pop_front();
        }
    }
}

/// Picks `k` decoys from `cands` for the real transaction at `height`, or None if there are
/// fewer than `k`. The real transaction's rank in the returned window is uniform over
/// every rank the candidates allow.
fn choose<R: Rng>(real: Txid, height: u32, cands: &[(u32, Txid)], k: usize, rng: &mut R) -> Option<Vec<Txid>> {
    if cands.len() < k {
        return None;
    }
    let mut all: Vec<(u32, Txid)> = cands.to_vec();
    all.push((height, real));
    all.sort();
    all.dedup();
    let i = all.iter().position(|(_, t)| *t == real).expect("pushed above");
    // Offsets r place the real one at rank r of a window [i - r, i - r + k].
    let valid: Vec<usize> = (0..=k).filter(|r| *r <= i && i - r + k < all.len()).collect();
    let r = valid[rng.random_range(0..valid.len())];
    let start = i - r;
    Some(all[start..=start + k].iter().map(|(_, t)| *t).filter(|t| *t != real).collect())
}

/// Received transactions whose memo is unknown and which librustzcash has queued for
/// retrieval, with their mined heights. Transparent-input funding transactions, also in
/// that queue, are excluded: see the module documentation.
fn needed(conn: &Connection) -> PyResult<Vec<(Txid, u32)>> {
    let mut stmt = conn
        .prepare(
            "SELECT t.txid, t.mined_height
             FROM tx_retrieval_queue q
             JOIN transactions t ON t.txid = q.txid
             WHERE q.query_type = 1
               AND t.mined_height IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM v_received_outputs r
                   WHERE r.transaction_id = t.id_tx AND r.pool IN (2, 3, 4) AND r.memo IS NULL
               )",
        )
        .map_err(error::wallet("cannot query pending memos"))?;
    let rows = stmt
        .query_map([], |row| Ok((row.get::<_, Vec<u8>>(0)?, row.get::<_, u32>(1)?)))
        .map_err(error::wallet("cannot query pending memos"))?;
    let mut out = Vec::new();
    for row in rows {
        let (txid, height) = row.map_err(error::wallet("cannot read pending memo"))?;
        out.push((txid.try_into().map_err(|_| WalletError::new_err("malformed txid in wallet database"))?, height));
    }
    Ok(out)
}

fn own_txids(conn: &Connection) -> PyResult<HashSet<Txid>> {
    let mut stmt = conn
        .prepare("SELECT txid FROM transactions")
        .map_err(error::wallet("cannot list wallet transactions"))?;
    let rows = stmt
        .query_map([], |row| row.get::<_, Vec<u8>>(0))
        .map_err(error::wallet("cannot list wallet transactions"))?;
    Ok(rows.filter_map(|r| r.ok()).filter_map(|t| t.try_into().ok()).collect())
}

fn read_only(db_path: &str) -> PyResult<Connection> {
    Connection::open_with_flags(db_path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(error::wallet("cannot open wallet database read-only"))
}

/// Records decoy candidates from a scanned batch. Local only.
pub fn record(db_path: &str, blocks: &BlockTxids) -> PyResult<()> {
    let conn = read_only(db_path)?;
    let path = book_path(db_path);
    let mut book = Book::load(&path);
    let needed = needed(&conn)?;
    if needed.is_empty() && book.pending.is_empty() && blocks.iter().all(|(_, t)| t.is_empty()) {
        return Ok(());
    }
    book.add(blocks, &needed, &own_txids(&conn)?);
    book.save(&path)
}

/// What one `fetch_memos` call did.
#[pyclass(module = "zecpy._native", frozen, get_all)]
pub struct MemoReport {
    /// Transactions fetched and decrypted; their memos are now in the history.
    pub fetched: u32,
    /// Transactions still waiting for enough decoys from blocks not yet scanned.
    pub waiting: u32,
    /// Transactions the server did not know (reorged away or pruned); not retried.
    pub unavailable: u32,
}

#[pymethods]
impl MemoReport {
    fn __repr__(&self) -> String {
        format!("MemoReport(fetched={}, waiting={}, unavailable={})", self.fetched, self.waiting, self.unavailable)
    }
}

/// One real fetch hidden among its decoys: the transactions to request, in order.
pub struct Batch {
    pub real: Txid,
    pub height: u32,
    pub order: Vec<Txid>,
}

/// Plans the fetches that have enough decoys. Local only.
pub fn plan(db_path: &str, k: usize) -> PyResult<(Vec<Batch>, u32)> {
    let conn = read_only(db_path)?;
    let book = Book::load(&book_path(db_path));
    let mut rng = UnwrapErr(SysRng);
    let mut batches = Vec::new();
    let mut waiting = 0;
    for (real, height) in needed(&conn)? {
        let cands = book.pending.get(&real).map(|(_, c)| c.as_slice()).unwrap_or_default();
        match choose(real, height, cands, k, &mut rng) {
            Some(mut order) => {
                order.push(real);
                order.shuffle(&mut rng);
                batches.push(Batch { real, height, order });
            }
            None => waiting += 1,
        }
    }
    Ok((batches, waiting))
}

pub enum Fetched {
    Found(Vec<u8>),
    Unknown,
}

/// Requests every transaction of a batch on one connection and returns the real one.
/// Decoy responses are discarded unread; a decoy the server cannot find is not an error.
pub async fn fetch(client: &mut Client, batch: &Batch) -> PyResult<Fetched> {
    let mut real = Fetched::Unknown;
    for txid in &batch.order {
        let response = client
            .get_transaction(TxFilter { block: None, index: 0, hash: txid.to_vec() })
            .await;
        if *txid != batch.real {
            continue;
        }
        match response {
            Ok(raw) => real = Fetched::Found(raw.into_inner().data),
            Err(status) if status.code() == tonic::Code::NotFound => {}
            Err(e) => return Err(error::network("GetTransaction failed")(e)),
        }
    }
    Ok(real)
}

/// Decrypts and stores a fetched transaction, or records that the server did not know it.
pub fn store(db: &mut Db, network: &Network, batch: &Batch, fetched: Fetched) -> PyResult<bool> {
    let txid = TxId::from_bytes(batch.real);
    let height = BlockHeight::from_u32(batch.height);
    let Fetched::Found(data) = fetched else {
        db.set_transaction_status(txid, TransactionStatus::TxidNotRecognized)
            .map_err(error::wallet("cannot record transaction status"))?;
        return Ok(false);
    };
    let tx = Transaction::read(&data[..], BranchId::for_height(network, height))
        .map_err(error::wallet("server sent an unparseable transaction"))?;
    if tx.txid() != txid {
        return Err(error::NetworkError::new_err("server answered with a different transaction"));
    }
    decrypt_and_store_transaction(network, db, &tx, Some(height))
        .map_err(error::wallet("cannot store fetched transaction"))?;
    Ok(true)
}

/// Drops finished fetches from the book.
pub fn forget(db_path: &str, done: &[Txid]) -> PyResult<()> {
    let path = book_path(db_path);
    let mut book = Book::load(&path);
    for t in done {
        book.pending.remove(t);
    }
    book.save(&path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(n: u8) -> Txid {
        [n; 32]
    }

    #[test]
    fn choose_needs_k_candidates() {
        let cands: Vec<_> = (1..=3).map(|n| (100, t(n))).collect();
        assert!(choose(t(0), 100, &cands, 4, &mut UnwrapErr(SysRng)).is_none());
        let got = choose(t(0), 100, &cands, 3, &mut UnwrapErr(SysRng)).unwrap();
        assert_eq!(got.len(), 3);
        assert!(!got.contains(&t(0)));
    }

    #[test]
    fn real_rank_in_window_is_uniform() {
        // Twenty candidates around the real one; k = 4 allows ranks 0..=4.
        let real = t(100);
        let cands: Vec<_> = (0..20u8).map(|n| (90 + u32::from(n), t(n))).collect();
        let mut counts = [0u32; 5];
        let mut rng = UnwrapErr(SysRng);
        for _ in 0..5_000 {
            let decoys = choose(real, 100, &cands, 4, &mut rng).unwrap();
            let mut window: Vec<(u32, Txid)> =
                cands.iter().copied().filter(|(_, x)| decoys.contains(x)).collect();
            window.push((100, real));
            window.sort();
            let rank = window.iter().position(|(_, x)| *x == real).unwrap();
            counts[rank] += 1;
        }
        for c in counts {
            assert!((850..1150).contains(&c), "rank counts should be near 1000 each: {counts:?}");
        }
    }

    #[test]
    fn book_collects_nearby_candidates_and_skips_own() {
        let mut book = Book::default();
        let own: HashSet<Txid> = [t(9)].into();
        let blocks: BlockTxids = vec![(100, vec![t(1), t(9)]), (101, vec![t(2)]), (400, vec![t(3)])];
        book.add(&blocks, &[(t(9), 100)], &own);
        let (height, cands) = &book.pending[&t(9)];
        assert_eq!(*height, 100);
        assert_eq!(cands, &vec![(100, t(1)), (101, t(2))], "own tx and far blocks excluded");
        // Later blocks top the entry up; a fetch no longer needed is dropped.
        book.add(&vec![(102, vec![t(4)])], &[(t(9), 100)], &own);
        assert_eq!(book.pending[&t(9)].1.len(), 3);
        book.add(&vec![], &[], &own);
        assert!(book.pending.is_empty());
    }

    #[test]
    fn book_round_trips_through_its_file() {
        let mut book = Book::default();
        book.add(&vec![(5, vec![t(1), t(2)])], &[(t(7), 5)], &HashSet::new());
        let dir = std::env::temp_dir().join(format!("zecpy-book-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("memo_decoys.txt");
        book.save(&path).unwrap();
        assert_eq!(Book::load(&path), book);
        std::fs::remove_dir_all(dir).unwrap();
    }
}
