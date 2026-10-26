## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## What the light server sees

<!-- For changes to sync, send, memos, Tor or the server connection: paste the leak-audit
summary before and after, or write "no network change". -->

## Checklist

- [ ] Every request this adds or alters is `public`, `coarse` or `broadcast` in the audit, or is opt-in, documented, and goes over Tor
- [ ] No seed, key, memo or user address appears in a log, repr, exception or test failure message
- [ ] Refusals raise `PrivacyError` with the reason and the flag that overrides it
- [ ] Amounts are integer zatoshis
- [ ] Tests added; privacy behaviour asserted on the audit record
- [ ] Cryptography stays in librustzcash
- [ ] Docs updated (API reference; privacy model if a guarantee changed)
