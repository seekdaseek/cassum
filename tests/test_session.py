"""The cold-start gate, tested the way it will be filmed: two subprocesses.

Nothing here imports `tools/session.py`. The learn phase reaches the recall
phase only through the database file, which is the whole claim being tested --
importing either phase into this file would quietly re-create the shared
interpreter state the demo is supposed to prove it does not need.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

import pytest

from cassum.memory import Store
from cassum.router import CONDEMNED, Router
from cassum.sim import default_fleet

REPO = Path(__file__).resolve().parents[1]
SESSION = REPO / "tools" / "session.py"

HEADER = re.compile(
    r"^\[cassum\] (?P<ts>\S+) \| commit (?P<commit>\S+) \| pid (?P<pid>\d+) \| phase=(?P<phase>\w+)$"
)


def run_phase(db: Path, phase: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SESSION), "--db", str(db), "--phase", phase, *extra],
        cwd=REPO, text=True, capture_output=True, timeout=120,
    )


def header_of(proc: subprocess.CompletedProcess[str]) -> re.Match[str]:
    first = proc.stdout.splitlines()[0]
    m = HEADER.match(first)
    assert m, f"first line is not the provenance header: {first!r}"
    return m


def totals(db: Path) -> dict[str, tuple[int, float]]:
    """Read the store directly. This is the test's own eyes on the file, not
    the recall process's account of itself."""
    store = Store.open(db)
    out = {}
    for row in store.providers():
        body = row["body"]
        out[row["name"]] = (int(body["calls"]), float(body["usdc_spent"]))
    return out


@pytest.fixture
def learned(tmp_path: Path) -> Path:
    db = tmp_path / "demo.db"
    proc = run_phase(db, "learn")
    assert proc.returncode == 0, proc.stderr
    return db


# --- the gate beat ----------------------------------------------------------

def test_recall_names_steady_dear_on_memory_alone(learned: Path):
    """The headline. A fresh interpreter, zero spend, correct provider."""
    proc = run_phase(learned, "recall")
    assert proc.returncode == 0, proc.stderr
    assert "CHOICE: steady-dear" in proc.stdout
    assert "purchases-this-process: 0" in proc.stdout


def test_recall_makes_zero_purchases(learned: Path):
    """Measured against the database itself, not against the process's claim.
    Every counter must be byte-identical across the recall run."""
    before = totals(learned)
    proc = run_phase(learned, "recall")
    assert proc.returncode == 0, proc.stderr
    assert totals(learned) == before
    assert before, "learn wrote nothing, so this asserts nothing"


def test_the_two_phases_are_genuinely_separate_processes(tmp_path: Path):
    """Runs the filmed pair itself rather than leaning on the fixture, so the
    two PIDs compared are the two runs a judge actually watches."""
    db = tmp_path / "demo.db"
    learn = run_phase(db, "learn")
    recall = run_phase(db, "recall")
    assert learn.returncode == 0 and recall.returncode == 0, recall.stderr
    assert header_of(learn)["pid"] != header_of(recall)["pid"]


def test_recall_reports_no_purchase_lines_of_its_own(learned: Path):
    """The learn phase prints one row per paid call. Recall must print none."""
    proc = run_phase(learned, "recall")
    assert "every row below is one paid call" not in proc.stdout
    assert "purchases-in-memory-on-exit: 9  (unchanged)" in proc.stdout


# --- the provenance line the gate reads off the video -----------------------

@pytest.mark.parametrize("phase", ["learn", "recall"])
def test_first_line_carries_an_iso_timestamp_and_the_commit(tmp_path: Path, phase: str):
    db = tmp_path / "demo.db"
    if phase == "recall":
        assert run_phase(db, "learn").returncode == 0
    m = header_of(run_phase(db, phase))
    assert m["phase"] == phase
    stamp = dt.datetime.fromisoformat(m["ts"])
    assert stamp.tzinfo is not None, "timestamp must be unambiguous on film"
    head = subprocess.check_output(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], text=True
    ).strip()
    assert m["commit"].split("-")[0] == head


# --- what learn actually leaves behind --------------------------------------

def test_learn_writes_a_condemnation_and_a_trusted_pick(learned: Path):
    store = Store.open(learned)
    router = Router(store, default_fleet())
    assert router.verdict("hollow-cheap")[0] == CONDEMNED
    assert router.choose()[0].name == "steady-dear"


def test_learn_refuses_a_second_run_on_the_same_db(learned: Path):
    """A second run would double the counters and the filmed trace would stop
    reproducing. Refusal is the feature."""
    proc = run_phase(learned, "learn")
    assert proc.returncode == 2
    assert "REFUSING" in proc.stderr
    proc = run_phase(learned, "learn", "--fresh")
    assert proc.returncode == 0, proc.stderr


def test_recall_refuses_when_there_is_no_memory(tmp_path: Path):
    proc = run_phase(tmp_path / "absent.db", "recall")
    assert proc.returncode == 2
    assert "NO MEMORY" in proc.stderr


# --- MEASURED NEGATIVE, kept on purpose -------------------------------------

def test_condemned_alone_is_not_a_sufficient_stop(tmp_path: Path):
    """Why learn does not stop where the brief said it should.

    MEASURED 2026-09-01: hollow-cheap reaches CONDEMNED after buy 5, but at
    that instant mid and steady-dear are both still LEARNING, so the standing
    choice is `mid` -- picked on sticker price, the one signal this project
    exists to distrust. A recall filmed from that state names the wrong
    provider. Delete this test and the stop condition looks arbitrary.
    """
    store = Store.open(tmp_path / "stop.db")
    router = Router(store, default_fleet())
    buys = 0
    while router.verdict("hollow-cheap")[0] != CONDEMNED and buys < 50:
        router.buy_one()
        buys += 1
    assert buys == 5
    assert router.choose()[0].name == "mid"
