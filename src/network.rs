//! Consensus parameters for the three networks ZecPy runs on.

use std::collections::HashMap;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use zcash_protocol::consensus::{self, BlockHeight, NetworkType, NetworkUpgrade, Parameters};
use zcash_protocol::local_consensus::LocalNetwork;

#[derive(Clone, Copy, Debug)]
pub enum Network {
    Main,
    Test,
    /// A private regtest chain with caller-chosen activation heights (see devnet/).
    Regtest(LocalNetwork),
}

impl Network {
    /// Builds a network from its name and, for regtest, the activation heights that the
    /// validator was configured with. The keys match devnet/activation-heights.toml.
    pub fn from_spec(name: &str, heights: Option<&HashMap<String, u32>>) -> PyResult<Self> {
        match (name, heights) {
            ("main", None) => Ok(Network::Main),
            ("test", None) => Ok(Network::Test),
            ("regtest", Some(h)) => {
                let known = [
                    "overwinter", "sapling", "blossom", "heartwood", "canopy", "nu5", "nu6",
                    "nu6_1", "nu6_2", "nu6_3", "nu7",
                ];
                if let Some(unknown) = h.keys().find(|k| !known.contains(&k.as_str())) {
                    return Err(PyValueError::new_err(format!(
                        "unknown network upgrade {unknown:?} in regtest activation heights"
                    )));
                }
                let at = |k: &str| h.get(k).map(|&v| BlockHeight::from_u32(v));
                Ok(Network::Regtest(LocalNetwork {
                    overwinter: at("overwinter"),
                    sapling: at("sapling"),
                    blossom: at("blossom"),
                    heartwood: at("heartwood"),
                    canopy: at("canopy"),
                    nu5: at("nu5"),
                    nu6: at("nu6"),
                    nu6_1: at("nu6_1"),
                    nu6_2: at("nu6_2"),
                    nu6_3: at("nu6_3"),
                    nu7: at("nu7"),
                }))
            }
            ("regtest", None) => Err(PyValueError::new_err(
                "regtest needs the validator's activation heights",
            )),
            ("main" | "test", Some(_)) => Err(PyValueError::new_err(
                "activation heights are fixed on main and test; pass them only for regtest",
            )),
            _ => Err(PyValueError::new_err(format!(
                "unknown network {name:?}; expected \"main\", \"test\" or \"regtest\""
            ))),
        }
    }

    pub fn name(&self) -> &'static str {
        match self {
            Network::Main => "main",
            Network::Test => "test",
            Network::Regtest(_) => "regtest",
        }
    }
}

impl Parameters for Network {
    fn network_type(&self) -> NetworkType {
        match self {
            Network::Main => NetworkType::Main,
            Network::Test => NetworkType::Test,
            Network::Regtest(_) => NetworkType::Regtest,
        }
    }

    fn activation_height(&self, nu: NetworkUpgrade) -> Option<BlockHeight> {
        match self {
            Network::Main => consensus::Network::MainNetwork.activation_height(nu),
            Network::Test => consensus::Network::TestNetwork.activation_height(nu),
            Network::Regtest(local) => local.activation_height(nu),
        }
    }
}
