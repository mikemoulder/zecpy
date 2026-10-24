//! Shielded sends: propose, prove and sign locally, then broadcast.

use std::num::NonZeroUsize;
use std::str::FromStr;
use std::sync::OnceLock;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rand::{rand_core::UnwrapErr, rngs::SysRng};
use zcash_address::ZcashAddress;
use zcash_client_backend::data_api::wallet::input_selection::{GreedyInputSelector, GreedyInputSelectorError, SpendPolicy};
use zcash_client_backend::data_api::wallet::{
    ConfirmationsPolicy, SpendingKeys, create_proposed_transactions, propose_transfer,
};
use zcash_client_backend::data_api::WalletRead;
use zcash_client_backend::fees::zip317::MultiOutputChangeStrategy;
use zcash_primitives::transaction::fees::zip317::FeeError;
use zcash_client_backend::fees::{DustOutputPolicy, SplitPolicy, StandardFeeRule};
use zcash_client_backend::proto::service::RawTransaction;
use zcash_client_backend::wallet::OvkPolicy;
use zcash_client_sqlite::util::SystemClock;
use zcash_client_sqlite::wallet::commitment_tree;
use zcash_keys::address::Address;
use zcash_keys::keys::UnifiedSpendingKey;
use zcash_proofs::prover::LocalTxProver;
use zcash_protocol::memo::{Memo, MemoBytes};
use zcash_protocol::ShieldedPool;
use zcash_protocol::value::Zatoshis;
use zip321::{Payment, TransactionRequest};

use crate::error::{self, NetworkError, PrivacyError, WalletError};
use crate::net::Client;
use crate::network::Network;
use crate::wallet::Db;

/// Change is split into up to this many notes, so a busy wallet (payroll, refunds) can make
/// several payments in a row without waiting for its change to confirm.
const TARGET_NOTE_COUNT: usize = 4;
/// The smallest change note worth splitting off: 0.1 ZEC.
const MIN_SPLIT_OUTPUT_ZAT: u64 = 10_000_000;

/// Loading the bundled Sapling parameters costs about a second, so it happens once.
fn prover() -> &'static LocalTxProver {
    static PROVER: OnceLock<LocalTxProver> = OnceLock::new();
    PROVER.get_or_init(LocalTxProver::bundled)
}

/// A validated payment: the recipient is on this wallet's network, and is shielded unless
/// the caller explicitly allowed a transparent one.
pub struct SendRequest {
    request: TransactionRequest,
}

impl SendRequest {
    pub fn new(
        network: &Network,
        to: &str,
        amount_zat: u64,
        memo: Option<&str>,
        allow_transparent: bool,
    ) -> PyResult<Self> {
        let recipient = Address::decode(network, to).ok_or_else(|| {
            PyValueError::new_err(format!(
                "{to:?} is not a valid {} address",
                network.name()
            ))
        })?;
        let recipient: ZcashAddress = recipient.to_zcash_address(network);
        if recipient.is_transparent_only() && !allow_transparent {
            return Err(PrivacyError::new_err(
                "refusing to pay a transparent-only address: the amount and recipient would be \
                 public on chain. Pass allow_transparent=True to send anyway",
            ));
        }
        let amount = Zatoshis::from_u64(amount_zat)
            .ok()
            .filter(|z| !z.is_zero())
            .ok_or_else(|| PyValueError::new_err("amount must be between 1 zatoshi and 21M ZEC"))?;
        let memo = memo
            .map(|text| {
                Memo::from_str(text)
                    .map(MemoBytes::from)
                    .map_err(|_| PyValueError::new_err("memo is longer than 512 bytes"))
            })
            .transpose()?;
        if memo.is_some() && !recipient.can_receive_memo() {
            return Err(PyValueError::new_err("this recipient cannot receive a memo"));
        }
        let payment = Payment::new(recipient, Some(amount), memo, None, None, vec![])
            .map_err(|e| PyValueError::new_err(format!("invalid payment: {e:?}")))?;
        let request = TransactionRequest::new(vec![payment])
            .map_err(|e| PyValueError::new_err(format!("invalid payment request: {e:?}")))?;
        Ok(SendRequest { request })
    }
}

/// Builds, proves and signs the transaction, storing it in the wallet. Returns the raw
/// transaction ready to broadcast. CPU-bound: call from a blocking context.
pub fn build(
    db: &mut Db,
    network: &Network,
    usk: &UnifiedSpendingKey,
    send: SendRequest,
) -> PyResult<(String, RawTransaction)> {
    let account = crate::wallet::first_account(db)?;
    let change_strategy = MultiOutputChangeStrategy::new(
        StandardFeeRule::Zip317,
        None,
        ShieldedPool::Orchard,
        DustOutputPolicy::default(),
        SplitPolicy::with_min_output_value(
            NonZeroUsize::new(TARGET_NOTE_COUNT).expect("nonzero"),
            Zatoshis::const_from_u64(MIN_SPLIT_OUTPUT_ZAT),
        ),
    );
    let proposal = propose_transfer::<_, _, _, _, commitment_tree::Error>(
        db,
        network,
        account,
        &GreedyInputSelector::new(),
        &change_strategy,
        send.request,
        ConfirmationsPolicy::default(),
        &SpendPolicy::default(),
        None,
        None,
    )
    .map_err(|e| WalletError::new_err(format!("cannot fund this payment: {e}")))?;

    let txids = create_proposed_transactions::<_, _, GreedyInputSelectorError, _, FeeError, _>(
        db,
        network,
        &SystemClock,
        &mut UnwrapErr(SysRng),
        prover(),
        prover(),
        &SpendingKeys::from_unified_spending_key(usk.clone()),
        OvkPolicy::Sender,
        &proposal,
        None,
    )
    .map_err(|e| WalletError::new_err(format!("cannot build transaction: {e}")))?;
    if txids.len() > 1 {
        return Err(WalletError::new_err(
            "this payment needs more than one transaction, which ZecPy does not send yet",
        ));
    }

    let tx = db
        .get_transaction(*txids.first())
        .map_err(error::wallet("cannot load the new transaction"))?
        .ok_or_else(|| WalletError::new_err("new transaction missing from wallet"))?;
    let mut raw = RawTransaction::default();
    tx.write(&mut raw.data)
        .map_err(error::wallet("cannot serialise transaction"))?;
    Ok((tx.txid().to_string(), raw))
}

/// A uniformly random delay in `[0, max_seconds)`, from the system CSPRNG.
pub fn random_delay(max_seconds: f64) -> std::time::Duration {
    use rand::Rng;
    let unit = (UnwrapErr(SysRng).next_u64() >> 11) as f64 / (1u64 << 53) as f64;
    std::time::Duration::from_secs_f64(unit * max_seconds)
}

pub async fn broadcast(client: &mut Client, raw: RawTransaction) -> PyResult<()> {
    let response = client
        .send_transaction(raw)
        .await
        .map_err(error::network("SendTransaction failed"))?
        .into_inner();
    if response.error_code != 0 {
        return Err(NetworkError::new_err(format!(
            "server rejected the transaction ({}): {}",
            response.error_code, response.error_message
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::random_delay;

    #[test]
    fn broadcast_delay_stays_in_range_and_varies() {
        let delays: Vec<f64> = (0..200).map(|_| random_delay(30.0).as_secs_f64()).collect();
        assert!(delays.iter().all(|d| (0.0..30.0).contains(d)));
        let spread = delays.iter().cloned().fold(f64::MIN, f64::max) - delays.iter().cloned().fold(f64::MAX, f64::min);
        assert!(spread > 15.0, "200 draws should cover most of the range, got spread {spread}");
    }
}
