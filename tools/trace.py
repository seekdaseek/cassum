"""Print the routing decision sequence. Every column is read from memory."""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from cassum.memory import Store
from cassum.router import Router
from cassum.sim import default_fleet, flat_fleet

FLEETS = {"default": default_fleet, "flat": flat_fleet}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--buys", type=int, default=14)
    ap.add_argument("--fleet", choices=sorted(FLEETS), default="default")
    args = ap.parse_args()

    fleet = FLEETS[args.fleet]
    db = Path(tempfile.mkdtemp(prefix="cassum-trace-")) / "t.db"
    store = Store.open(db)
    r = Router(store, fleet())
    names = [p.name for p in r.providers]

    head = f"{'#':>3}  {'bought':<14} {'ok':<4} {'usdc':>6}  " + "  ".join(f"{n:<12}" for n in names)
    print(head)
    print("-" * len(head))
    spent = got = 0.0, 0
    spent, got = 0.0, 0
    for i in range(1, args.buys + 1):
        before = {n: r.verdict(n) for n in names}
        p, _ = r.choose()
        if p is None:
            print(f"{i:>3}  {'(none)':<14}")
            break
        delivered, usdc, _ = r.buy_one()
        spent += usdc
        got += int(delivered)
        cells = []
        for n in names:
            state, body = before[n]
            calls = int(body.get("calls", 0))
            cells.append(f"{state[:4]}/{calls}".ljust(12))
        print(f"{i:>3}  {p.name:<14} {'yes' if delivered else 'NO':<4} {usdc:>6.3f}  " + "  ".join(cells))

    print("-" * len(head))
    per = spent / got if got else float("nan")
    print(f"{got} delivered for {spent:.3f} USDC = {per:.5f} per payload")
    print("\nstate columns show the verdict BEFORE that purchase, as state/prior-calls")


if __name__ == "__main__":
    main()
