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

## State as of commit 370c162

26 tests green. Working: memory layer, router, deletion-test harness, trace
tool, generated RESULTS.md, packaging, public repo, cold-start recall demo.

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
- `tools/trace.py` — prints the routing decision sequence
- `tools/report.py` — generates RESULTS.md from runs
- `probes/` — capability probes and JSON reports
- `FINDINGS.md` — defects found in the vendor SDK and docs

## Still to build, in order

1. A real x402 provider adapter behind the same interface as `sim.SimProvider`.
   BLOCKED: waiting on which live endpoints are in scope. Do not invent any.
2. README section pointing at the memory call sites by file and function.
   Should also document the two `tools/session.py` commands, which the README
   does not mention yet.
3. Video, 2 to 5 minutes. Two build-in-public posts tagging @sibylcap.

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
