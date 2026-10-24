//! An in-memory compact block cache for `zcash_client_backend::sync::run`.
//!
//! Sync downloads at most one batch of compact blocks, scans it, and deletes it, so the cache
//! never holds more than a batch. Keeping it in memory avoids writing the chain to disk.

use std::convert::Infallible;
use std::sync::Mutex;

use async_trait::async_trait;
use zcash_client_backend::data_api::chain::{BlockCache, BlockSource, error::Error as ChainError};
use zcash_client_backend::data_api::scanning::ScanRange;
use zcash_client_backend::proto::compact_formats::CompactBlock;
use zcash_protocol::consensus::BlockHeight;

#[derive(Default)]
pub struct MemoryBlockCache {
    blocks: Mutex<Vec<CompactBlock>>,
}

fn height_of(block: &CompactBlock) -> BlockHeight {
    BlockHeight::from_u32(u32::try_from(block.height).expect("block heights fit in u32"))
}

impl BlockSource for MemoryBlockCache {
    type Error = Infallible;

    fn with_blocks<F, WalletErrT>(
        &self,
        from_height: Option<BlockHeight>,
        limit: Option<usize>,
        mut with_block: F,
    ) -> Result<(), ChainError<WalletErrT, Self::Error>>
    where
        F: FnMut(CompactBlock) -> Result<(), ChainError<WalletErrT, Self::Error>>,
    {
        let mut blocks: Vec<CompactBlock> = self
            .blocks
            .lock()
            .unwrap()
            .iter()
            .filter(|b| from_height.is_none_or(|h| height_of(b) >= h))
            .cloned()
            .collect();
        blocks.sort_by_key(|b| b.height);
        for block in blocks.into_iter().take(limit.unwrap_or(usize::MAX)) {
            with_block(block)?;
        }
        Ok(())
    }
}

#[async_trait]
impl BlockCache for MemoryBlockCache {
    fn get_tip_height(&self, range: Option<&ScanRange>) -> Result<Option<BlockHeight>, Infallible> {
        Ok(self
            .blocks
            .lock()
            .unwrap()
            .iter()
            .map(height_of)
            .filter(|h| range.is_none_or(|r| r.block_range().contains(h)))
            .max())
    }

    async fn read(&self, range: &ScanRange) -> Result<Vec<CompactBlock>, Infallible> {
        let mut blocks: Vec<CompactBlock> = self
            .blocks
            .lock()
            .unwrap()
            .iter()
            .filter(|b| range.block_range().contains(&height_of(b)))
            .cloned()
            .collect();
        blocks.sort_by_key(|b| b.height);
        Ok(blocks)
    }

    async fn insert(&self, mut compact_blocks: Vec<CompactBlock>) -> Result<(), Infallible> {
        let mut blocks = self.blocks.lock().unwrap();
        // A re-download after a reorg replaces the blocks at those heights.
        let incoming: Vec<BlockHeight> = compact_blocks.iter().map(height_of).collect();
        blocks.retain(|b| !incoming.contains(&height_of(b)));
        blocks.append(&mut compact_blocks);
        Ok(())
    }

    async fn delete(&self, range: ScanRange) -> Result<(), Infallible> {
        self.blocks
            .lock()
            .unwrap()
            .retain(|b| !range.block_range().contains(&height_of(b)));
        Ok(())
    }
}
