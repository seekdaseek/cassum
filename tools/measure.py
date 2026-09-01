"""Sample live endpoints to measure REAL empty rates. Spends real USDC.

    python tools/measure.py --db ./live.db --plan            # cost only, no spend
    python tools/measure.py --db ./live.db                   # the run

Sampling is deliberately ROUND-ROBIN and not `Router.collect()`. The router's
job is to stop paying bad providers, so it condemns and then avoids them --
exactly the wrong behaviour for a measurement, which needs N samples of EVERY
provider including the ones that decline. The router is what CONSUMES these
numbers later; it must not also produce them.

Every purchase is written through `Store.record_purchase`, so the transaction
hash lands in the journal event's `extra` and the provider records accumulate
the same way a real routed run would.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

from cassum.memory import Store, tx_hash
from cassum.router import Router
from cassum.x402 import (
    CAP_ENV, RAIL_ENV, BridgeError, CapExceeded, SpendCap, X402Provider,
    endpoint_name, quote, rail_from_env,
)

REPO = Path(__file__).resolve().parent.parent

# The reference case leads. get_cascade_forecast declines by design when a
# symbol's history is thin, so the symbol mix IS the measurement: majors have
# deep tape and should answer, obscure perps should decline. A run of nothing
# but SOLUSDT would report a 0% empty rate and prove nothing about the predicate.
# Interleaved, not blocked: a run cut short by budget must still have sampled
# both kinds. Blocking majors first would report a 0% empty rate for any run
# that stopped early.
FORECAST_SYMBOLS = [
    "SOL", "SXT", "BTC", "ZEREBRO", "ETH",
    "GOAT", "XRP", "MOODENG", "DOGE", "PNUT",
]

# MEASURED: /api/liquidations answers even for obscure perps (ZEREBROUSDT
# returned 25 prints), so the empty case needs genuinely quiet or unlisted
# symbols. A buyer scanning a token list hits exactly these, and the empty
# result set is what the usability predicate must catch.
# Interleaved for the same reason as FORECAST_SYMBOLS, and it was NOT the first
# time round: a 5-call run against this list in blocked order never reached the
# unlisted symbols at indices 5 and 6, so the empty case went untested and the
# run reported a 0% empty rate it had not earned.
LIQ_SYMBOLS = [
    "SOLUSDT",            # deep tape, expect delivery
    "NOSUCHCOINUSDT",     # unlisted, expect an empty set
    "BTCUSDT",
    "PLACEHOLDER1USDT",   # unlisted
    "ZEREBROUSDT",        # thin but listed
    "ETHUSDT",
    "MOODENGUSDT",
]

# Sample counts are what the remaining budget affords, not what is ideal.
# Purchases ACCUMULATE in the store, so a later run with more funds extends
# these same provider records rather than starting over.
DEFAULT_PLAN = [
    ("/api/cascade-forecast", 4),    # the reference case, priced at 0.02 each
    ("/api/liquidations", 5),
    ("/api/sol-price", 6),
]


def payer_balance() -> dict | None:
    """Read-only. None when it cannot be determined, which is not fatal."""
    script = REPO / "tools" / "payer_info.mjs"
    if not script.exists() or not os.environ.get("EVM_PAYER"):
        return None
    try:
        out = subprocess.run(["node", str(script), "--json"], capture_output=True,
                             text=True, timeout=60)
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return None


def sampled_path(path: str, i: int) -> str:
    """Vary the query so the sample spans real behaviour, not one lucky symbol.
    The query never reaches the provider name -- see route_of()."""
    if path == "/api/cascade-forecast":
        return f"{path}?symbol={FORECAST_SYMBOLS[i % len(FORECAST_SYMBOLS)]}"
    if path == "/api/liquidations":
        return f"{path}?symbol={LIQ_SYMBOLS[i % len(LIQ_SYMBOLS)]}"
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--plan", action="store_true", help="price the run and exit. Spends nothing.")
    ap.add_argument("--samples", type=int, help="override every endpoint's sample count")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    db = args.db.expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    rail = rail_from_env()
    cap = SpendCap()

    print(f"[cassum] {dt.datetime.now(dt.timezone.utc).isoformat()} | measurement run")
    print(f"  {RAIL_ENV:<16} {rail.name} -> {rail.script.name}")
    print(f"  {CAP_ENV:<16} {cap.limit} USDC")
    print(f"  memory           {db}\n")

    plan = [(p, args.samples or n) for p, n in DEFAULT_PLAN]

    # --- pre-flight. Price the whole run BEFORE paying for any of it ---------
    print(f"  {'endpoint':<24} {'price':>7} {'x':>3} {'subtotal':>9}")
    print("  " + "-" * 48)
    total = 0.0
    priced = []
    for path, n in plan:
        q = quote(path)                       # live, unpaid
        usd = q.price_on(rail.network) if rail.name != "solana" else q.rails[0].usdc
        try:
            usd = next(r for r in q.rails if r.network.startswith(rail.network)).usdc
        except StopIteration:
            print(f"  {path}: no {rail.network}* rail offered", file=sys.stderr)
            return 2
        priced.append((path, n, q, usd))
        total += usd * n
        print(f"  {endpoint_name(path):<24} {usd:>7} {n:>3} {usd * n:>9.4f}")
    print("  " + "-" * 48)
    print(f"  {'PLANNED SPEND':<24} {'':>7} {'':>3} {total:>9.4f} USDC\n")

    bal = payer_balance()
    if bal:
        print(f"  payer            {bal['address']}")
        print(f"  balance          {bal['usdc']:.6f} USDC")
    print(f"  run cap          {cap.limit} USDC")

    problems = []
    if total > cap.limit:
        problems.append(f"planned {total:.4f} exceeds {CAP_ENV}={cap.limit}")
    if bal and total > bal["usdc"]:
        problems.append(
            f"planned {total:.4f} exceeds the payer's {bal['usdc']:.6f} USDC "
            f"(short {total - bal['usdc']:.4f})"
        )
    if problems:
        print("\n  REFUSING TO START:", file=sys.stderr)
        for p in problems:
            print(f"    - {p}", file=sys.stderr)
        print("\n  A run that dies halfway leaves a half-measured empty_rate that reads\n"
              "  like a real one. Fund the payer or lower --samples.", file=sys.stderr)
        return 2
    if args.plan:
        print("\n  --plan given. Nothing was paid.")
        return 0

    # --- the run ------------------------------------------------------------
    store = Store.open(db)
    print(f"\n  {'#':>3}  {'endpoint':<20} {'ok':<4} {'usdc':>6}  tx")
    print("  " + "-" * 92)
    i = 0
    stopped = None
    for path, n, q, usd in priced:
        for k in range(n):
            call = sampled_path(path, k)
            p = X402Provider(call, quote_=q, live=True, cap=cap, rail=rail,
                             name=endpoint_name(path))
            try:
                delivered, paid = p.fetch()
            except (CapExceeded, BridgeError) as err:
                stopped = f"{type(err).__name__}: {err}"
                break
            i += 1
            settlement = getattr(p, "settlement", None)
            store.record_purchase(
                p.name, delivered=delivered, usdc=paid,
                considered=[endpoint_name(x) for x, _, _, _ in priced],
                next_step=[] if delivered else [f"re-evaluate {p.name}"],
                note=f"measurement sample {k + 1}/{n} of {call}",
                settlement=settlement,
            )
            tx = tx_hash(settlement) or "-"
            print(f"  {i:>3}  {p.name:<20} {'yes' if delivered else 'NO':<4} "
                  f"{paid:>6.3f}  {tx}")
        if stopped:
            break
    print("  " + "-" * 92)
    if stopped:
        print(f"  STOPPED EARLY: {stopped}", file=sys.stderr)

    print(f"\n  measured empty rates, read back from memory:")
    head = f"  {'provider':<20} {'calls':>6} {'empty':>6} {'empty_rate':>11} {'usdc':>9}"
    print(head)
    print("  " + "-" * (len(head) - 2))
    router = Router(store, [])
    for row in store.providers():
        b = row["body"]
        state = "condemned" if float(b.get("empty_rate", 0)) >= router.threshold and int(b["calls"]) >= router.min_calls else ""
        print(f"  {row['name']:<20} {b['calls']:>6} {b['empty']:>6} "
              f"{b['empty_rate']:>11.4f} {b['usdc_spent']:>9.5f}  {state}")
    print(f"\n  spent this run   {cap.spent} of {cap.limit} USDC")
    print(f"  memory           {db}")
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
