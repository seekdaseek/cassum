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
import json
import sys

from cassum.x402 import (
    BASE_RAIL,
    CAP_ENV,
    RAIL_ENV,
    SETTLEMENT_RAILS,
    BridgeError,
    CapExceeded,
    PaymentNotImplemented,
    default_cap,
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
        "--rail", choices=sorted(SETTLEMENT_RAILS),
        help=f"which bridge signs. Default ${RAIL_ENV}, else 'solana'.",
    )
    ap.add_argument(
        "--live", action="store_true",
        help=f"SPENDS REAL USDC. Off by default. Ceiling is ${CAP_ENV}, default 0.05.",
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
        p = X402Provider(path, quote_=q, live=args.live, network=args.network,
                         rail=args.rail)
        providers.append(p)
        rail = q.rail(args.network)
        print(f"{p.name:<34} {rail.amount_base_units:>8} {p.price:>8}  {','.join(q.tags[:4])}")

    if not providers:
        print("\nnothing quoted.", file=sys.stderr)
        return 1

    print("-" * 96)
    print(f"{len(providers)} endpoints priced from the challenge header. Nothing was paid.\n")

    if not args.live:
        print(f"payment is OFF. --live spends real USDC, capped by ${CAP_ENV}.")
        return 0

    # --- from here on, real money can move -----------------------------------
    cheapest = min(providers, key=lambda p: p.price)
    try:
        # ONE cap for the run. Every provider built above already shares it via
        # default_cap(); taking it from there rather than making a new one keeps
        # the ledger honest if this tool ever buys more than once.
        cap = default_cap()
        rail = cheapest.settlement_rail()
    except (CapExceeded, BridgeError) as err:
        print(f"NOT SETTLED: {err}", file=sys.stderr)
        return 3
    print("--live given. THIS SPENDS REAL USDC.")
    print(f"  run ceiling   {cap.limit} USDC   (${CAP_ENV}, default 0.05)")
    print(f"  buying        {cheapest.name} at {cheapest.price} USDC")
    print(f"  priced on     {args.network}")
    print(f"  SETTLING ON   {rail.network}  via the {cheapest.rail.name} bridge")
    print(f"  signer        {cheapest.rail.script.name}  (${cheapest.rail.dir_env})")
    print(f"  paying        {rail.pay_to}\n")

    cheapest.live = True
    try:
        delivered, usdc = cheapest.fetch()
    except CapExceeded as err:
        print(f"REFUSED BY CAP, nothing signed: {err}", file=sys.stderr)
        return 3
    except (BridgeError, PaymentNotImplemented) as err:
        print(f"NOT SETTLED: {err}", file=sys.stderr)
        return 3

    settlement = getattr(cheapest, "settlement", None)
    print(f"paid       {usdc} USDC")
    print(f"delivered  {delivered}   (usability predicate, not the status code)")
    print(f"settlement {json.dumps(settlement) if settlement else 'none reported'}")
    print(f"run spend  {cap.spent} of {cap.limit} USDC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
