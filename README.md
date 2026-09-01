# cassum

Latin: empty, hollow, paid for and not there.

Buyer-side memory for agents that pay per call for data. An agent buying over
x402 has no record of what it bought. It pays, gets back an empty array or a
stale quote, and next run it pays the same provider again. Every purchase is a
cold start. `cassum` remembers what each provider actually delivered and stops
paying the ones that don't.

## Where memory is load-bearing

Built on [Sibyl Memory](https://docs.sibyllabs.org/memory/) (`sibyl-memory-client`).

| What | Tier | Call site |
|---|---|---|
| One record per data provider: calls, empties, USDC spent, empty rate | WARM entity | `cassum/memory.py` → `Store.record_purchase`, `Store.get_provider` |
| One event per paid call: what was considered, what was bought, what came back, what it cost | COLD journal | `cassum/memory.py` → `Store.record_purchase` → `write_event` |

**Delete the memory and the router has no record of what it bought.** On every
fresh start it pays the same failing provider again, and the spend the agent
exists to avoid comes back in full. The core function is not degraded, it is
absent.

## Measured, not assumed

Every call shape in `cassum/memory.py` was measured against
`sibyl-memory-client` 0.8.0 on 2026-09-01. Reports in `probes/`, defects found
in `FINDINGS.md`.

## Run

    python3.12 -m venv .venv
    source .venv/bin/activate
    python -m pip install 'sibyl-memory-cli[mcp]' pytest
    sibyl init
    python -m pytest tests -q

## Prior work declaration

Nothing in this repository predates 2026-09-01. Prior projects by the same
author informed the approach to measured-versus-assumed absence; no code was
carried over.

## License

MIT.
