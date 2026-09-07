# cassum

Latin: empty, hollow, paid for and not there.

Buyer-side memory for agents that pay per call for data. An agent buying over
x402 has no record of what it bought. It pays, gets back an empty array or a
stale quote, and next run it pays the same provider again. Every purchase is a
cold start. `cassum` remembers what each provider actually delivered and stops
paying the ones that don't.

## Where memory is load-bearing

Built on [Sibyl Memory](https://docs.sibyllabs.org/memory/) (`sibyl-memory-client`).
Two functions, one file. Everything below is in `cassum/memory.py`.

| | write | read |
|---|---|---|
| **function** | `Store.record_purchase` | `Store.get_provider` |
| **line** | [`memory.py:115`](cassum/memory.py#L115) | [`memory.py:81`](cassum/memory.py#L81) |
| **tier** | WARM entity + COLD journal | WARM entity |
| **called from** | `Router.buy_one` — [`router.py:76`](cassum/router.py#L76) | `Router.verdict` — [`router.py:34`](cassum/router.py#L34) |

**What is persisted.** One entity per provider, keyed by name
([`memory.py:144`](cassum/memory.py#L144)): `calls`, `empty`, `usdc_spent`,
`empty_rate`. Plus one journal event per paid call
([`memory.py:153`](cassum/memory.py#L153)) carrying what was evaluated, what
was bought, what came back and — for a real payment — the on-chain
transaction hash under `extra.tx`.

**How a fresh process recalls it.** `Store.open(path)` on the same SQLite file.
`tools/session.py --phase recall` is exactly that: a second OS process, a
`ReadOnlyStore` whose `record_purchase` raises
([`session.py:157`](tools/session.py#L157)) so it *cannot* buy, and it still
names the right provider. See [Cold-start recall](#cold-start-recall-across-two-processes).

**What decision changes.** `Router.verdict`
([`router.py:31`](cassum/router.py#L31)) turns a provider record into UNTRIED /
LEARNING / TRUSTED / CONDEMNED. `Router.effective_cost`
([`router.py:45`](cassum/router.py#L45)) then ranks on
`price / (1 - empty_rate)` — cost per *delivered* payload — rather than on
sticker price. With memory, the dearest provider per call is chosen because it
is the cheapest per payload. Without it, every provider is UNTRIED forever and
the only available signal is the sticker price.

### The deletion test

`cassum/ablate.py` runs the same workload twice: once against Sibyl Memory,
once against a `NullStore` ([`ablate.py:13`](cassum/ablate.py#L13)) that
accepts every write and returns nothing on every read. That second run is the
judges' litmus test, executed rather than asserted.

| | calls | USDC | USDC per delivered payload |
|---|---:|---:|---:|
| with memory | 24 | 0.111 | 0.00555 |
| **memory deleted** | **80** | **0.24** | **0.012** |

Both runs delivered 20 payloads. **2.162x** the cost per payload with memory
removed — 80 paid calls instead of 24. The memoryless agent buys from the
cheapest sticker price every time, which is not a strawman: it is the only
rational policy available with no purchase history.

Those figures are generated, never typed. `python tools/report.py` rewrites
`RESULTS.md` from an actual run, and `RESULTS.md` is authoritative if this
table ever drifts.

**The core function is not degraded, it is absent.** Delete the memory and the
router has no record of what it bought; on every fresh start it pays the same
failing provider again, and the spend the agent exists to avoid comes back in
full.

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

Settlement runs on **either rail**, chosen by `CASSUM_RAIL` (default `solana`,
so an upgrade never silently changes which chain money moves on). Pricing and
settlement are separate: a provider priced on Base can be paid on Solana or the
reverse, depending on which bridge is configured.

| `CASSUM_RAIL` | Bridge | Signs with | Directory |
|---|---|---|---|
| `solana` | `tools/pay_bridge.mjs` | `@seekdaseek/plugin-agentfeed` | `CASSUM_BRIDGE_DIR` |
| `base` | `tools/pay_bridge_evm.mjs` | `@seekdaseek/x402-wallet` | `CASSUM_WALLET_DIR` |

**This repo contains no signing code.** Both bridges are thin translators: they
hand a key read from a file to a library that already has a live mainnet
settlement on its rail, and print one line of JSON back. See `FINDINGS.md` 8
to 10 for three measured defects found along the way.

Discovery spends nothing and needs no key:

    python tools/quote.py              # live, unpaid: prices off the 402 header
    python tools/quote.py --fixture    # recorded challenges, no network

Payment is off unless asked for, and capped by one environment variable:

    export CASSUM_MAX_USDC=0.01        # cumulative ceiling for the whole run
    export CASSUM_RAIL=base            # or solana, the default

    # base rail
    export CASSUM_WALLET_DIR=/path/to/x402-wallet
    export EVM_PAYER=/path/to/payer-evm.key      # a FILE, read only by Node

    # solana rail
    export CASSUM_BRIDGE_DIR=/path/to/a/dir/with/plugin-agentfeed/installed
    export AGENTFEED_PRIVATE_KEY=...             # read only by Node

The cap is checked against the amount parsed from the `payment-required`
header *before* anything is signed, because there is no refund. A second,
independent per-call ceiling is enforced inside the JS client.

## Partner stacks, and where

**Base.** The `base` rail above is where Base does real work. An x402 payment is
signed and settled on Base mainnet before a payload is returned, and the
resulting transaction hash is written into memory on the journal event as
`extra.tx` (`cassum/memory.py:153`), so what was paid and what it bought are one
record rather than two. The rail is selected with `CASSUM_RAIL=base` and the
bridge is `tools/pay_bridge_evm.mjs`, signing through `@seekdaseek/x402-wallet`.
Base mainnet settlements recorded during this build, transaction hashes
included, are in `FINDINGS.md` 8 to 12 — finding 12 bisected an undocumented
length bound in the facilitator that made an endpoint unpayable, and it cost
real USDC on Base mainnet to find.

**Virtuals Protocol.** Not used, and not claimed.

## Cold-start recall, across two processes
<a id="cold-start-recall-across-two-processes"></a>

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

## How memory made this possible

Without a memory layer the router has exactly one signal: the price in the 402
challenge. Price is free to discover and says nothing about whether the payload
is worth having, so the only rational memoryless policy is to buy the cheapest
provider, every time, forever.

Sibyl Memory is what turns a purchase into evidence. One entity per provider
carries `calls`, `empty`, `usdc_spent` and `empty_rate`. One journal event per
paid call carries what was evaluated, what came back, and the transaction hash.
That is the entire difference between the two runs in the deletion test: 24 paid
calls against 80, for the same 20 delivered payloads. Remove the entity tier and
every provider is UNTRIED forever, and the router falls back to sticker price.

The recall process is what proves the dependence lives on disk and not in RAM.
It runs in a different pid, its `record_purchase` raises rather than buying, and
it still names the right provider from records another process wrote.

## Prior work declaration

Nothing in this repository predates 2026-09-01. Prior projects by the same
author informed the approach to measured-versus-assumed absence; no code was
carried over.

## License

MIT.
