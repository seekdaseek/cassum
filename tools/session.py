"""Cold-start recall across two separate OS processes. The gate beat.

    python tools/session.py --db ./demo.db --phase learn
    python tools/session.py --db ./demo.db --phase recall

`learn` spends against the simulated fleet and writes what it measured to
`--db`. `recall` is a FRESH interpreter that opens the same file, buys
nothing, and names a provider off the inherited records alone. Nothing is
handed between them except the database on disk.

STOP CONDITION, and why it is not the one in the brief. The brief said learn
should run "until hollow-cheap is CONDEMNED". MEASURED 2026-09-01: that is
reached after buy 5, and at that moment the router's standing choice is `mid`,
not `steady-dear` -- hollow-cheap is out on evidence but mid and steady-dear
are both still LEARNING, so the pick is made on sticker price, which is the
signal this project exists to distrust. Stopping there films a recall that
names the wrong provider. Learn therefore runs until BOTH: hollow-cheap is
CONDEMNED, and the standing choice is a TRUSTED provider, i.e. one picked on
measured cost per delivered payload rather than on exploration. That is buy 9.
See tests/test_session.py::test_condemned_alone_is_not_a_sufficient_stop.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from cassum.memory import Store
from cassum.router import CONDEMNED, TRUSTED, Router
from cassum.sim import default_fleet

REPO = Path(__file__).resolve().parent.parent
CONDEMN_TARGET = "hollow-cheap"


# --- provenance line the gate needs on screen -------------------------------

def commit() -> str:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(REPO), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()

    try:
        head = git("rev-parse", "--short", "HEAD")
    except Exception:
        return "unknown"
    try:
        return f"{head}-dirty" if git("status", "--porcelain") else head
    except Exception:
        return head


def header(phase: str, db: Path) -> None:
    """First line of both phases. Timestamp and commit are what a judge reads
    off the video to prove the two runs are the same build, minutes apart."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    print(f"[cassum] {now} | commit {commit()} | pid {os.getpid()} | phase={phase}")
    print(f"memory: {db}")
    print()


# --- shared rendering -------------------------------------------------------

def provider_rows(store: Store, router: Router) -> list[tuple[str, str, dict[str, Any]]]:
    """Fleet order, not storage order, so learn and recall print the same shape."""
    return [(p.name, *router.verdict(p.name)) for p in router.providers]


def print_records(rows: list[tuple[str, str, dict[str, Any]]], title: str) -> None:
    print(title)
    head = f"  {'provider':<14} {'verdict':<10} {'calls':>5} {'empty':>6} {'empty_rate':>11} {'usdc_spent':>11}"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for name, state, body in rows:
        if not body:
            print(f"  {name:<14} {state:<10} {'-':>5} {'-':>6} {'-':>11} {'-':>11}")
            continue
        print(
            f"  {name:<14} {state:<10} {int(body.get('calls', 0)):>5} "
            f"{int(body.get('empty', 0)):>6} {float(body.get('empty_rate', 0.0)):>11.4f} "
            f"{float(body.get('usdc_spent', 0.0)):>11.5f}"
        )
    print()


def recorded_calls(rows: list[tuple[str, str, dict[str, Any]]]) -> int:
    return sum(int(b.get("calls", 0)) for _, _, b in rows)


# --- learn ------------------------------------------------------------------

def settled(router: Router) -> bool:
    if router.verdict(CONDEMN_TARGET)[0] != CONDEMNED:
        return False
    chosen, _ = router.choose()
    return chosen is not None and router.verdict(chosen.name)[0] == TRUSTED


def phase_learn(db: Path, max_buys: int) -> int:
    store = Store.open(db)
    if store.providers(limit=1):
        print(
            f"REFUSING: {db} already holds provider records. A second learn run would\n"
            f"add to them and the trace would not reproduce. Delete it, or pass --fresh.",
            file=sys.stderr,
        )
        return 2

    router = Router(store, default_fleet())
    print("spending against sim.default_fleet(). every row below is one paid call:")
    head = f"  {'#':>3}  {'bought':<14} {'ok':<4} {'usdc':>6}  reason"
    print(head)
    print("  " + "-" * 76)

    bought = delivered_n = 0
    spent = 0.0
    while not settled(router) and bought < max_buys:
        # choose() is pure, so asking first only names who buy_one is about to pay.
        target, _ = router.choose()
        if target is None:
            break
        delivered, usdc, reason = router.buy_one()
        bought += 1
        spent += usdc
        delivered_n += int(delivered)
        print(
            f"  {bought:>3}  {target.name:<14} {'yes' if delivered else 'NO':<4} "
            f"{usdc:>6.3f}  {reason}"
        )
    print("  " + "-" * 76)

    if not settled(router):
        print(f"UNSETTLED after {bought} buys (--max-buys {max_buys}).", file=sys.stderr)
        return 3

    print(
        f"  stopped after {bought} buys: {CONDEMN_TARGET} is CONDEMNED and the standing\n"
        f"  choice is TRUSTED, i.e. picked on measured cost per payload, not on price."
    )
    print(f"  {delivered_n} delivered for {spent:.3f} USDC\n")

    rows = provider_rows(store, router)
    print_records(rows, "provider records this process WROTE to memory:")
    print(f"purchases-this-process: {bought}")
    print(f"purchases-in-memory: {recorded_calls(rows)}")
    print(f"\nnone of that is in RAM any more. it is in {db}. run --phase recall.")
    return 0


# --- recall -----------------------------------------------------------------

class ReadOnlyStore:
    """Reads delegate to the real Store; writes raise. This is why the recall
    phase's zero-purchase claim is structural and not a promise."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def get_provider(self, name: str) -> dict[str, Any] | None:
        return self._store.get_provider(name)

    def record_purchase(self, provider: str, **kw: Any) -> str:
        raise AssertionError(
            f"the recall phase attempted to buy from {provider}. "
            "it is meant to decide on inherited memory alone."
        )


def phase_recall(db: Path) -> int:
    if not db.exists():
        print(f"NO MEMORY at {db}. run --phase learn first.", file=sys.stderr)
        return 2

    store = Store.open(db)
    if not store.providers(limit=1):
        print(f"MEMORY AT {db} IS EMPTY. run --phase learn first.", file=sys.stderr)
        return 2

    # Writes are disabled before the router is ever handed the store.
    router = Router(ReadOnlyStore(store), default_fleet())
    rows = provider_rows(store, router)
    before = recorded_calls(rows)

    print_records(rows, "provider records INHERITED from memory, written by another process:")
    print(f"purchases-in-memory-on-entry: {before}\n")

    print("what this process computes from those records, having bought nothing:")
    head = (
        f"  {'provider':<14} {'verdict':<10} {'price/call':>11} {'empty_rate':>11} "
        f"{'USDC/payload':>13}"
    )
    print(head)
    print("  " + "-" * (len(head) - 2))
    print(f"  {'(source)':<14} {'memory':<10} {'code':>11} {'memory':>11} {'derived':>13}")
    for provider in router.providers:
        state, body = router.verdict(provider.name)
        if state == CONDEMNED:
            cell = "EXCLUDED"
        elif body:
            cell = f"{router.effective_cost(provider.price, body):.5f}"
        else:
            cell = "-"
        rate = f"{float(body.get('empty_rate', 0.0)):.4f}" if body else "-"
        print(
            f"  {provider.name:<14} {state:<10} {provider.price:>11.3f} {rate:>11} {cell:>13}"
        )
    print()

    chosen, why = router.choose()
    if chosen is None:
        print(f"NO CHOICE: {why}", file=sys.stderr)
        return 3

    print(f"CHOICE: {chosen.name}")
    print(f"WHY: {why}")
    print(
        f"     {chosen.name} is not the cheapest sticker price. {CONDEMN_TARGET} is, and it is\n"
        f"     excluded because memory recorded what it actually delivered. This process\n"
        f"     made no calls of its own; the empty rates above were measured elsewhere."
    )
    print()

    after = recorded_calls(provider_rows(store, router))
    print("purchases-this-process: 0")
    print(f"purchases-in-memory-on-exit: {after}"
          f"{'  (unchanged)' if after == before else '  CHANGED -- SOMETHING BOUGHT'}")
    return 0 if after == before else 4


# --- entry ------------------------------------------------------------------

def remove_db(db: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(db) + suffix)
        if target.exists():
            target.unlink()
            print(f"removed {target}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--phase", required=True, choices=("learn", "recall"))
    ap.add_argument("--max-buys", type=int, default=100)
    ap.add_argument(
        "--fresh", action="store_true",
        help="learn only: delete an existing --db first. Never implicit.",
    )
    args = ap.parse_args()

    # The header must be the FIRST line on screen even when a later step writes
    # to stderr. Under a pipe stdout is block-buffered and stderr is not, which
    # reorders them; the gate reads the timestamp off line one, so pin it.
    sys.stdout.reconfigure(line_buffering=True)

    db = args.db.expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    header(args.phase, db)

    if args.fresh:
        if args.phase != "learn":
            print("--fresh is only valid with --phase learn", file=sys.stderr)
            return 2
        remove_db(db)
        print()

    if args.phase == "learn":
        return phase_learn(db, args.max_buys)
    return phase_recall(db)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # `| head` or a quit `less` closes stdout mid-run. Retarget the fd so
        # the interpreter's own flush at exit cannot raise a second time.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(141)
