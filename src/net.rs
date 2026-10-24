//! Connections to a light server (lightwalletd or Zaino).
//!
//! Remote servers are reached over Tor unless the caller explicitly opts out. Every
//! operation gets its own isolated Tor circuit: one sync session never shares a circuit with
//! the previous one, and a broadcast never shares one with a sync. The server therefore
//! cannot use the network path to link "this wallet synced" to "this transaction appeared",
//! and ZecPy's sync loop does not link sessions by content either (see sync.rs).
//!
//! A Tor failure is an error. ZecPy never falls back to a direct connection.

use std::net::IpAddr;
use std::path::PathBuf;
use std::sync::Arc;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use tokio::sync::OnceCell;
use tonic::transport::{Channel, ClientTlsConfig, Endpoint, Uri};
use zcash_client_backend::proto::service::compact_tx_streamer_client::CompactTxStreamerClient;
use zcash_client_backend::tor;

use crate::error::{self, NetworkError};

pub type Client = CompactTxStreamerClient<Channel>;

/// Fresh circuits tried before a Tor connection is reported as failed.
const TOR_CONNECT_ATTEMPTS: usize = 3;

/// What a connection is for. Each purpose, and each use of it, gets a fresh circuit.
#[derive(Clone, Copy, Debug)]
pub enum Purpose {
    /// Wallet creation and syncing.
    Sync,
    /// Publishing a transaction.
    Broadcast,
}

#[derive(Clone)]
pub struct Server {
    uri: Uri,
    tor: bool,
    /// Where arti keeps its state, so later bootstraps are fast. Inside the wallet directory.
    tor_dir: PathBuf,
    /// Bootstrapped once per wallet, on first use; isolated handles are cut from it.
    tor_client: Arc<OnceCell<tor::Client>>,
}

impl Server {
    pub fn new(url: &str, tor: bool, tor_dir: PathBuf) -> PyResult<Self> {
        let uri: Uri = url
            .parse()
            .map_err(|e| PyValueError::new_err(format!("bad server URL {url:?}: {e}")))?;
        if !matches!(uri.scheme_str(), Some("http" | "https")) {
            return Err(PyValueError::new_err(format!(
                "server URL {url:?} must start with http:// or https://"
            )));
        }
        if uri.host().is_none() {
            return Err(PyValueError::new_err(format!("server URL {url:?} has no host")));
        }
        Ok(Server { uri, tor, tor_dir, tor_client: Arc::new(OnceCell::new()) })
    }

    fn is_loopback(&self) -> bool {
        let host = self.uri.host().unwrap_or_default().trim_matches(['[', ']']);
        host == "localhost" || host.parse::<IpAddr>().is_ok_and(|ip| ip.is_loopback())
    }

    /// Whether connections to this server go through Tor.
    pub fn uses_tor(&self) -> bool {
        // Tor cannot reach loopback, and a server on this machine learns nothing from our IP.
        self.tor && !self.is_loopback()
    }

    async fn tor_client(&self) -> PyResult<&tor::Client> {
        self.tor_client
            .get_or_try_init(|| async {
                std::fs::create_dir_all(&self.tor_dir)
                    .map_err(error::network("cannot create the Tor state directory"))?;
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    std::fs::set_permissions(&self.tor_dir, std::fs::Permissions::from_mode(0o700))
                        .map_err(error::network("cannot restrict the Tor state directory"))?;
                }
                tor::Client::create(&self.tor_dir, |_| {})
                    .await
                    .map_err(|e| NetworkError::new_err(format!("Tor failed to start: {e}")))
            })
            .await
    }

    pub async fn connect(&self, purpose: Purpose) -> PyResult<Client> {
        if self.uses_tor() {
            // Individual circuits fail now and then (a slow relay, an exit that refuses the
            // port). Retry on a fresh isolated circuit each time; never fall back to direct.
            let base = self.tor_client().await?;
            let mut last_error = None;
            for _ in 0..TOR_CONNECT_ATTEMPTS {
                match base.isolated_client().connect_to_lightwalletd(self.uri.clone(), true).await {
                    Ok(client) => return Ok(client),
                    Err(e) => last_error = Some(e),
                }
            }
            return Err(NetworkError::new_err(format!(
                "cannot reach {} over Tor for {purpose:?} after {TOR_CONNECT_ATTEMPTS} circuits: {}",
                self.uri,
                last_error.expect("at least one attempt")
            )));
        }
        let mut endpoint = Endpoint::from(self.uri.clone());
        if self.uri.scheme_str() == Some("https") {
            let tls = ClientTlsConfig::new()
                .with_webpki_roots()
                .domain_name(self.uri.host().unwrap_or_default());
            endpoint = endpoint.tls_config(tls).map_err(error::network("TLS setup failed"))?;
        }
        let channel = endpoint
            .connect()
            .await
            .map_err(error::network(&format!("cannot reach {}", self.uri)))?;
        Ok(CompactTxStreamerClient::new(channel))
    }
}
