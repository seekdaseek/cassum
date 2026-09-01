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
- Account tier is FREE, so `learn()`, `learner()`, `lint()` and the
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

## State as of commit 370c162

77 tests green. Working: memory layer, router, deletion-test harness, trace
tool, generated RESULTS.md, packaging, public repo, cold-start recall demo,
x402 discovery adapter (stage 1; payment gated off).

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
- `tools/record_402.py` — the ONLY thing that touches the network. Writes
  `tests/fixtures/`, which is what the suite parses
- `tools/trace.py` — prints the routing decision sequence
- `tools/report.py` — generates RESULTS.md from runs
- `probes/` — capability probes and JSON reports
- `FINDINGS.md` — defects found in the vendor SDK and docs

## Still to build, in order

1. x402 STAGE 2, settlement. BLOCKED ON A DECISION, not on code: which signer,
   which rail, and what spend cap. `X402Provider.fetch()` raises
   `PaymentNotImplemented` naming the three missing pieces. Do not implement
   signing without Sergiu saying so — it spends real treasury USDC.
2. README section pointing at the memory call sites by file and function.
   Should also document `tools/session.py` and `tools/quote.py`, which the
   README does not mention yet.
3. Video, 2 to 5 minutes. Two build-in-public posts tagging @sibylcap.

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
