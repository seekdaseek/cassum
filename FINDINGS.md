# Findings

Measured 2026-09-01 on macOS, Python 3.12.13.

Part A is the memory SDK, raw output in `probes/`. Part B is the x402
adapter: two of those are the vendor's, one is ours, and it is labelled.

# Part A — sibyl-memory-client 0.8.0

## 1. Documented parameter name does not exist

The quickstart shows `set_entity("project", "atlas", {...})` described as
`kind`. The measured signature is
`set_entity(category, name, body, *, status=None)`. Every doc example uses a
keyword the SDK does not accept.

## 2. `search(tiers=...)` tier names are singular and undocumented

`search(query, *, tiers)` is not in the docs at all. Passing the plural raises:

    ValueError: unknown tiers: entities; valid: entity, state, reference, journal

The archive tier is described in the docs but is not a searchable tier.

## 3. Free cap is documented twice with two different numbers

The tiers table says 2 MB. The prose below it says 5,242,880 bytes.
`free_tier_status()` returns `soft_cap_bytes: 5242880`. The table is wrong.

## 4. `write_event` has four undocumented fields

Measured: `write_event(*, evaluated, acted, forward, extra, ts)`. Only `acted`
appears in the docs. All four round-trip intact through `read_events`.

## 5. Reference and state tiers return different types for the same input

Write a dict to both. `get_state` returns `body` as a dict. `get_reference`
returns `body` as a JSON string. Undocumented asymmetry.

## 6. Persisted credentials do not match the constructor

`sibyl init` writes `account_id, bearer_token, email, issued_at,
session_token, tenant_id, tier, wallet`. `MemoryClient.local` accepts
`credentials_claim` and `credentials_signature`, which the file never contains.

## 7. Archived entities appear to have no deletion path

`archive_entity` moves an entity out of the active set; `delete_entity` then
returns False for that name and the public surface offers no purge. The docs
say archived rows are stored plaintext at rest and advise "use archive to
declutter, delete to truly forget". Inferred from the public surface, not
proven — a purge may exist elsewhere.

## Not a defect, retracted

An earlier reading of `delete_entity` returning `False` was reported as a
silent failure. A control run shows it returns True on a live entity and False
when there is nothing to delete. Correct behaviour. The original observation
was on an already-archived entity and lacked a control.

# Part B — the x402 adapter

Challenges recorded verbatim in `tests/fixtures/` by `tools/record_402.py`.

## 8. Cloudflare answers the default Python User-Agent with 403, not 402

`x402.ochinimus.app` sits behind Cloudflare, which rejects the stdlib default
`Python-urllib/3.12` User-Agent:

    default urllib   403  challenge_header=False  body='{"type":"https://developers.cloudflare.com/...'
    curl/8.7.1       402  challenge_header=True   body='{}'

The 403 carries **no `payment-required` header at all**. A client that treats
"no challenge header" as "this endpoint is free" or "this endpoint is down"
reads a live, correctly-priced service as broken — and the failure is invisible
from `curl`, which passes. Every stdlib-Python x402 client hits this.

Fixed by sending a named User-Agent (`cassum/x402.py`, `REQUEST_HEADERS`).
This is the service's edge configuration, not an x402 protocol defect.

## 9. `paidGet` reports zero spend on a non-2xx that may have been paid

`@seekdaseek/plugin-agentfeed` 0.1.2, `src/service.ts`: `paidGet` returns early
when `!res.ok` and never sets `paidUsd`, while the `totalSpentUsd` increment
sits *after* that return. A request that settled and then got a 500 back is
reported as `{ok: false, paidUsd: undefined}` and never counted against the
lifetime spend.

So a failed paid call has genuinely UNKNOWN spend. `X402Provider.fetch` raises
`BridgeError` rather than returning a tuple, because `(False, 0.0)` understates
cost and `(False, price)` overstates it; both invent a data point the Router
would then learn from. Not fixed here — it is a different repo.

## 10. OUR BUG: naming a provider after its path parameter fragments memory

Found by running `tools/quote.py`, not by a test. `X402Provider` derived
`.name` from the last path segment, so `/api/token-risk/So1111..112` produced a
provider named `So1111..112`.

`Router` keys memory on `provider.name`. Every mint would therefore file as a
separate provider: none would reach `min_calls`, none would ever leave
LEARNING, and the router would go on paying while memory accumulated rows that
changed no decision. Memory would look busy and be inert — which is exactly the
failure the deletion test is supposed to expose, hidden behind real writes.

Fixed by `endpoint_name()`, which strips the argument from the five known
parameterised routes. Pinned by
`test_a_path_parameter_never_becomes_the_provider_name`.

## 11. `validateFacilitatorCapabilities` does not fail boot on an unsupported rail

agentfeed `payments.js:102` says of the middleware's sync-on-start: "If the
facilitator doesn't support our network, boot fails loudly — that IS the check."
It is not the check. `@x402/core` 2.18.0 `server/index.js`:

    validateFacilitatorCapabilities() {
      ...
      const supportedKind = this.getSupportedKind(x402Version2, network, scheme);
      if (!supportedKind) continue;          // <-- skipped, not an error

An unsupported network is silently skipped at boot. The real gate is later, in
`buildPaymentRequirements()`, which throws `Facilitator does not support
<scheme> on <network>` when the same lookup misses.

The consequence is the opposite of alarming, and worth writing down because it
is load-bearing evidence: since the live service returns a 402 whose `accepts[]`
contains an `eip155:8453` / `exact` entry, and that entry can only be produced
by the function that throws, **the configured facilitator demonstrably supports
EVM exact-scheme on Base.** That is what makes the Base rail payable. It was
established from source plus one observed 402, not from documentation.

