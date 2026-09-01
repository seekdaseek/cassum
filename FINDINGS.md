# Findings against sibyl-memory-client 0.8.0

Measured 2026-09-01 on macOS, Python 3.12.13. Raw output in `probes/`.

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
