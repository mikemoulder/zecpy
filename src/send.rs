//! Shielded sends: propose, prove and sign locally, then broadcast.

use std::num::NonZeroUsize;
use std::str::FromStr;
use std::sync::{Arc, Mutex, OnceLock};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rand::{rand_core::UnwrapErr, rngs::SysRng};
use zcash_address::ZcashAddress;
use zcash_client_backend::data_api::MaxSpendMode;
use zcash_client_backend::data_api::wallet::input_selection::{
    GreedyInputSelector, GreedyInputSelectorError, LockedInputPolicy, SpendPolicy,
};
use zcash_client_backend::data_api::wallet::{
    ConfirmationsPolicy, SpendingKeys, create_pczt_from_proposal, create_proposed_transactions,
    extract_and_store_transaction_from_pczt, propose_send_max_transfer, propose_transfer,
};
use zcash_client_backend::proposal::Proposal;
use pczt::Pczt;
use pczt::roles::prover::Prover;
use zcash_primitives::transaction::TxId;
use zcash_primitives::transaction::builder::BundlePadding;
use zcash_primitives::transaction::components::orchard::bundle_version_for_branch;
use zcash_protocol::consensus::BranchId;
use zcash_client_sqlite::{AccountUuid, ReceivedNoteId};
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
use zcash_protocol::consensus::BlockHeight;
use zcash_protocol::zip318::{BlockSpacing, PoolMigrationConstants};
use zcash_protocol::{PoolType, ShieldedPool};
use zcash_client_backend::fees::canonical_crossing_fee;
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
    /// Whether a transaction may move value between shielded pools in a way that makes the
    /// crossing amount public (see `Crossing`).
    pub allow_pool_crossing: bool,
}

/// Most recipients a single ZecPy transaction pays. Bounds proving time and memory; larger
/// payrolls go out in several transactions.
pub const MAX_RECIPIENTS: usize = 50;

impl SendRequest {
    pub fn new(
        network: &Network,
        to: &str,
        amount_zat: u64,
        memo: Option<&str>,
        allow_transparent: bool,
    ) -> PyResult<Self> {
        Self::many(network, &[(to.to_string(), amount_zat, memo.map(str::to_string))], allow_transparent)
    }

    /// One transaction paying several recipients. Every payment is validated before anything
    /// is built: one bad entry rejects the whole batch, naming its position.
    pub fn many(
        network: &Network,
        payments: &[(String, u64, Option<String>)],
        allow_transparent: bool,
    ) -> PyResult<Self> {
        if payments.is_empty() || payments.len() > MAX_RECIPIENTS {
            return Err(PyValueError::new_err(format!(
                "a transaction pays between 1 and {MAX_RECIPIENTS} recipients"
            )));
        }
        let payments = payments
            .iter()
            .enumerate()
            .map(|(i, (to, amount, memo))| {
                payment(network, to, *amount, memo.as_deref(), allow_transparent).map_err(|e| {
                    if payments.len() == 1 {
                        e
                    } else {
                        Python::attach(|py| {
                            let message = e.value(py).to_string();
                            PyErr::from_type(e.get_type(py), format!("payment {i}: {message}"))
                        })
                    }
                })
            })
            .collect::<PyResult<Vec<_>>>()?;
        let request = TransactionRequest::new(payments)
            .map_err(|e| PyValueError::new_err(format!("invalid payment request: {e:?}")))?;
        Ok(SendRequest { request, allow_pool_crossing: false })
    }
}

/// A recipient on this wallet's network, refused if transparent-only unless allowed.
fn checked_recipient(network: &Network, to: &str, allow_transparent: bool) -> PyResult<ZcashAddress> {
    let recipient = Address::decode(network, to).ok_or_else(|| {
        PyValueError::new_err(format!("{to:?} is not a valid {} address", network.name()))
    })?;
    let recipient: ZcashAddress = recipient.to_zcash_address(network);
    if recipient.is_transparent_only() && !allow_transparent {
        return Err(PrivacyError::new_err(
            "refusing to pay a transparent-only address: the amount and recipient would be \
             public on chain. Pass allow_transparent=True to send anyway",
        ));
    }
    Ok(recipient)
}

impl SendRequest {
    /// A ZIP 321 payment request, validated like any other payment, and refused whole if it
    /// asks for more than `max_zat` in total. A request without an amount is refused: an
    /// agent paying links must never choose the amount itself.
    pub fn from_uri(network: &Network, uri: &str, allow_transparent: bool, max_zat: u64) -> PyResult<Self> {
        let request = TransactionRequest::from_uri(uri)
            .map_err(|e| PyValueError::new_err(format!("not a valid ZIP 321 payment request: {e}")))?;
        let payments = request.payments();
        if payments.is_empty() || payments.len() > MAX_RECIPIENTS {
            return Err(PyValueError::new_err(format!(
                "a payment request pays between 1 and {MAX_RECIPIENTS} recipients"
            )));
        }
        let mut total = 0u64;
        for (i, p) in payments {
            checked_recipient(network, &p.recipient_address().encode(), allow_transparent)
                .map_err(|e| Python::attach(|py| PyErr::from_type(e.get_type(py), format!("payment {i}: {}", e.value(py)))))?;
            let amount = p
                .amount()
                .filter(|a| !a.is_zero())
                .ok_or_else(|| PyValueError::new_err(format!("payment {i}: the request names no amount")))?;
            total = total.saturating_add(u64::from(amount));
        }
        if total > max_zat {
            return Err(PrivacyError::new_err(format!(
                "the request asks for {total} zatoshis, more than max_zat={max_zat}; nothing was built"
            )));
        }
        Ok(SendRequest { request, allow_pool_crossing: false })
    }
}

/// A parsed ZIP 321 request, shown before paying it. Labels and messages come from whoever
/// made the link: display them, never act on them.
#[pyclass(module = "zecpy._native", frozen, get_all)]
pub struct PaymentRequest {
    /// (address, amount_zat, label, message, has_memo) per payment, in index order.
    pub payments: Vec<(String, u64, Option<String>, Option<String>, bool)>,
    pub total: u64,
}

#[pymethods]
impl PaymentRequest {
    fn __repr__(&self) -> String {
        format!("PaymentRequest(payments={}, total={})", self.payments.len(), self.total)
    }
}

impl PaymentRequest {
    pub fn of(network: &Network, uri: &str) -> PyResult<Self> {
        let send = SendRequest::from_uri(network, uri, true, u64::MAX)?;
        let payments: Vec<_> = send
            .request
            .payments()
            .values()
            .map(|p| {
                (
                    p.recipient_address().encode(),
                    p.amount().map(u64::from).unwrap_or(0),
                    p.label().cloned(),
                    p.message().cloned(),
                    p.memo().is_some(),
                )
            })
            .collect();
        Ok(PaymentRequest { total: payments.iter().map(|p| p.1).sum(), payments })
    }
}

fn payment(
    network: &Network,
    to: &str,
    amount_zat: u64,
    memo: Option<&str>,
    allow_transparent: bool,
) -> PyResult<Payment> {
    let recipient = checked_recipient(network, to, allow_transparent)?;
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
    Payment::new(recipient, Some(amount), memo, None, None, vec![])
        .map_err(|e| PyValueError::new_err(format!("invalid payment: {e:?}")))
}

type WalletProposal = Proposal<StandardFeeRule, ReceivedNoteId>;

/// Selects notes and computes change for a payment. Shared by direct sends and cold-signed
/// ones, so both follow the same note-splitting and confirmation rules.
fn propose(db: &mut Db, network: &Network, send: SendRequest) -> PyResult<(AccountUuid, WalletProposal)> {
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
    if proposal.steps().len() > 1 {
        return Err(WalletError::new_err(
            "this payment needs more than one transaction, which ZecPy does not send yet",
        ));
    }
    refuse_public_crossing(network, &proposal, send.allow_pool_crossing)?;
    Ok((account, proposal))
}

fn refuse_public_crossing(network: &Network, proposal: &WalletProposal, allowed: bool) -> PyResult<()> {
    let crossing = Crossing::of(network, proposal)?;
    if crossing.value > 0 && !crossing.canonical && !allowed {
        return Err(PrivacyError::new_err(format!(
            "this payment would move {} zatoshis between shielded pools, and a crossing \
             amount is public on chain. Pass allow_pool_crossing=True to send anyway",
            crossing.value
        )));
    }
    Ok(())
}

/// Plans a payment of everything spendable in the Orchard and Ironwood pools to `to`, the
/// fee taken from it. With `everything`, refuses instead of leaving behind notes that are
/// not spendable yet. Sapling notes, which ZecPy only receives, are left alone. Local only.
#[allow(clippy::too_many_arguments)]
pub fn plan_sweep(
    db: &mut Db,
    network: &Network,
    wallet: &str,
    to: &str,
    memo: Option<&str>,
    everything: bool,
    allow_transparent: bool,
    allow_pool_crossing: bool,
) -> PyResult<PyProposal> {
    let recipient = checked_recipient(network, to, allow_transparent)?;
    let memo = memo
        .map(|text| {
            Memo::from_str(text)
                .map(MemoBytes::from)
                .map_err(|_| PyValueError::new_err("memo is longer than 512 bytes"))
        })
        .transpose()?;
    let account = crate::wallet::first_account(db)?;
    let mode = if everything { MaxSpendMode::Everything } else { MaxSpendMode::MaxSpendable };
    let proposal = propose_send_max_transfer::<_, _, _, commitment_tree::Error>(
        db,
        network,
        account,
        &[ShieldedPool::Orchard, ShieldedPool::Ironwood],
        &StandardFeeRule::Zip317,
        recipient,
        memo,
        mode,
        ConfirmationsPolicy::default(),
        &LockedInputPolicy::Exclude,
        None,
    )
    .map_err(|e| WalletError::new_err(format!("cannot sweep this wallet: {e}")))?;
    refuse_public_crossing(network, &proposal, allow_pool_crossing)?;
    PyProposal::new(network, wallet, proposal)
}

fn pool_name(pool: PoolType) -> &'static str {
    match pool {
        PoolType::Transparent => "transparent",
        PoolType::Shielded(ShieldedPool::Sapling) => "sapling",
        PoolType::Shielded(ShieldedPool::Orchard) => "orchard",
        PoolType::Shielded(ShieldedPool::Ironwood) => "ironwood",
    }
}

/// ZIP 318 parameters for this chain: only the block spacing differs between networks.
struct Migration(BlockSpacing);

impl PoolMigrationConstants for Migration {
    fn block_spacing(&self) -> BlockSpacing {
        self.0
    }
}

/// Value moving between shielded pools. Each shielded bundle's net value balance is public,
/// so whatever enters a pool the transaction did not spend from is visible on chain: a
/// payment to an Ironwood receiver funded by Orchard notes publishes its amount. ZIP 318
/// migration transfers are the exception that is safe by construction: one canonical
/// denomination, a fixed shape and fee, so each looks like every other.
struct Crossing {
    value: u64,
    canonical: bool,
}

/// Value that enters each shielded pool beyond what was spent from it, summed.
fn entering_value(inputs: &[(PoolType, u64)], outputs: &[(PoolType, u64)]) -> u64 {
    let sum = |list: &[(PoolType, u64)], pool: PoolType| -> u64 {
        list.iter().filter(|(p, _)| *p == pool).map(|(_, v)| v).sum()
    };
    [ShieldedPool::Sapling, ShieldedPool::Orchard, ShieldedPool::Ironwood]
        .into_iter()
        .map(PoolType::Shielded)
        .map(|pool| sum(outputs, pool).saturating_sub(sum(inputs, pool)))
        .sum()
}

/// Inputs and outputs of a single-step proposal, by pool: (inputs, payments, change).
type Flows = (Vec<(PoolType, u64)>, Vec<(PoolType, u64)>, Vec<(PoolType, u64)>);

fn flows(proposal: &WalletProposal) -> Flows {
    let step = proposal.steps().first();
    let inputs = step
        .shielded_inputs()
        .map(|i| i.notes().iter().map(|n| (PoolType::Shielded(n.note().pool()), u64::from(n.note().value()))).collect())
        .unwrap_or_default();
    let payments = step
        .payment_pools()
        .iter()
        .filter_map(|(i, pool)| {
            step.transaction_request().payments().get(i).and_then(|p| p.amount()).map(|a| (*pool, u64::from(a)))
        })
        .collect();
    let change = step
        .balance()
        .proposed_change()
        .iter()
        .map(|c| (c.output_pool(), u64::from(c.value())))
        .collect();
    (inputs, payments, change)
}

impl Crossing {
    fn of(network: &Network, proposal: &WalletProposal) -> PyResult<Self> {
        let (inputs, payments, change) = flows(proposal);
        let outputs: Vec<_> = payments.into_iter().chain(change).collect();
        let value = entering_value(&inputs, &outputs);
        let canonical = value > 0 && {
            let target = BlockHeight::from(proposal.min_target_height());
            let fee = canonical_crossing_fee(network, target)
                .map_err(|e| WalletError::new_err(format!("cannot compute the ZIP 318 fee: {e}")))?;
            proposal.steps().first().is_canonical_crossing(&Migration(BlockSpacing::for_network(network)), fee)
        };
        Ok(Crossing { value, canonical })
    }
}

/// A payment planned but not yet built: what it spends, pays and costs. Sending it with
/// `send_proposal` builds exactly this transaction, or fails if the wallet changed.
#[pyclass(module = "zecpy._native", name = "Proposal", frozen)]
pub struct PyProposal {
    proposal: WalletProposal,
    /// The wallet database this proposal was made from; it cannot be sent from another.
    wallet: String,
    #[pyo3(get)]
    fee: u64,
    /// Total paid to recipients, fee and change excluded.
    #[pyo3(get)]
    paying_out: u64,
    /// (pool, value) of each payment, in request order.
    #[pyo3(get)]
    payments: Vec<(&'static str, u64)>,
    /// (pool, value) of each note spent.
    #[pyo3(get)]
    spends: Vec<(&'static str, u64)>,
    /// (pool, value) of each change note.
    #[pyo3(get)]
    change: Vec<(&'static str, u64)>,
    /// Value that crosses between shielded pools and is therefore public; 0 if none.
    #[pyo3(get)]
    crossing_value: u64,
    /// The crossing is a ZIP 318 canonical migration transfer, which hides in its set.
    #[pyo3(get)]
    canonical_crossing: bool,
    /// The proposal is valid from this height.
    #[pyo3(get)]
    target_height: u32,
}

#[pymethods]
impl PyProposal {
    fn __repr__(&self) -> String {
        format!(
            "Proposal(paying_out={}, fee={}, payments={}, spends={}, change={}, crossing_value={})",
            self.paying_out,
            self.fee,
            self.payments.len(),
            self.spends.len(),
            self.change.len(),
            self.crossing_value
        )
    }
}

impl PyProposal {
    fn new(network: &Network, wallet: &str, proposal: WalletProposal) -> PyResult<Self> {
        let named = |list: Vec<(PoolType, u64)>| list.into_iter().map(|(p, v)| (pool_name(p), v)).collect::<Vec<_>>();
        let (inputs, payments, change) = flows(&proposal);
        let crossing = Crossing::of(network, &proposal)?;
        Ok(PyProposal {
            fee: u64::from(proposal.steps().first().balance().fee_required()),
            paying_out: payments.iter().map(|(_, v)| v).sum(),
            payments: named(payments),
            spends: named(inputs),
            change: named(change),
            crossing_value: crossing.value,
            canonical_crossing: crossing.canonical,
            target_height: u32::from(BlockHeight::from(proposal.min_target_height())),
            wallet: wallet.to_string(),
            proposal,
        })
    }
}

/// Plans a payment without building it. Local only.
pub fn plan(db: &mut Db, network: &Network, wallet: &str, send: SendRequest) -> PyResult<PyProposal> {
    let (_, proposal) = propose(db, network, send)?;
    PyProposal::new(network, wallet, proposal)
}

/// Refuses a proposal whose notes were spent, or rolled back by a reorg, since it was made.
fn check_fresh(db_path: &str, proposal: &WalletProposal) -> PyResult<()> {
    let conn = rusqlite::Connection::open_with_flags(db_path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(error::wallet("cannot open wallet database read-only"))?;
    let tip: Option<u32> = conn
        .query_row("SELECT MAX(height) FROM blocks", [], |row| row.get(0))
        .map_err(error::wallet("cannot read scanned height"))?;
    let mut stmt = conn
        .prepare(
            "SELECT
                EXISTS (SELECT 1 FROM v_received_outputs ro JOIN transactions t ON t.id_tx = ro.transaction_id
                        WHERE t.txid = :txid AND ro.pool = :pool AND ro.output_index = :idx),
                EXISTS (SELECT 1 FROM v_received_outputs ro
                        JOIN transactions t ON t.id_tx = ro.transaction_id
                        JOIN v_received_output_spends s
                             ON s.pool = ro.pool AND s.received_output_id = ro.id_within_pool_table
                        JOIN transactions st ON st.id_tx = s.transaction_id
                        WHERE t.txid = :txid AND ro.pool = :pool AND ro.output_index = :idx
                          AND (st.mined_height IS NOT NULL OR st.expiry_height = 0 OR st.expiry_height > :tip))",
        )
        .map_err(error::wallet("cannot check proposal inputs"))?;
    let Some(inputs) = proposal.steps().first().shielded_inputs() else { return Ok(()) };
    for note in inputs.notes() {
        let pool: i64 = match note.note().pool() {
            ShieldedPool::Sapling => 2,
            ShieldedPool::Orchard => 3,
            ShieldedPool::Ironwood => 4,
        };
        let (exists, spent): (bool, bool) = stmt
            .query_row(
                rusqlite::named_params! {
                    ":txid": note.txid().as_ref(),
                    ":pool": pool,
                    ":idx": note.output_index(),
                    ":tip": tip.unwrap_or(0),
                },
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .map_err(error::wallet("cannot check proposal inputs"))?;
        if !exists || spent {
            return Err(WalletError::new_err(
                "the wallet changed since this proposal was made (a note it spends is gone or \
                 already spent); propose the payment again",
            ));
        }
    }
    Ok(())
}

fn raw_transaction(db: &Db, txid: TxId) -> PyResult<(String, RawTransaction)> {
    let tx = db
        .get_transaction(txid)
        .map_err(error::wallet("cannot load the new transaction"))?
        .ok_or_else(|| WalletError::new_err("new transaction missing from wallet"))?;
    let mut raw = RawTransaction::default();
    tx.write(&mut raw.data)
        .map_err(error::wallet("cannot serialise transaction"))?;
    Ok((tx.txid().to_string(), raw))
}

/// Builds, proves and signs the transaction, storing it in the wallet. Returns the raw
/// transaction ready to broadcast. CPU-bound: call from a blocking context.
pub fn build(
    db: &mut Db,
    network: &Network,
    usk: &UnifiedSpendingKey,
    send: SendRequest,
) -> PyResult<(String, RawTransaction)> {
    let (_, proposal) = propose(db, network, send)?;
    build_proposal(db, network, usk, &proposal)
}

/// Builds a planned payment exactly as proposed, after checking it is still fundable.
pub fn build_planned(
    db: &mut Db,
    network: &Network,
    usk: &UnifiedSpendingKey,
    db_path: &str,
    planned: &PyProposal,
) -> PyResult<(String, RawTransaction)> {
    if planned.wallet != db_path {
        return Err(PyValueError::new_err("this proposal was made by a different wallet"));
    }
    check_fresh(db_path, &planned.proposal)?;
    build_proposal(db, network, usk, &planned.proposal)
}

fn build_proposal(
    db: &mut Db,
    network: &Network,
    usk: &UnifiedSpendingKey,
    proposal: &WalletProposal,
) -> PyResult<(String, RawTransaction)> {
    let txids = create_proposed_transactions::<_, _, GreedyInputSelectorError, _, FeeError, _>(
        db,
        network,
        &SystemClock,
        &mut UnwrapErr(SysRng),
        prover(),
        prover(),
        &SpendingKeys::from_unified_spending_key(usk.clone()),
        OvkPolicy::Sender,
        proposal,
        None,
    )
    .map_err(|e| WalletError::new_err(format!("cannot build transaction: {e}")))?;
    raw_transaction(db, *txids.first())
}

/// Builds an unsigned, proven PCZT for a cold signer. Needs no spending key: Orchard and
/// Ironwood proofs only use public data. Sapling spends would need a proof key derived from
/// the seed, so they are refused. CPU-bound: call from a blocking context.
pub fn build_pczt(db: &mut Db, network: &Network, send: SendRequest) -> PyResult<Vec<u8>> {
    let (account, proposal) = propose(db, network, send)?;
    let pczt = create_pczt_from_proposal::<_, _, GreedyInputSelectorError, _, FeeError, _>(
        db,
        network,
        &SystemClock,
        &mut UnwrapErr(SysRng),
        account,
        OvkPolicy::Sender,
        &proposal,
        None,
        BundlePadding::DEFAULT,
    )
    .map_err(|e| WalletError::new_err(format!("cannot build unsigned transaction: {e}")))?;

    let branch_id = BranchId::try_from(*pczt.global().consensus_branch_id())
        .map_err(|_| WalletError::new_err("PCZT has an unknown consensus branch"))?;
    let mut prover_role = Prover::new(pczt);
    if prover_role.requires_sapling_proofs() {
        return Err(WalletError::new_err(
            "this payment would spend Sapling notes, which a cold signer must prove itself; \
             ZecPy cold signing spends Orchard and Ironwood notes only",
        ));
    }
    if prover_role.requires_orchard_proof() || prover_role.requires_ironwood_proof() {
        let pk = orchard_proving_key(branch_id)?;
        if prover_role.requires_orchard_proof() {
            prover_role = prover_role
                .create_orchard_proof(UnwrapErr(SysRng), &pk)
                .map_err(|e| WalletError::new_err(format!("Orchard proof failed: {e:?}")))?;
        }
        if prover_role.requires_ironwood_proof() {
            prover_role = prover_role
                .create_ironwood_proof(UnwrapErr(SysRng), &pk)
                .map_err(|e| WalletError::new_err(format!("Ironwood proof failed: {e:?}")))?;
        }
    }
    prover_role
        .finish()
        .serialize()
        .map_err(|e| WalletError::new_err(format!("cannot serialise PCZT: {e:?}")))
}

/// The Orchard proving key for the circuit a branch uses (Orchard and Ironwood share it).
/// Building one takes seconds, so each circuit version is built once per process.
fn orchard_proving_key(branch_id: BranchId) -> PyResult<Arc<orchard::circuit::ProvingKey>> {
    static KEYS: OnceLock<Mutex<Vec<(BranchId, Arc<orchard::circuit::ProvingKey>)>>> = OnceLock::new();
    let version = bundle_version_for_branch(branch_id, orchard::ValuePool::Orchard)
        .ok_or_else(|| WalletError::new_err(format!("branch {branch_id:?} has no Orchard")))?
        .circuit_version();
    let mut keys = KEYS.get_or_init(Default::default).lock().unwrap();
    if let Some((_, pk)) = keys.iter().find(|(b, _)| *b == branch_id) {
        return Ok(Arc::clone(pk));
    }
    let pk = Arc::new(orchard::circuit::ProvingKey::build(version));
    keys.push((branch_id, Arc::clone(&pk)));
    Ok(pk)
}

/// Checks a signed PCZT, extracts the transaction and stores it in the wallet. Returns it
/// ready to broadcast.
pub fn extract_signed(db: &mut Db, signed: &[u8]) -> PyResult<(String, RawTransaction)> {
    let pczt = Pczt::parse(signed)
        .map_err(|e| PyValueError::new_err(format!("not a valid PCZT: {e:?}")))?;
    let (spend_vk, output_vk) = prover().verifying_keys();
    let txid = extract_and_store_transaction_from_pczt::<_, ReceivedNoteId>(
        db,
        &SystemClock,
        &mut UnwrapErr(SysRng),
        pczt,
        Some((&spend_vk, &output_vk)),
        None,
    )
    .map_err(|e| WalletError::new_err(format!("signed PCZT is incomplete or invalid: {e:?}")))?;
    raw_transaction(db, txid)
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
    use super::{entering_value, random_delay};
    use zcash_protocol::{PoolType, ShieldedPool};

    const ORCHARD: PoolType = PoolType::Shielded(ShieldedPool::Orchard);
    const IRONWOOD: PoolType = PoolType::Shielded(ShieldedPool::Ironwood);
    const SAPLING: PoolType = PoolType::Shielded(ShieldedPool::Sapling);

    #[test]
    fn same_pool_payment_crosses_nothing() {
        // 1.0 in, 0.3 paid, 0.69 change, 0.01 fee: all Ironwood.
        let inputs = [(IRONWOOD, 100_000_000)];
        let outputs = [(IRONWOOD, 30_000_000), (IRONWOOD, 69_000_000)];
        assert_eq!(entering_value(&inputs, &outputs), 0);
    }

    #[test]
    fn orchard_funds_paying_ironwood_publish_the_amount() {
        // An Orchard note pays an Ironwood receiver, change stays in Orchard: the payment
        // enters Ironwood with nothing spent from it, so its value balance shows it.
        let inputs = [(ORCHARD, 100_000_000)];
        let outputs = [(IRONWOOD, 30_000_000), (ORCHARD, 69_990_000)];
        assert_eq!(entering_value(&inputs, &outputs), 30_000_000);
    }

    #[test]
    fn mixed_inputs_count_only_the_excess_entering_each_pool() {
        let inputs = [(ORCHARD, 50_000), (IRONWOOD, 20_000), (SAPLING, 10_000)];
        let outputs = [(IRONWOOD, 60_000), (ORCHARD, 15_000)];
        // Ironwood gains 40,000 over its inputs; Orchard and Sapling only lose value.
        assert_eq!(entering_value(&inputs, &outputs), 40_000);
    }

    #[test]
    fn transparent_outputs_are_not_counted_as_crossing() {
        let inputs = [(IRONWOOD, 100_000)];
        let outputs = [(PoolType::Transparent, 50_000), (IRONWOOD, 40_000)];
        assert_eq!(entering_value(&inputs, &outputs), 0);
    }

    #[test]
    fn broadcast_delay_stays_in_range_and_varies() {
        let delays: Vec<f64> = (0..200).map(|_| random_delay(30.0).as_secs_f64()).collect();
        assert!(delays.iter().all(|d| (0.0..30.0).contains(d)));
        let spread = delays.iter().cloned().fold(f64::MIN, f64::max) - delays.iter().cloned().fold(f64::MAX, f64::min);
        assert!(spread > 15.0, "200 draws should cover most of the range, got spread {spread}");
    }
}
