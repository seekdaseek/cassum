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

## Paying for real: what the live adapter does and does not prove

`cassum/x402.py` is a real provider behind the same interface as the simulated
one — `.name`, `.price`, `.fetch()` — so the router cannot tell them apart.
Price is read from the endpoint's own 402 challenge, never from config.

**A live run cannot produce a savings number.** The `payTo` address on both
rails is a treasury this project controls, so USDC paid in a live run comes
back to the same hands; a percentage computed from that is circular. The
savings figure in `RESULTS.md` comes from the simulated fleet and only from
there. The two claims are separate and must not be added together.

What a live run is for, and all it is for:

1. **Settlement works** — one verifiable on-chain transaction hash.
2. **Real empty rates** — how often each sampled endpoint actually returns
   something usable, measured instead of chosen.

Settlement is on **Solana**. The JS payer this repo shells out to is an SVM
client and cannot sign for Base, though both rails are quoted and priced. See
`FINDINGS.md` 8 to 10 for that and two other measured defects.

Discovery spends nothing and needs no key:

    python tools/quote.py              # live, unpaid: prices off the 402 header
    python tools/quote.py --fixture    # recorded challenges, no network

Payment is off unless asked for, and capped by one environment variable:

    export CASSUM_MAX_USDC=0.01        # cumulative ceiling for the whole run
    export CASSUM_BRIDGE_DIR=/path/to/a/dir/with/plugin-agentfeed/installed
    export AGENTFEED_PRIVATE_KEY=...   # read by the Node bridge, never by Python

The cap is checked against the amount parsed from the `payment-required`
header *before* anything is signed, because there is no refund. A second,
independent per-call ceiling is enforced inside the JS client.

## Cold-start recall, across two processes

    python tools/session.py --db ./demo.db --phase learn
    python tools/session.py --db ./demo.db --phase recall

The second process opens the same database, buys nothing, and names a provider
from the inherited records alone. Both print an ISO timestamp, the commit hash
and their pid on the first line.

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

The suite is offline. `tools/record_402.py` is the only thing in the repo that
touches the network, and `tests/test_x402.py` blocks `urlopen` for its whole
module.

## Prior work declaration

Nothing in this repository predates 2026-09-01. Prior projects by the same
author informed the approach to measured-versus-assumed absence; no code was
carried over.

## License

MIT.
