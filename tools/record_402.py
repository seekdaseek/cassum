"""Record real 402 challenges to tests/fixtures/ so the suite can run offline.

Discovery only. A GET with no X-PAYMENT header is answered with 402 and settles
nothing, so this script cannot spend. It is the only thing in the repo that
touches the network, and the test suite never runs it.

    python tools/record_402.py                 # refresh every fixture
    python tools/record_402.py --path /api/sol-price
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

from cassum.x402 import (
    BASE_URL,
    CHALLENGE_HEADER,
    FIXTURE_DIR,
    RECORDED_PATHS,
    REQUEST_HEADERS,
    fixture_name,
)

def record(path: str, base_url: str = BASE_URL) -> dict:
    url = f"{base_url}{path}"
    req = urllib.request.Request(url, method="GET", headers=dict(REQUEST_HEADERS))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, headers, body = resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as err:
        # A 402 arrives here, not in the success branch. That is the whole point.
        status, headers, body = err.code, dict(err.headers), err.read().decode()

    if status != 402:
        raise SystemExit(f"{path}: expected 402, got {status}. Not recording a non-challenge.")
    if CHALLENGE_HEADER not in {k.lower() for k in headers}:
        raise SystemExit(f"{path}: 402 carried no {CHALLENGE_HEADER} header.")

    return {
        "_recorded_from": url,
        "_note": "verbatim 402 challenge. regenerate with tools/record_402.py.",
        "status": status,
        "headers": {k.lower(): v for k, v in headers.items()},
        "body": body,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", action="append", help="repeatable; defaults to the recorded set")
    ap.add_argument("--base-url", default=BASE_URL)
    args = ap.parse_args()

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for path in args.path or RECORDED_PATHS:
        data = record(path, args.base_url)
        out = FIXTURE_DIR / fixture_name(path)
        out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(f"{path:<58} -> {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")


if __name__ == "__main__":
    main()
