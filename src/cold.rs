//! The offline half of cold signing.
//!
//! A `ColdSigner` holds the spending key and never touches the network. The hot wallet sends
//! it an unsigned, already-proven PCZT; the signer shows what the transaction does, then
//! signs the spends that belong to its seed. Which outputs are change is decided by checking
//! each recipient against the signer's own viewing key, not by trusting labels the hot side
//! wrote, so a compromised hot wallet cannot pass off a payment to itself as change.

use std::collections::HashMap;
use std::convert::Infallible;

use bip0039::{English, Mnemonic};
use pczt::Pczt;
use pczt::roles::signer::Signer;
use pczt::roles::verifier::Verifier;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rand::{rand_core::UnwrapErr, rngs::SysRng};
use zcash_keys::keys::UnifiedSpendingKey;
use zeroize::Zeroize;
use zip32::fingerprint::SeedFingerprint;

use crate::error::{PrivacyError, WalletError};
use crate::network::Network;

/// One output of a transaction, as the signer understands it.
#[pyclass(module = "zecpy._native", frozen, get_all)]
pub struct SignedOutput {
    pub pool: &'static str,
    pub value: u64,
    /// The payment address the hot wallet attached, if any. Informational only.
    pub label: Option<String>,
    /// True when the recipient is one of this signer's own addresses (checked with its
    /// viewing key): the value comes back to this wallet.
    pub is_change: bool,
}

/// What a PCZT will do if signed.
#[pyclass(module = "zecpy._native", frozen, get_all)]
pub struct Summary {
    /// Value of the notes this signer's key will spend.
    pub spending: u64,
    pub outputs: Vec<Py<SignedOutput>>,
    /// Value leaving the wallet to other recipients.
    pub paying_out: u64,
    pub fee: u64,
    /// Spends in the transaction that this signer's seed does not control.
    pub foreign_spends: usize,
}

#[pyclass(module = "zecpy._native", frozen)]
pub struct ColdSigner {
    network: Network,
    usk: UnifiedSpendingKey,
    seed_fingerprint: SeedFingerprint,
    account: zip32::AccountId,
}

struct Plan {
    orchard: Vec<usize>,
    ironwood: Vec<usize>,
    sapling: Vec<usize>,
    spending: u64,
    foreign: usize,
    outputs: Vec<(&'static str, u64, Option<String>, bool)>,
}

impl ColdSigner {
    fn is_ours(&self, derivation: Option<&pczt_derivation::Any<'_>>) -> bool {
        derivation.is_some_and(|d| d.account_index(&self.seed_fingerprint, &self.network) == Some(self.account))
    }

    fn plan(&self, pczt: Pczt) -> PyResult<(Pczt, Plan)> {
        let fvk = self.usk.to_unified_full_viewing_key();
        let orchard_fvk = fvk.orchard().cloned();
        let sapling_dfvk = fvk.sapling().cloned();
        let mut plan = Plan {
            orchard: vec![],
            ironwood: vec![],
            sapling: vec![],
            spending: 0,
            foreign: 0,
            outputs: vec![],
        };

        let visit_orchard = |pool: &'static str, bundle: &orchard::pczt::Bundle, plan: &mut Plan, indices: fn(&mut Plan) -> &mut Vec<usize>| {
            for (index, action) in bundle.actions().iter().enumerate() {
                let spend = action.spend();
                let value = spend.value().map(|v| v.inner()).unwrap_or(0);
                let ours = self.is_ours(spend.zip32_derivation().as_ref().map(pczt_derivation::Any::Orchard).as_ref());
                if ours {
                    indices(plan).push(index);
                    plan.spending += value;
                } else if value > 0 {
                    plan.foreign += 1;
                }
                let output = action.output();
                let value = output.value().map(|v| v.inner()).unwrap_or(0);
                if value == 0 {
                    continue; // padding
                }
                let is_change = match (output.recipient(), orchard_fvk.as_ref()) {
                    (Some(addr), Some(fvk)) => fvk.scope_for_address(addr).is_some(),
                    _ => false,
                };
                plan.outputs.push((pool, value, output.user_address().clone(), is_change));
            }
        };

        let pczt = Verifier::new(pczt)
            .with_orchard::<Infallible, _>(|bundle| {
                visit_orchard("orchard", bundle, &mut plan, |p| &mut p.orchard);
                Ok(())
            })
            .map_err(|e| PyValueError::new_err(format!("invalid PCZT (Orchard): {e:?}")))?
            .with_ironwood::<Infallible, _>(|bundle| {
                visit_orchard("ironwood", bundle, &mut plan, |p| &mut p.ironwood);
                Ok(())
            })
            .map_err(|e| PyValueError::new_err(format!("invalid PCZT (Ironwood): {e:?}")))?
            .with_sapling::<Infallible, _>(|bundle| {
                for (index, spend) in bundle.spends().iter().enumerate() {
                    let value = spend.value().map(|v| v.inner()).unwrap_or(0);
                    if self.is_ours(spend.zip32_derivation().as_ref().map(pczt_derivation::Any::Sapling).as_ref()) {
                        plan.sapling.push(index);
                        plan.spending += value;
                    } else if value > 0 {
                        plan.foreign += 1;
                    }
                }
                for output in bundle.outputs() {
                    let value = output.value().map(|v| v.inner()).unwrap_or(0);
                    if value == 0 {
                        continue;
                    }
                    let is_change = match (output.recipient(), sapling_dfvk.as_ref()) {
                        (Some(addr), Some(dfvk)) => dfvk.decrypt_diversifier(addr).is_some(),
                        _ => false,
                    };
                    plan.outputs.push(("sapling", value, output.user_address().clone(), is_change));
                }
                Ok(())
            })
            .map_err(|e| PyValueError::new_err(format!("invalid PCZT (Sapling): {e:?}")))?
            .finish();
        Ok((pczt, plan))
    }
}

#[pymethods]
impl ColdSigner {
    #[new]
    #[pyo3(signature = (phrase, network, heights=None, account=0))]
    fn new(mut phrase: String, network: &str, heights: Option<HashMap<String, u32>>, account: u32) -> PyResult<Self> {
        let network = Network::from_spec(network, heights.as_ref())?;
        let parsed = <Mnemonic<English>>::from_phrase(phrase.trim());
        phrase.zeroize();
        let mnemonic = parsed.map_err(|e| PyValueError::new_err(format!("invalid mnemonic: {e}")))?;
        let account = zip32::AccountId::try_from(account)
            .map_err(|_| PyValueError::new_err("account index out of range"))?;
        let mut seed = mnemonic.to_seed("");
        let derived = SeedFingerprint::from_seed(&seed).zip(UnifiedSpendingKey::from_seed(&network, &seed, account).ok());
        seed.zeroize();
        let (seed_fingerprint, usk) =
            derived.ok_or_else(|| WalletError::new_err("cannot derive keys from this mnemonic"))?;
        Ok(ColdSigner { network, usk, seed_fingerprint, account })
    }

    /// What a hot wallet needs to watch this account and build transactions for it: the
    /// Unified Full Viewing Key, the seed fingerprint and the account index. None of it can
    /// spend. The viewing key does reveal the account's history, so treat it as confidential.
    fn watch_bundle(&self) -> PyResult<HashMap<&'static str, String>> {
        let ufvk = self
            .usk
            .to_unified_full_viewing_key()
            .encode(&self.network)
            .map_err(|e| WalletError::new_err(format!("cannot encode viewing key: {e:?}")))?;
        Ok(HashMap::from([
            ("network", self.network.name().to_string()),
            ("ufvk", ufvk),
            ("seed_fingerprint", hex(&self.seed_fingerprint.to_bytes())),
            ("account", u32::from(self.account).to_string()),
        ]))
    }

    /// Describes what signing this PCZT would do, without signing it.
    fn inspect(&self, py: Python<'_>, pczt: &[u8]) -> PyResult<Summary> {
        let pczt = Pczt::parse(pczt).map_err(|e| PyValueError::new_err(format!("not a valid PCZT: {e:?}")))?;
        let (_, plan) = self.plan(pczt)?;
        summarise(py, plan)
    }

    /// Signs every spend in the PCZT that belongs to this signer's seed and returns the
    /// signed PCZT. Refuses a PCZT that spends nothing of ours.
    fn sign(&self, pczt: &[u8]) -> PyResult<Vec<u8>> {
        let pczt = Pczt::parse(pczt).map_err(|e| PyValueError::new_err(format!("not a valid PCZT: {e:?}")))?;
        let (pczt, plan) = self.plan(pczt)?;
        if plan.orchard.is_empty() && plan.ironwood.is_empty() && plan.sapling.is_empty() {
            return Err(PrivacyError::new_err("this PCZT spends nothing that belongs to this signer"));
        }
        let mut signer = Signer::new(pczt).map_err(|e| PyValueError::new_err(format!("cannot sign this PCZT: {e:?}")))?;
        let ask = orchard::keys::SpendAuthorizingKey::from(self.usk.orchard());
        for index in plan.orchard {
            signer
                .sign_orchard(UnwrapErr(SysRng), index, &ask)
                .map_err(|e| WalletError::new_err(format!("cannot sign Orchard spend {index}: {e:?}")))?;
        }
        for index in plan.ironwood {
            signer
                .sign_ironwood(UnwrapErr(SysRng), index, &ask)
                .map_err(|e| WalletError::new_err(format!("cannot sign Ironwood spend {index}: {e:?}")))?;
        }
        for index in plan.sapling {
            signer
                .sign_sapling(UnwrapErr(SysRng), index, self.usk.sapling().expsk().ask())
                .map_err(|e| WalletError::new_err(format!("cannot sign Sapling spend {index}: {e:?}")))?;
        }
        signer
            .finish()
            .serialize()
            .map_err(|e| WalletError::new_err(format!("cannot serialise signed PCZT: {e:?}")))
    }

    #[getter]
    fn network(&self) -> &'static str {
        self.network.name()
    }

    fn __repr__(&self) -> String {
        format!("ColdSigner(network={:?}, account={})", self.network.name(), u32::from(self.account))
    }
}

fn summarise(py: Python<'_>, plan: Plan) -> PyResult<Summary> {
    let total_out: u64 = plan.outputs.iter().map(|o| o.1).sum();
    let paying_out = plan.outputs.iter().filter(|o| !o.3).map(|o| o.1).sum();
    let outputs = plan
        .outputs
        .into_iter()
        .map(|(pool, value, label, is_change)| Py::new(py, SignedOutput { pool, value, label, is_change }))
        .collect::<PyResult<_>>()?;
    Ok(Summary {
        spending: plan.spending,
        outputs,
        paying_out,
        fee: plan.spending.saturating_sub(total_out),
        foreign_spends: plan.foreign,
    })
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// ZIP 32 derivation info, which the Orchard and Sapling PCZT types each define.
mod pczt_derivation {
    use zcash_protocol::consensus::{NetworkConstants, Parameters};
    use zip32::fingerprint::SeedFingerprint;

    pub enum Any<'a> {
        Orchard(&'a orchard::pczt::Zip32Derivation),
        Sapling(&'a sapling::pczt::Zip32Derivation),
    }

    impl Any<'_> {
        pub fn account_index<P: Parameters>(&self, seed: &SeedFingerprint, params: &P) -> Option<zip32::AccountId> {
            let coin = zip32::ChildIndex::hardened(params.network_type().coin_type());
            match self {
                Any::Orchard(d) => d.extract_account_index(seed, coin),
                Any::Sapling(d) => d.extract_account_index(seed, coin),
            }
        }
    }
}
