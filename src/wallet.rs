//! The native wallet: one account, backed by a zcash_client_sqlite database.

use std::collections::HashMap;
use std::sync::Arc;

use bip0039::{Count, English, Mnemonic};
use pyo3::prelude::*;
use rand::{rand_core::UnwrapErr, rngs::SysRng};
use secrecy::SecretVec;
use tokio::sync::Mutex;
use zcash_client_backend::data_api::wallet::ConfirmationsPolicy;
use zcash_client_backend::data_api::{Account, AccountBirthday, WalletRead, WalletWrite};
use zcash_client_backend::proto::service::{BlockId, ChainSpec};
use zcash_client_sqlite::util::SystemClock;
use zcash_client_sqlite::wallet::init::init_wallet_db;
use zcash_client_sqlite::{AccountUuid, WalletDb};
use zcash_keys::keys::{UnifiedAddressRequest, UnifiedSpendingKey};
use zcash_protocol::consensus::{BlockHeight, NetworkUpgrade, Parameters};
use zeroize::Zeroize;
use zip32::DiversifierIndex;

use crate::cache::MemoryBlockCache;
use crate::error::{self, PrivacyError, WalletError};
use crate::net::{Client, Purpose, Server};
use crate::network::Network;
use crate::received;
use crate::send::{self, SendRequest};

pub type Db = WalletDb<rusqlite::Connection, Network, SystemClock, UnwrapErr<SysRng>>;

/// Birthdays are rounded down to a multiple of this many blocks (itself a multiple of
/// `sync::BATCH`, so the birthday tree-state request looks like any batch boundary) before the wallet asks the
/// server for the tree state there, so the request reveals only a coarse creation window
/// (about 8.7 days on Mainnet) instead of the exact block.
const BIRTHDAY_BUCKET: u32 = 10_000;

struct Inner {
    db: Mutex<Db>,
    network: Network,
    server: Server,
    cache: MemoryBlockCache,
    /// Present only when the wallet was created or opened with its mnemonic. Never persisted.
    usk: Option<UnifiedSpendingKey>,
    /// Path of the wallet database, for read-only queries on a separate connection.
    path: String,
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

/// Tor keeps its state next to the wallet database, inside the wallet directory.
fn tor_dir_for(db_path: &str) -> std::path::PathBuf {
    std::path::Path::new(db_path).with_file_name("tor")
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

/// Derives the account's spending key from a mnemonic and checks it belongs to this wallet,
/// so a mistyped phrase fails here instead of producing an unusable key.
fn spending_key_for(db: &Db, network: &Network, mut phrase: String) -> PyResult<UnifiedSpendingKey> {
    let parsed = <Mnemonic<English>>::from_phrase(phrase.trim());
    phrase.zeroize();
    let mnemonic = parsed.map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("invalid mnemonic: {e}")))?;
    let account = db
        .get_account(first_account(db)?)
        .map_err(error::wallet("cannot read account"))?
        .ok_or_else(|| WalletError::new_err("wallet database has no account"))?;
    let derivation = account
        .source()
        .key_derivation()
        .ok_or_else(|| WalletError::new_err("this account was imported from a viewing key and cannot spend"))?;
    let mut seed = mnemonic.to_seed("");
    let usk = UnifiedSpendingKey::from_seed(network, &seed, derivation.account_index())
        .map_err(|e| WalletError::new_err(format!("cannot derive spending key: {e:?}")));
    seed.zeroize();
    let usk = usk?;
    let matches = account
        .ufvk()
        .is_some_and(|ufvk| ufvk.encode(network) == usk.to_unified_full_viewing_key().encode(network));
    if !matches {
        return Err(PrivacyError::new_err("this mnemonic does not belong to this wallet"));
    }
    Ok(usk)
}

pub fn first_account(db: &Db) -> PyResult<AccountUuid> {
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
        let server = Server::new(server, tor, tor_dir_for(&path))?;
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
            let db_path = path.clone();
            let mut client = server.connect(Purpose::Sync).await?;
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

            let (db, usk) = tokio::task::spawn_blocking(move || -> PyResult<(Db, UnifiedSpendingKey)> {
                let mut db = open_db(&path, network)?;
                let (_, usk) = db
                    .create_account("default", &seed, &account_birthday, None)
                    .map_err(error::wallet("cannot create account"))?;
                db.update_chain_tip(BlockHeight::from_u32(tip))
                    .map_err(error::wallet("cannot record chain tip"))?;
                Ok((db, usk))
            })
            .await
            .map_err(error::wallet("wallet task failed"))??;

            let wallet = NativeWallet {
                inner: Arc::new(Inner {
                    db: Mutex::new(db),
                    network,
                    server,
                    cache: MemoryBlockCache::default(),
                    usk: Some(usk),
                    path: db_path,
                }),
            };
            Ok((wallet, phrase_out, height))
        })
    }

    /// Opens an existing wallet database. With the mnemonic the wallet can send; without it
    /// the wallet is watch-only.
    #[staticmethod]
    #[pyo3(signature = (path, network, heights, server, tor, phrase=None))]
    fn open(
        path: &str,
        network: &str,
        heights: Option<HashMap<String, u32>>,
        server: &str,
        tor: bool,
        phrase: Option<String>,
    ) -> PyResult<Self> {
        let network = Network::from_spec(network, heights.as_ref())?;
        let server = Server::new(server, tor, tor_dir_for(path))?;
        let db = open_db(path, network)?;
        let usk = phrase.map(|p| spending_key_for(&db, &network, p)).transpose()?;
        Ok(NativeWallet {
            inner: Arc::new(Inner {
                db: Mutex::new(db),
                network,
                server,
                cache: MemoryBlockCache::default(),
                usk,
                path: path.to_string(),
            }),
        })
    }

    /// Sends a shielded payment and returns its txid. Proving runs off the event loop.
    ///
    /// With `max_broadcast_delay`, the finished transaction waits a uniformly random time up
    /// to that many seconds before it is published, so its appearance on the network is not
    /// timed to the action that caused it.
    #[pyo3(signature = (to, amount_zat, memo=None, allow_transparent=false, max_broadcast_delay=0.0))]
    fn send<'py>(
        &self,
        py: Python<'py>,
        to: &str,
        amount_zat: u64,
        memo: Option<String>,
        allow_transparent: bool,
        max_broadcast_delay: f64,
    ) -> PyResult<Bound<'py, PyAny>> {
        if !(0.0..=3600.0).contains(&max_broadcast_delay) {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "max_broadcast_delay must be between 0 and 3600 seconds",
            ));
        }
        let inner = Arc::clone(&self.inner);
        if inner.usk.is_none() {
            return Err(WalletError::new_err(
                "this wallet is watch-only; open it with its mnemonic to send",
            ));
        }
        let request = SendRequest::new(&inner.network, to, amount_zat, memo.as_deref(), allow_transparent)?;
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            // Connect first, on a circuit of its own: if the server (or Tor) is unreachable,
            // nothing is built or stored.
            let mut client = inner.server.connect(Purpose::Broadcast).await?;
            let mut db = inner.db.lock().await;
            let usk = inner.usk.as_ref().expect("checked above");
            let (txid, raw) =
                tokio::task::block_in_place(|| send::build(&mut db, &inner.network, usk, request))?;
            drop(db);
            if max_broadcast_delay > 0.0 {
                tokio::time::sleep(send::random_delay(max_broadcast_delay)).await;
            }
            send::broadcast(&mut client, raw).await?;
            Ok(txid)
        })
    }

    /// The shielded address at a specific diversifier index. Invoices use indices from their
    /// own range, so each invoice has an address no one else is given.
    fn address_at(&self, py: Python<'_>, index: u128) -> PyResult<String> {
        let inner = Arc::clone(&self.inner);
        py.detach(move || {
            let index = DiversifierIndex::try_from(index)
                .map_err(|_| pyo3::exceptions::PyValueError::new_err("diversifier index must fit in 88 bits"))?;
            let mut db = inner.db.blocking_lock();
            let account = first_account(&db)?;
            let ua = db
                .get_address_for_index(account, index, UnifiedAddressRequest::SHIELDED)
                .map_err(error::wallet("cannot derive address"))?
                .ok_or_else(|| WalletError::new_err("no shielded address exists at this index"))?;
            Ok(ua.encode(&inner.network))
        })
    }

    /// Shielded payments received in transactions observed at or after `since`.
    #[pyo3(signature = (since=0))]
    fn received(&self, py: Python<'_>, since: u32) -> PyResult<Vec<received::ReceivedOutput>> {
        let path = self.inner.path.clone();
        py.detach(move || received::list(&path, since))
    }

    #[getter]
    fn can_send(&self) -> bool {
        self.inner.usk.is_some()
    }

    /// Downloads and scans compact blocks up to the chain tip, rewinding on reorgs.
    /// Returns the height the wallet is now synced to.
    fn sync<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let inner = Arc::clone(&self.inner);
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            let mut client = inner.server.connect(Purpose::Sync).await?;
            let mut db = inner.db.lock().await;
            crate::sync::run(&mut client, &inner.network, &inner.cache, &mut db, &inner.path).await?;
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
