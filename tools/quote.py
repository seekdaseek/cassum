"""Discovery against the live x402 service. Stage 1: this cannot spend.

    python tools/quote.py                       # quote the recorded set, live
    python tools/quote.py --path /api/sol-price
    python tools/quote.py --fixture             # parse recordings, no network
    python tools/quote.py --live                # attempt payment. REFUSES.

A quote is an unpaid GET. The server answers 402 with the price in the
`payment-required` header and settles nothing, which is why this is safe to run
in a loop. `--live` is the settlement switch and is OFF unless typed; with it
on, fetch() still refuses, because signing spends real treasury USDC and is not
implemented here.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from cassum.x402 import (
    BASE_RAIL,
    PaymentNotImplemented,
    X402Error,
    RECORDED_PATHS,
    X402Provider,
    quote,
    quote_from_fixture,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", action="append", help="repeatable; defaults to the recorded set")
    ap.add_argument("--network", default=BASE_RAIL)
    ap.add_argument("--fixture", action="store_true", help="parse tests/fixtures instead of the network")
    ap.add_argument(
        "--live", action="store_true",
        help="attempt settlement. OFF by default. Spends real USDC once implemented.",
    )
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    paths = args.path or RECORDED_PATHS
    source = "tests/fixtures" if args.fixture else "live, unpaid"
    print(f"[cassum] {dt.datetime.now(dt.timezone.utc).isoformat()} | quotes from {source}")
    print(f"rail {args.network}\n")

    head = f"{'endpoint':<34} {'units':>8} {'USDC':>8}  tags"
    print(head)
    print("-" * 96)

    providers = []
    for path in paths:
        try:
            q = quote_from_fixture(path) if args.fixture else quote(path)
        except (X402Error, OSError) as err:
            print(f"{path:<34} {'-':>8} {'-':>8}  ERROR: {err}")
            continue
        p = X402Provider(path, quote_=q, live=args.live, network=args.network)
        providers.append(p)
        rail = q.rail(args.network)
        print(f"{p.name:<34} {rail.amount_base_units:>8} {p.price:>8}  {','.join(q.tags[:4])}")

    if not providers:
        print("\nnothing quoted.", file=sys.stderr)
        return 1

    print("-" * 96)
    print(f"{len(providers)} endpoints priced from the challenge header. Nothing was paid.\n")

    if not args.live:
        print("payment is OFF. Re-run with --live to see what settlement would need.")
        return 0

    print("--live given, attempting settlement on the cheapest endpoint:\n")
    cheapest = min(providers, key=lambda p: p.price)
    try:
        cheapest.fetch()
    except PaymentNotImplemented as err:
        print(f"REFUSED: {err}")
        return 3
    print("a payment path exists. That should not be reachable yet.", file=sys.stderr)
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
