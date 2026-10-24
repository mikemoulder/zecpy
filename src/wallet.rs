//! The native wallet: one account, backed by a zcash_client_sqlite database.

use std::collections::HashMap;
use std::sync::Arc;

use bip0039::{Count, English, Mnemonic};
use pyo3::prelude::*;
use rand::{rand_core::UnwrapErr, rngs::SysRng};
use secrecy::SecretVec;
use tokio::sync::Mutex;
use zcash_client_backend::data_api::wallet::ConfirmationsPolicy;
use zcash_client_backend::data_api::{AccountBirthday, WalletRead, WalletWrite};
use zcash_client_backend::proto::service::{BlockId, ChainSpec};
use zcash_client_sqlite::util::SystemClock;
use zcash_client_sqlite::wallet::init::init_wallet_db;
use zcash_client_sqlite::{AccountUuid, WalletDb};
use zcash_keys::keys::UnifiedAddressRequest;
use zcash_protocol::consensus::{BlockHeight, NetworkUpgrade, Parameters};
use zeroize::Zeroize;

use crate::cache::MemoryBlockCache;
use crate::error::{self, WalletError};
use crate::net::{Client, Server};
use crate::network::Network;

type Db = WalletDb<rusqlite::Connection, Network, SystemClock, UnwrapErr<SysRng>>;

/// Blocks downloaded and scanned per batch during sync.
const SYNC_BATCH: u32 = 1_000;

/// Birthdays are rounded down to a multiple of this many blocks before the wallet asks the
/// server for the tree state there, so the request reveals only a coarse creation window
/// (about 8.7 days on Mainnet) instead of the exact block.
const BIRTHDAY_BUCKET: u32 = 10_000;

struct Inner {
    db: Mutex<Db>,
    network: Network,
    server: Server,
    cache: MemoryBlockCache,
}

#[pyclass(module = "zecpy._native", frozen)]
pub struct NativeWallet {
    inner: Arc<Inner>,
}

#[pyclass(module = "zecpy._native", frozen, get_all)]
pub struct Balance {
    /// Everything the wallet holds, spendable or not, in zatoshis.
    pub total: u64,
    /// What can be spent right now.
    pub spendable: u64,
    /// Received or change value still waiting for confirmations.
    pub pending: u64,
    pub sapling: u64,
    pub orchard: u64,
    pub ironwood: u64,
    pub chain_tip: u32,
    pub scanned_height: u32,
}

fn open_db(path: &str, network: Network) -> PyResult<Db> {
    let mut db = WalletDb::for_path(path, network, SystemClock, UnwrapErr(SysRng))
        .map_err(error::wallet("cannot open wallet database"))?;
    init_wallet_db(&mut db, None).map_err(error::wallet("cannot initialise wallet database"))?;
    Ok(db)
}

async fn chain_tip(client: &mut Client) -> PyResult<u32> {
    let block = client
        .get_latest_block(ChainSpec::default())
        .await
        .map_err(error::network("GetLatestBlock failed"))?
        .into_inner();
    u32::try_from(block.height).map_err(|_| error::NetworkError::new_err("server reported an impossible height"))
}

/// The earliest height a birthday can take: the block before it must be at least Sapling
/// activation (no tree state exists earlier), and must not be 0, which lightwalletd reads as
/// "no height given".
fn birthday_floor(network: &Network) -> u32 {
    let sapling = network
        .activation_height(NetworkUpgrade::Sapling)
        .map(u32::from)
        .unwrap_or(1);
    (sapling + 1).max(2)
}

/// Rounds `requested` down to the bucket boundary, clamped to the floor and the chain tip.
fn coarsen_birthday(network: &Network, requested: u32, tip: u32) -> u32 {
    let bucketed = requested.min(tip) / BIRTHDAY_BUCKET * BIRTHDAY_BUCKET;
    bucketed.max(birthday_floor(network)).min(tip.max(birthday_floor(network)))
}

async fn fetch_birthday(client: &mut Client, height: u32) -> PyResult<AccountBirthday> {
    let tree_state = client
        .get_tree_state(BlockId {
            height: u64::from(height - 1),
            hash: vec![],
        })
        .await
        .map_err(error::network("GetTreeState failed"))?
        .into_inner();
    AccountBirthday::from_treestate(tree_state, None)
        .map_err(|e| WalletError::new_err(format!("server returned an unusable tree state: {e:?}")))
}

fn first_account(db: &Db) -> PyResult<AccountUuid> {
    db.get_account_ids()
        .map_err(error::wallet("cannot list accounts"))?
        .into_iter()
        .next()
        .ok_or_else(|| WalletError::new_err("wallet database has no account"))
}

#[pymethods]
impl NativeWallet {
    /// Creates a wallet database with one account. Returns the wallet, the mnemonic phrase
    /// (generated if none was given) and the birthday height actually used.
    #[staticmethod]
    #[pyo3(signature = (path, network, heights, server, tor, phrase=None, birthday=None))]
    fn create<'py>(
        py: Python<'py>,
        path: String,
        network: &str,
        heights: Option<HashMap<String, u32>>,
        server: &str,
        tor: bool,
        phrase: Option<String>,
        birthday: Option<u32>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let network = Network::from_spec(network, heights.as_ref())?;
        let server = Server::new(server, tor)?;
        let restoring = phrase.is_some();
        let mnemonic = match phrase {
            Some(mut p) => {
                let parsed = <Mnemonic<English>>::from_phrase(p.trim())
                    .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("invalid mnemonic: {e}")));
                p.zeroize();
                parsed?
            }
            None => Mnemonic::generate(Count::Words24),
        };

        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let mut client = server.connect().await?;
            let tip = chain_tip(&mut client).await?;
            // A new wallet cannot have received anything before now; a restored one needs
            // the caller's birthday, or scans from the earliest possible height.
            let requested = match (birthday, restoring) {
                (Some(h), _) => h,
                (None, false) => tip,
                (None, true) => 0,
            };
            let height = coarsen_birthday(&network, requested, tip);
            let account_birthday = fetch_birthday(&mut client, height).await?;

            let seed = {
                let mut bytes = mnemonic.to_seed("");
                let secret = SecretVec::new(bytes.to_vec());
                bytes.zeroize();
                secret
            };
            let phrase_out = mnemonic.phrase().to_string();

            let db = tokio::task::spawn_blocking(move || -> PyResult<Db> {
                let mut db = open_db(&path, network)?;
                db.create_account("default", &seed, &account_birthday, None)
                    .map_err(error::wallet("cannot create account"))?;
                db.update_chain_tip(BlockHeight::from_u32(tip))
                    .map_err(error::wallet("cannot record chain tip"))?;
                Ok(db)
            })
            .await
            .map_err(error::wallet("wallet task failed"))??;

            let wallet = NativeWallet {
                inner: Arc::new(Inner {
                    db: Mutex::new(db),
                    network,
                    server,
                    cache: MemoryBlockCache::default(),
                }),
            };
            Ok((wallet, phrase_out, height))
        })
    }

    /// Opens an existing wallet database.
    #[staticmethod]
    fn open(
        path: &str,
        network: &str,
        heights: Option<HashMap<String, u32>>,
        server: &str,
        tor: bool,
    ) -> PyResult<Self> {
        let network = Network::from_spec(network, heights.as_ref())?;
        let server = Server::new(server, tor)?;
        let db = open_db(path, network)?;
        first_account(&db)?;
        Ok(NativeWallet {
            inner: Arc::new(Inner {
                db: Mutex::new(db),
                network,
                server,
                cache: MemoryBlockCache::default(),
            }),
        })
    }

    /// Downloads and scans compact blocks up to the chain tip, rewinding on reorgs.
    /// Returns the height the wallet is now synced to.
    fn sync<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let mut client = inner.server.connect().await?;
            let mut db = inner.db.lock().await;
            zcash_client_backend::sync::run(&mut client, &inner.network, &inner.cache, &mut *db, SYNC_BATCH)
                .await
                .map_err(error::wallet("sync failed"))?;
            let scanned = db
                .block_fully_scanned()
                .map_err(error::wallet("cannot read scan progress"))?
                .map(|m| u32::from(m.block_height()))
                .unwrap_or(0);
            Ok(scanned)
        })
    }

    fn balance(&self, py: Python<'_>) -> PyResult<Balance> {
        let inner = Arc::clone(&self.inner);
        py.detach(move || {
            let db = inner.db.blocking_lock();
            let account = first_account(&db)?;
            let summary = db
                .get_wallet_summary(ConfirmationsPolicy::default())
                .map_err(error::wallet("cannot read balance"))?
                .ok_or_else(|| WalletError::new_err("wallet has not synced yet"))?;
            let bal = summary
                .account_balances()
                .get(&account)
                .ok_or_else(|| WalletError::new_err("account missing from wallet summary"))?;
            let zat = |z: zcash_protocol::value::Zatoshis| u64::from(z);
            let pending = [bal.sapling_balance(), bal.orchard_balance(), bal.ironwood_balance()]
                .iter()
                .map(|b| zat(b.change_pending_confirmation()) + zat(b.value_pending_spendability()))
                .sum();
            Ok(Balance {
                total: zat(bal.total()),
                spendable: zat(bal.spendable_value()),
                pending,
                sapling: zat(bal.sapling_balance().spendable_value()),
                orchard: zat(bal.orchard_balance().spendable_value()),
                ironwood: zat(bal.ironwood_balance().spendable_value()),
                chain_tip: u32::from(summary.chain_tip_height()),
                scanned_height: u32::from(summary.fully_scanned_height()),
            })
        })
    }

    /// A fresh Unified Address with shielded receivers only.
    fn address(&self, py: Python<'_>) -> PyResult<String> {
        let inner = Arc::clone(&self.inner);
        py.detach(move || {
            let mut db = inner.db.blocking_lock();
            let account = first_account(&db)?;
            let (ua, _) = db
                .get_next_available_address(account, UnifiedAddressRequest::SHIELDED)
                .map_err(error::wallet("cannot derive address"))?
                .ok_or_else(|| WalletError::new_err("account cannot produce a shielded address"))?;
            Ok(ua.encode(&inner.network))
        })
    }

    #[getter]
    fn network(&self) -> &'static str {
        self.inner.network.name()
    }
}
