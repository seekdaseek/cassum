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
# Symbol choices are EVIDENCE, not guesses. Each was checked against the live
# data before the run: /opt/agentfeed/liquidations.db for tape coverage and
# /opt/caliper/model.json for which symbols the forecaster was fitted on.
#
#   (symbol, expectation, why)
# expectation is what the source says SHOULD happen. Recording it next to the
# outcome is the point: a symbol that delivers when it was expected empty is a
# finding about coverage, not a failed test.

# Checked 2026-09-01 against liquidations.db (849 symbols ever, 723 in 24h).
LIQ_SYMBOLS = [
    ("SOLUSDT",          "deliver", "major, continuously in the tape"),
    ("NOSUCHCOINUSDT",   "EMPTY",   "invented ticker, 0 rows ever in liquidations.db"),
    ("BTCUSDT",          "deliver", "major, continuously in the tape"),
    ("AI16ZUSDT",        "EMPTY",   "REAL token, but 0 rows ever recorded - a genuine tape gap"),
    ("ZEREBROUSDT",      "deliver", "1,228 rows ever"),
    ("PLACEHOLDER1USDT", "EMPTY",   "invented ticker, 0 rows ever in liquidations.db"),
    ("ETHUSDT",          "deliver", "major, continuously in the tape"),
    ("FARTCOINUSDT",     "deliver", "10,462 rows ever"),
    ("MOODENGUSDT",      "deliver", "1,538 rows ever"),
    ("BANANAS31USDT",    "deliver", "1,703 rows ever"),
]

# Checked against model.json: fitted 2026-09-01T12:00Z on 1,831,458 pairs,
# thresholds stored for 384 symbols. No stored threshold means the symbol was
# not in the training universe, and caliper's answer.mjs then refuses rather
# than recomputing a threshold that would answer a different question.
FORECAST_SYMBOLS = [
    ("SOLUSDT",        "measured",   "model threshold present"),
    ("AI16ZUSDT",      "UNMEASURED", "no model threshold AND 0 tape rows - never recorded"),
    ("BTCUSDT",        "measured",   "model threshold present"),
    ("ANETUSDT",       "UNMEASURED", "no model threshold, 1 tape row - far below minWindows"),
    ("ETHUSDT",        "measured",   "model threshold present"),
    ("CLSKUSDT",       "UNMEASURED", "no model threshold, 1 tape row"),
    ("ZEREBROUSDT",    "measured",   "model threshold present (1,228 tape rows)"),
    ("NOSUCHCOINUSDT", "UNMEASURED", "invented ticker, never recorded"),
]

# peg-deviation is DELIBERATELY ABSENT. Verified before spending: all 12 tracked
# symbols carry 288 ticks/24h and 12/hour, so `no_data` is unreachable with the
# `hours` floor of 1; on-chain price uniqueness runs 18-57% against a 2%
# stale_pool threshold, so `stale_pool` is unreachable too; and an untracked
# symbol makes resolveSymbol THROW, which is a 500, not a priced decline.
# Sampling it would buy six guaranteed deliveries.
#
# oi_spike_scan is also absent, at Sergiu's instruction: its 30-minute baseline
# rebuilds from process boot and today saw nine redeploys, so a warming:true
# would measure our own deploys rather than the product.
DEFAULT_PLAN = [
    ("/api/liquidations", 20),
    ("/api/cascade-forecast", 8),
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


def sampled_path(path: str, i: int):
    """Vary the query so the sample spans real behaviour, not one lucky symbol.
    The query never reaches the provider name -- see route_of()."""
    table = {"/api/cascade-forecast": FORECAST_SYMBOLS, "/api/liquidations": LIQ_SYMBOLS}.get(path)
    if not table:
        return path, None
    sym, expect, why = table[i % len(table)]
    return f"{path}?symbol={sym}", (sym, expect, why)


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

    # --- declare the symbol plan BEFORE spending -----------------------------
    print("\n  symbols chosen, and why. `expect` is what the SOURCE says should happen;")
    print("  a symbol that delivers when it was expected empty is a coverage finding.")
    print(f"\n  {'symbol':<18} {'expect':<11} why")
    print("  " + "-" * 88)
    for _, table in (("liq", LIQ_SYMBOLS), ("fc", FORECAST_SYMBOLS)):
        for sym, expect, why in table:
            print(f"  {sym:<18} {expect:<11} {why}")
        print()

    # --- the run ------------------------------------------------------------
    store = Store.open(db)
    print(f"  {'#':>3}  {'endpoint':<18} {'symbol':<17} {'expect':<11} {'got':<4} {'usdc':>6}")
    print("  " + "-" * 78)
    i = 0
    stopped = None
    outcomes: list[tuple] = []
    for path, n, q, usd in priced:
        for k in range(n):
            call, meta = sampled_path(path, k)
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
            sym, expect, _why = meta or ("-", "-", "")
            got = "yes" if delivered else "NO"
            agree = (expect == "EMPTY" or expect == "UNMEASURED") == (not delivered)
            outcomes.append((p.name, sym, expect, delivered, agree))
            print(f"  {i:>3}  {p.name:<18} {sym:<17} {expect:<11} {got:<4} {paid:>6.3f}"
                  f"{'' if agree else '   <-- DISAGREES WITH SOURCE'}")
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
    # --- expectation vs outcome ---------------------------------------------
    if outcomes:
        disagreed = [o for o in outcomes if not o[4]]
        print(f"\n  expectation check: {len(outcomes) - len(disagreed)}/{len(outcomes)} matched the source")
        if disagreed:
            print("  SYMBOLS THAT DISAGREED WITH THE SOURCE -- coverage findings, not failures:")
            for name, sym, expect, delivered, _ in disagreed:
                print(f"    {name}/{sym}: source said {expect}, "
                      f"actually {'delivered' if delivered else 'returned nothing usable'}")

    print(f"\n  spent this run   {cap.spent} of {cap.limit} USDC")
    print(f"  memory           {db}")
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
