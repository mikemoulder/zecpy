# zcash-devtool: `pczt sign` leaves Ironwood spends unsigned and exits successfully

Prepared for zcash/zcash-devtool. Not yet filed (to be filed when ZecPy is published).
Against `0b3436b` (2026-10-17).

## What happens

`pczt sign` collects the spends to sign with `Verifier::with_orchard`, `with_sapling` and
`with_transparent`, and signs them with `sign_orchard`, `sign_sapling` and
`sign_transparent`. It never visits the Ironwood bundle, so Ironwood spends stay unsigned.
It still exits 0. The failure only appears at `pczt send`.

Since NU6.3, received funds land in the Ironwood pool, so this affects most cold-signing
flows on a current chain.

## Reproduction

Regtest chain with NU6.3 active from height 1 (zebrad 7.0.0, lightwalletd 0.6.0). A fresh
devtool wallet receives 0.01 REG, which lands in Ironwood, and syncs past 10 confirmations:

```
wallet balance  ->  Ironwood Spendable: 0.01000000 REG
pczt create --address <own UA> --value 100000 --output 1.pczt     exit 0
pczt prove 1.pczt --output 2.pczt                                exit 0
pczt sign -i id.age 2.pczt --output 3.pczt                       exit 0
pczt send 3.pczt                                                 exit 1
Error: Failed to extract and store transaction from PCZT:
       Pczt(Extraction(Ironwood(Extract(MissingSpendAuthSig))))
```

## Suggested fix

Add an Ironwood arm alongside Orchard: collect indices with `Verifier::with_ironwood`
(matching `zip32_derivation` against the seed fingerprint as for Orchard) and sign them with
`Signer::sign_ironwood`, which takes the same `SpendAuthorizingKey` as `sign_orchard`.
Ironwood notes use Orchard keys.

It would also help if `pczt sign` failed when the PCZT still has unsigned spends belonging
to the seed, rather than reporting success.

ZecPy's signer (`src/cold.rs`) does this. `tests/test_devnet_cold.py` covers a cold-signed
payment from Ironwood notes end to end.
