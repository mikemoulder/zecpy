//! Compact-block sync that does not tell the server where the wallet left off.
//!
//! Scanning a range of blocks needs the note commitment tree state as of the block before
//! it. librustzcash's `sync::run` fetches that from the server for every batch, so each
//! incremental sync asks for the tree state at the height where the previous sync ended:
//! the server can link one session to the next. The leak audit flagged it
//! (tests/test_devnet_leak_audit.py).
//!
//! This loop avoids it:
//! 1. The session's target is the tip reported by `GetLatestTreeState`, a request every
//!    wallet makes. That state is saved and becomes the starting state of the next sync.
//! 2. Adjacent scan ranges are merged and scanned as one segment, so there is no tree
//!    state to fetch at internal range boundaries.
//! 3. Long segments are split only at absolute multiples of `BATCH`, so any tree state
//!    fetched mid-segment is one that every wallet crossing that height would ask for.
//!
//! After a reorg the wallet rewinds and must fetch the state at the rewind height. That is
//! rare, and the audit reports it rather than hiding it.

use std::ops::Range;
use std::path::{Path, PathBuf};

use futures_util::TryStreamExt;
use orchard::tree::MerkleHashOrchard;
use prost::Message;
use pyo3::prelude::*;
use zcash_client_backend::data_api::chain::{
    BlockCache, ChainState, CommitmentTreeRoot, error::Error as ChainError, scan_cached_blocks,
};
use zcash_client_backend::data_api::{WalletCommitmentTrees, WalletRead, WalletWrite};
use zcash_client_backend::proto::service::{
    self, BlockId, BlockRange, Empty, GetSubtreeRootsArg, TreeState,
};
use zcash_primitives::merkle_tree::HashSer;
use zcash_protocol::consensus::BlockHeight;

use crate::cache::MemoryBlockCache;
use crate::error::{self, NetworkError, WalletError};
use crate::net::Client;
use crate::network::Network;
use crate::wallet::Db;

/// Long segments are cut at absolute multiples of this height. Also the alignment the leak
/// audit accepts as coarse; ZecPy's birthday bucket is a multiple of it.
pub const BATCH: u32 = 1_000;

/// A chain that reorgs this many times during one sync is not converging.
const MAX_ROUNDS: usize = 10;

/// How far below a continuity error to rewind, as librustzcash's own sync does.
const REORG_REWIND_MARGIN: u32 = 10;

/// The saved tree state at the height the last sync reached.
pub fn saved_state_path(db_path: &str) -> PathBuf {
    Path::new(db_path).with_file_name("chain_state.bin")
}

fn load_saved(path: &Path) -> Option<ChainState> {
    let bytes = std::fs::read(path).ok()?;
    TreeState::decode(bytes.as_slice()).ok()?.to_chain_state().ok()
}

fn save(path: &Path, state: &TreeState) -> PyResult<()> {
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, state.encode_to_vec()).map_err(error::wallet("cannot save chain state"))?;
    std::fs::rename(&tmp, path).map_err(error::wallet("cannot save chain state"))
}

async fn fetch_tree_state(client: &mut Client, height: BlockHeight) -> PyResult<ChainState> {
    client
        .get_tree_state(BlockId { height: u64::from(height), hash: vec![] })
        .await
        .map_err(error::network("GetTreeState failed"))?
        .into_inner()
        .to_chain_state()
        .map_err(|e| NetworkError::new_err(format!("server sent an unusable tree state: {e}")))
}

async fn subtree_roots<H: HashSer>(
    client: &mut Client,
    protocol: service::ShieldedProtocol,
) -> PyResult<Vec<CommitmentTreeRoot<H>>> {
    let mut request = GetSubtreeRootsArg::default();
    request.set_shielded_protocol(protocol);
    client
        .get_subtree_roots(request)
        .await
        .map_err(error::network("GetSubtreeRoots failed"))?
        .into_inner()
        .map_err(|e| std::io::Error::other(e.to_string()))
        .and_then(|root| async move {
            Ok(CommitmentTreeRoot::from_parts(
                BlockHeight::from_u32(root.completing_block_height as u32),
                H::read(&root.root_hash[..])?,
            ))
        })
        .try_collect()
        .await
        .map_err(error::network("bad subtree root from server"))
}

async fn update_subtree_roots(client: &mut Client, db: &mut Db) -> PyResult<()> {
    let sapling = subtree_roots::<sapling::Node>(client, service::ShieldedProtocol::Sapling).await?;
    db.put_sapling_subtree_roots(0, &sapling)
        .map_err(error::wallet("cannot store Sapling subtree roots"))?;
    let orchard = subtree_roots::<MerkleHashOrchard>(client, service::ShieldedProtocol::Orchard).await?;
    db.put_orchard_subtree_roots(0, &orchard)
        .map_err(error::wallet("cannot store Orchard subtree roots"))?;
    let ironwood = subtree_roots::<MerkleHashOrchard>(client, service::ShieldedProtocol::Ironwood).await?;
    db.put_ironwood_subtree_roots(0, &ironwood)
        .map_err(error::wallet("cannot store Ironwood subtree roots"))?;
    Ok(())
}

/// Merges the wallet's suggested ranges into contiguous segments, in height order, ending
/// no later than `tip`.
fn segments(mut ranges: Vec<Range<BlockHeight>>, tip: BlockHeight) -> Vec<Range<BlockHeight>> {
    ranges.sort_by_key(|r| r.start);
    let mut merged: Vec<Range<BlockHeight>> = Vec::new();
    for r in ranges {
        let r = r.start..r.end.min(tip + 1);
        if r.is_empty() {
            continue;
        }
        match merged.last_mut() {
            Some(last) if last.end >= r.start => last.end = last.end.max(r.end),
            _ => merged.push(r),
        }
    }
    merged
}

/// Splits a segment at absolute multiples of `BATCH`.
fn batches(segment: &Range<BlockHeight>) -> Vec<Range<BlockHeight>> {
    let mut out = Vec::new();
    let mut start = u32::from(segment.start);
    let end = u32::from(segment.end);
    while start < end {
        let next = ((start / BATCH) + 1) * BATCH;
        let stop = next.min(end);
        out.push(BlockHeight::from_u32(start)..BlockHeight::from_u32(stop));
        start = stop;
    }
    out
}

/// Downloads a batch into the cache. Returns the shielded txids per block, which serve as
/// memo-fetch decoys (memos.rs) at no extra request.
async fn download(
    client: &mut Client,
    cache: &MemoryBlockCache,
    range: &Range<BlockHeight>,
) -> PyResult<crate::memos::BlockTxids> {
    let request = BlockRange {
        start: Some(BlockId { height: u64::from(range.start), hash: vec![] }),
        end: Some(BlockId { height: u64::from(range.end - 1), hash: vec![] }),
        pool_types: vec![],
    };
    let blocks = client
        .get_block_range(request)
        .await
        .map_err(error::network("GetBlockRange failed"))?
        .into_inner()
        .try_collect::<Vec<_>>()
        .await
        .map_err(error::network("GetBlockRange stream failed"))?;
    let txids = crate::memos::shielded_txids(&blocks);
    cache.insert(blocks).await.expect("memory cache cannot fail");
    Ok(txids)
}

/// Brings the wallet up to the chain tip.
pub async fn run(
    client: &mut Client,
    network: &Network,
    cache: &MemoryBlockCache,
    db: &mut Db,
    db_path: &str,
) -> PyResult<()> {
    let state_path = saved_state_path(db_path);
    update_subtree_roots(client, db).await?;

    for _ in 0..MAX_ROUNDS {
        let latest = client
            .get_latest_tree_state(Empty {})
            .await
            .map_err(error::network("GetLatestTreeState failed"))?
            .into_inner();
        let latest_state = latest
            .to_chain_state()
            .map_err(|e| NetworkError::new_err(format!("server sent an unusable tree state: {e}")))?;
        let tip = latest_state.block_height();

        // A tip at or below what we already scanned produces no new scan range, so a reorg
        // onto a branch that is not (yet) longer would go unnoticed. Compare block hashes:
        // a different block at the server's tip height is a reorg; the same block means the
        // server is merely behind us.
        let max_scanned = db
            .block_max_scanned()
            .map_err(error::wallet("cannot read scan progress"))?
            .map(|m| m.block_height());
        if max_scanned.is_some_and(|max| tip <= max) {
            let ours = db.get_block_hash(tip).map_err(error::wallet("cannot read block hash"))?;
            if ours.is_some_and(|hash| hash != latest_state.block_hash()) {
                db.truncate_to_height(tip.saturating_sub(REORG_REWIND_MARGIN))
                    .map_err(error::wallet("cannot rewind after reorg"))?;
                let _ = std::fs::remove_file(&state_path);
                continue;
            }
        }

        db.update_chain_tip(tip).map_err(error::wallet("cannot record chain tip"))?;

        let mut known = load_saved(&state_path);
        let suggested = db
            .suggest_scan_ranges()
            .map_err(error::wallet("cannot plan scan"))?
            .into_iter()
            .map(|r| r.block_range().clone())
            .collect();

        let mut reorged = false;
        'segments: for segment in segments(suggested, tip) {
            for batch in batches(&segment) {
                let prior = batch.start - 1;
                let from_state = match known.take() {
                    Some(state) if state.block_height() == prior => state,
                    _ => fetch_tree_state(client, prior).await?,
                };
                let txids = download(client, cache, &batch).await?;
                let scanned = scan_cached_blocks(
                    network,
                    cache,
                    db,
                    batch.start,
                    &from_state,
                    usize::try_from(u32::from(batch.end) - u32::from(batch.start)).expect("fits"),
                );
                cache
                    .delete(zcash_client_backend::data_api::scanning::ScanRange::from_parts(
                        batch.clone(),
                        zcash_client_backend::data_api::scanning::ScanPriority::Ignored,
                    ))
                    .await
                    .expect("memory cache cannot fail");
                match scanned {
                    Ok(_) => crate::memos::record(db_path, &txids)?,
                    Err(ChainError::Scan(err)) if err.is_continuity_error() => {
                        let rewind = err.at_height().saturating_sub(REORG_REWIND_MARGIN);
                        db.truncate_to_height(rewind)
                            .map_err(error::wallet("cannot rewind after reorg"))?;
                        let _ = std::fs::remove_file(&state_path);
                        reorged = true;
                        break 'segments;
                    }
                    Err(e) => return Err(WalletError::new_err(format!("scan failed: {e}"))),
                }
            }
        }
        if reorged {
            continue;
        }

        let scanned_to = db
            .block_fully_scanned()
            .map_err(error::wallet("cannot read scan progress"))?
            .map(|m| m.block_height());
        if scanned_to == Some(tip) {
            save(&state_path, &latest)?;
        }
        return Ok(());
    }
    Err(WalletError::new_err("the chain kept reorganising during sync; try again"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn h(n: u32) -> BlockHeight {
        BlockHeight::from_u32(n)
    }

    #[test]
    fn adjacent_ranges_merge_into_one_segment() {
        let verify = h(335)..h(345);
        let chain_tip = h(345)..h(400);
        assert_eq!(segments(vec![chain_tip, verify], h(399)), vec![h(335)..h(400)]);
    }

    #[test]
    fn segments_stop_at_the_tip_and_keep_gaps() {
        let got = segments(vec![h(10)..h(20), h(30)..h(60)], h(49));
        assert_eq!(got, vec![h(10)..h(20), h(30)..h(50)]);
    }

    #[test]
    fn batches_split_only_at_absolute_multiples() {
        let got = batches(&(h(1_335)..h(3_100)));
        assert_eq!(got, vec![h(1_335)..h(2_000), h(2_000)..h(3_000), h(3_000)..h(3_100)]);
    }
}
