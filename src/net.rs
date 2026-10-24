//! Connections to a light server (lightwalletd or Zaino).
//!
//! Remote servers require Tor unless the caller explicitly opts out. A failed Tor connection
//! is an error, never a silent fallback to a direct connection.

use std::net::IpAddr;

use pyo3::prelude::*;
use tonic::transport::{Channel, ClientTlsConfig, Endpoint, Uri};
use zcash_client_backend::proto::service::compact_tx_streamer_client::CompactTxStreamerClient;

use crate::error::{self, PrivacyError};

pub type Client = CompactTxStreamerClient<Channel>;

#[derive(Clone, Debug)]
pub struct Server {
    uri: Uri,
    tor: bool,
}

impl Server {
    pub fn new(url: &str, tor: bool) -> PyResult<Self> {
        let uri: Uri = url
            .parse()
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(format!("bad server URL {url:?}: {e}")))?;
        match uri.scheme_str() {
            Some("http" | "https") => {}
            _ => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "server URL {url:?} must start with http:// or https://"
                )));
            }
        }
        if uri.host().is_none() {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "server URL {url:?} has no host"
            )));
        }
        Ok(Server { uri, tor })
    }

    fn is_loopback(&self) -> bool {
        let host = self.uri.host().unwrap_or_default().trim_matches(['[', ']']);
        host == "localhost" || host.parse::<IpAddr>().is_ok_and(|ip| ip.is_loopback())
    }

    pub async fn connect(&self) -> PyResult<Client> {
        // Tor cannot reach loopback, and a server on this machine learns nothing new from
        // our IP, so loopback is always direct.
        if self.tor && !self.is_loopback() {
            return Err(PrivacyError::new_err(
                "connecting to a remote server requires Tor, which this build does not include \
                 yet; pass tor=False to connect directly and accept that the server sees your IP",
            ));
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
