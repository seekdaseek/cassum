# cassum — working rules

Sibyl Labs Hackathon. Submission closes 2026-09-10 23:59 UTC. Submission IS the
private build page, no separate form.

Scoring: pass/fail gate first, then rubric. Memory load-bearing 40, innovation
25, technical execution 20, pitch 15, PMF bonus +10, then a partner multiplier
up to x1.25. Gate is panel majority and a tie FAILS.

Gate requires three things: the deletion test (remove memory, core function
breaks), cold-start recall shown in the video as ONE continuous unedited
segment with an on-screen timestamp or commit hash, and README pointing at the
memory read/write call sites so a judge finds them in under two minutes.

Rubric note that shapes design: "Recall is competitive; coordination and
dynamic-storage patterns top the band."

## Hard rules

1. NEVER say something works unless it was run and the output observed. If it
   was not executed, write UNTESTED and say what would test it.
2. Every claim about `sibyl-memory-client` comes from `probes/` or a test. The
   vendor docs are wrong in seven measured places, see FINDINGS.md.
3. When a test fails, first ask whether the TEST is wrong. Four failures so far
   were all bad assumptions in tests, not bugs in the code.
4. Keep measured negatives. Do not delete
   `test_memory_is_overhead_when_no_provider_has_an_edge`.
5. Never type a figure into a doc. `tools/report.py` generates RESULTS.md from
   runs. Extend it rather than hand-writing numbers.
6. Commit per working session with a real message. Judges read history; a
   single end-of-window dump is a disqualification pattern.
7. No new runtime dependencies without asking.

## Measured surface, sibyl-memory-client 0.8.0

Verified 2026-09-01, Python 3.12.13, macOS. Raw output in `probes/`.

- `MemoryClient.local(path, *, tenant_id, tier, account_id, session_token,
  credentials_claim, credentials_signature)`
- `set_entity(category, name, body, *, status)` — parameter is `category`, NOT
  `kind` as the docs say
- `get_entity(category, name)` RAISES NotFoundError on a miss, does not return
  None. `Store.get_provider` wraps it so absence is a value.
- `write_event(*, evaluated, acted, forward, extra, ts)` — all four round-trip.
  Only `acted` is documented.
- `read_events(*, limit, since, until)` returns NEWEST FIRST
- `search(query, *, limit, prefix, tiers)` — undocumented. Tiers are SINGULAR:
  entity, state, reference, journal. Plural raises ValueError.
- `get_state` returns body as a dict; `get_reference` returns body as a JSON
  STRING. `Store.read_reference` decodes it.
- `delete_entity` returns True on a live entity, False when there is nothing to
  delete. Correct behaviour, not a bug.
- Account tier was FREE during the build, so `learn()`, `learner()`, `lint()` and the
  skill-proposal calls raise TierGateError. Do not call them.

## Measured surface, x402.ochinimus.app

Verified 2026-09-01. Challenges recorded verbatim in `tests/fixtures/`.

- unpaid GET returns 402, body `{}`, quote base64 in the `payment-required`
  HEADER, never the body. x402Version 2, one `accepts[]` entry per rail.
- Base rail `eip155:8453`, asset `0x8335..2913`, payTo `0x22DB..76e6`, `amount`
  a decimal STRING in USDC base units at 6dp. Parsed as int, never float.
- the origin is behind Cloudflare, which answers the default
  `Python-urllib/3.12` User-Agent with **403 and no challenge header** — not
  402. Send a named User-Agent or every endpoint reads as down.
- success envelope is `{tool, data, paid}`, measured on the one free endpoint
  `/api/fear-greed`. `/pricing` and the forecast free tasters are 404 on HTTP;
  they exist on the MCP rail only.
- BOTH RAILS CAN BE PAID, via two different JS payers. `plugin-agentfeed` 0.1.2
  is SVM-only (`@solana/kit`, `ExactSvmScheme`, bs58) — it cannot sign for Base.
  Base goes through `@seekdaseek/x402-wallet` at
  `/Volumes/D/skrproject/x402-wallet/x402-wallet` instead (viem signTypedData,
  `@x402/evm` ExactEvmScheme). Selected by `CASSUM_RAIL`, default `solana`.
- the CDP facilitator ACCEPTS EVM exact on Base. Proof from source, not docs:
  `@x402/core` 2.18.0 `buildPaymentRequirements()` THROWS "Facilitator does not
  support <scheme> on <network>" unless the facilitator's `/supported` lists it,
  and the live service returns a 402 whose accepts[] contains eip155:8453. Note
  `validateFacilitatorCapabilities()` does NOT enforce this at boot — it does
  `if (!supportedKind) continue`. agentfeed payments.js:102 claims boot fails
  loudly; it does not. The throw happens at requirements-building time.
- A LONG `resource.description` MAKES AN ENDPOINT UNPAYABLE. Bisected over
  seven redeploys against the live CDP facilitator: desc 487 chars (challenge
  header 2268 B) settles, 515 chars (2308 B) does not. The facilitator rejects
  the paymentPayload with HTTP 400, agentfeed relays it as a fresh 402, and the
  route's handler is never reached. Invisible from curl. FIXED AT THE BOUNDARY
  in `/opt/agentfeed/payments.js` only: `challengeDesc()` trims to 256 chars
  when building the challenge; `GET /`, `/.well-known/x402.json`, the landing
  page and the MCP tool defs still serve the full text. Backup
  `payments.js.bak-desctrunc-20260901-132211`. Two of 44 endpoints are trimmed
  today (get_cascade_forecast 683, get_liq_history 284); all 44 verified to
  still issue a valid, correctly-priced challenge on both rails.
- FIRST LIVE SETTLEMENT FROM CASSUM, 2026-09-01, verified on basescan:
  `0x1f99b069600f32d890c710b3e8894e415cbe039c6e102750c28d7c9866a580cf`
  0.001 USDC, block 50738445, `/api/sol-price`, delivered=True by the usability
  predicate. Payer `0xFEfF369D5048b2Cf817d87467E48404b3ADfE4Ee` -> treasury
  `0x22DB..76e6`; balance 0.029000 -> 0.028000, exactly the quoted amount. Gas
  paid by relayer `0x93f6..be44`, payer holds 0 ETH throughout. The settlement
  payload carries success/payer/transaction/network and NO `amountUsd`, so the
  paidUsd fallback (the header-parsed amount) is the code path that ran.
  Preceded by a dry run at CASSUM_MAX_USDC=0.0005 in which all six endpoints
  refused pre-signature, the bridge was reached 0 times, and the on-chain
  balance was unchanged.
- x402-wallet has ALREADY settled both rails on mainnet against this service:
  Solana `5XPKFW…WqM2`, Base `0xe49b8c…4a31` (0.001 USDC each). The payer holds
  zero native gas on either chain; the facilitator sponsors it.
- the payee is our own treasury on both rails (cj7 on Solana, 6e6 on Base), so
  a live run can never yield a savings number. See RESULTS.md, generated.

## State as of commit 370c162

87 tests green. Working: memory layer, router, deletion-test harness, trace
tool, generated RESULTS.md, packaging, public repo, cold-start recall demo,
x402 adapter with discovery live and settlement built but NEVER YET RUN.

Measured result, `default_fleet`, 20 payloads: 0.00555 USDC per payload with
memory against 0.01200 without. 24 calls against 80. 53.75% of spend recovered.
`flat_fleet` is the negative: no dispersion, memory costs 3.75% more.

Routing behaviour measured off `tools/trace.py`: exploration is breadth-first
because untried outranks learning, so every provider gets one call before any
gets a second. hollow-cheap is condemned after buy 5; the router settles on
steady-dear from buy 8 — but on LEARNING rank, i.e. because it is still
under-explored. steady-dear is TRUSTED, and so chosen on measured cost per
payload, from buy 9. That is where `tools/session.py --phase learn` stops.

## Layout

- `cassum/memory.py` — Store, wraps the measured surface, absence-safe reads
- `cassum/router.py` — core function: reads provider memory before spending,
  ranks TRUSTED providers by price / (1 - empty_rate), not by sticker price
- `cassum/sim.py` — deterministic fake providers, no network, no spend
- `cassum/ablate.py` — deletion test as code, NullStore vs real Store
- `tools/session.py` — the gate beat: `--phase learn` then `--phase recall`,
  two processes, one db, recall spends nothing
- `cassum/x402.py` — real paid provider, same interface as `sim.SimProvider`.
  Discovery is live and tested; `fetch()` refuses because signing is not built
- `tools/quote.py` — prices endpoints off the 402 header, `--live` gates payment
- `tools/pay_bridge_svm.mjs` — Solana settlement via @seekdaseek/x402-wallet.
  This is what `CASSUM_RAIL=solana` uses. Key file named by SOLANA_PAYER
- `tools/pay_bridge.mjs` — Solana via the PUBLISHED elizaOS plugin, rail
  `solana-plugin`. Installed NOWHERE on this machine; needs
  `npm i @seekdaseek/plugin-agentfeed` before it can run
- `tools/pay_bridge_evm.mjs` — Base settlement. Shells out to
  @seekdaseek/x402-wallet. Key read from a FILE named by EVM_PAYER
- `tools/measure.py` — samples live endpoints round-robin to measure REAL
  empty rates. Pre-flight refuses a run it cannot afford. `--plan` costs it
  without spending
- `tools/payer_info.mjs` — READ ONLY: payer address and Base balances. `--json`
- `tools/record_402.py` — the ONLY thing that touches the network. Writes
  `tests/fixtures/`, which is what the suite parses
- `tools/trace.py` — prints the routing decision sequence
- `tools/report.py` — generates RESULTS.md from runs
- `probes/` — capability probes and JSON reports
- `FINDINGS.md` — defects found in the vendor SDK and docs

## Still to build, in order

1. EXTEND THE MEASUREMENT, or stop. 43 paid calls now stand in `live.db`,
   0.321 USDC, every one with its tx in the journal under `extra.tx`. Measured
   empty rates: cascade-forecast 0.3333 (12 calls), liquidations 0.2400 (25),
   sol-price 0.0000 (6). All 28 calls of the second run matched what the
   handler source predicted, symbol by symbol.
   NOT SAMPLED and why: peg-deviation cannot decline today (all 12 tracked
   symbols carry 288 ticks/24h so no_data is unreachable, uniqueness is 18-57%
   against a 2% stale_pool threshold, and an untracked symbol makes
   resolveSymbol THROW a 500 rather than declining); oi_spike_scan was excluded
   at Sergiu's instruction because its 30-minute baseline rebuilds on every
   deploy and today saw nine. Endpoints with a verified decline branch still
   unsampled: cascade-history 0.03, liq-heatmap 0.05, liq-history 0.05,
   squeeze-score 0.10. See FINDINGS Part C.

OLD, kept for the record: THE MEASUREMENT RUN. BLOCKED ON FUNDS, not code. `tools/measure.py` is
   built and its pre-flight refuses to start: the plan costs 0.2180 USDC and
   the payer holds 0.028000, short 0.1900. A run that dies halfway leaves a
   half-measured empty_rate that reads like a real one, so it refuses rather
   than starting. Fund `0xFEfF369D5048b2Cf817d87467E48404b3ADfE4Ee` with USDC
   on Base, or lower `--samples`. Then regenerate RESULTS.md, whose live
   section reads the run's memory store.

DONE, and its settings kept for the rerun: the live gate.
     a. `npm i @seekdaseek/plugin-agentfeed` in some directory (it is installed
        NOWHERE on this machine right now), then `export CASSUM_BRIDGE_DIR=` it
     b. `export AGENTFEED_PRIVATE_KEY=` the Solana payer, read from a file,
        never pasted. Python never reads this; only the Node bridge does
     c. `export CASSUM_MAX_USDC=0.01` for the first run, then raise it
     d. `python tools/quote.py --live`
   For BASE instead: `export CASSUM_RAIL=base`,
   `export CASSUM_WALLET_DIR=/Volumes/D/skrproject/x402-wallet/x402-wallet`,
   `export EVM_PAYER=/Volumes/D/skrproject/payer-evm.key`, then the same
   `python tools/quote.py --live`. No npm install needed — that checkout already
   has @x402/evm 2.18.0 and viem 2.55.2 installed.
2. README section pointing at the memory call sites by file and function.
   Should also document `tools/session.py` and `tools/quote.py`, which the
   README does not mention yet.
3. Video, 2 to 5 minutes. Two build-in-public posts tagging @sibylcap.

DONE: x402 STAGE 2, settlement path. Cumulative run cap in `SpendCap`, read
from ONE env var `CASSUM_MAX_USDC` (default 0.05) and checked against the
header-parsed amount BEFORE the bridge is invoked, because there is no refund.
An independent per-call ceiling is passed down to the JS client. The default
cap is process-wide on purpose: per-provider caps turned a 0.05 ceiling into
0.05 x 6 in `tools/quote.py`, pinned by
`test_providers_without_an_explicit_cap_share_ONE_run_ceiling`. A bridge
failure RAISES rather than returning a tuple, because plugin service.ts reports
no `paidUsd` on a non-2xx and the spend is genuinely unknown.

DONE: x402 STAGE 1, discovery. `quote()` prices an endpoint off its own 402
challenge; the Router's `.price` comes from there and never from a constant,
pinned by `test_price_is_derived_from_the_challenge_not_from_config`.
`delivered` is decided by a per-endpoint usability predicate, each written
against the SERVER SOURCE and citing it — agentfeed `tools/liqdb.js`,
`answer.js`, caliper `lib/answer.mjs`. The reference case is
get_cascade_forecast, which returns 200 with `evidence: "unmeasured"` when
history is thin; `absent` counts as delivery and `unmeasured` does not, because
caliper's own comment says collapsing those two would be a lie. Suite is
offline: `tests/test_x402.py` blocks `urlopen` for the whole module and proves
the block works.

DONE, commit 370c162: `tools/session.py`. Filmed as two runs of
`python tools/session.py --db ./demo.db --phase learn|recall`. Learn settles
after 9 buys, NOT at hollow-cheap's condemnation on buy 5 — at buy 5 the
standing choice is still `mid` on sticker price. Pinned by
`test_condemned_alone_is_not_a_sufficient_stop`; do not "simplify" that stop
condition back.

## Environment

Mac, zsh. Python 3.12 venv at `~/cassum/.venv`. `source .venv/bin/activate`
first or nothing resolves; system `python3` is 3.9.6 and will not work. Package
is installed editable, so `python tools/trace.py` works from the repo root.
Tests: `python -m pytest tests -q`.
