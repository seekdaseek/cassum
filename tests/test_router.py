from __future__ import annotations

import tempfile
from pathlib import Path

from cassum.ablate import NullStore, run
from cassum.memory import Store
from cassum.router import CONDEMNED, LEARNING, TRUSTED, UNTRIED, Router
from cassum.sim import SimProvider, default_fleet, flat_fleet


def fresh() -> Store:
    return Store.open(Path(tempfile.mkdtemp(prefix="cassum-r-")) / "r.db")


def test_absent_provider_is_untried_not_condemned():
    r = Router(fresh(), default_fleet())
    assert r.verdict("hollow-cheap")[0] == UNTRIED
    assert r.choose()[0].name == "hollow-cheap"


def test_one_failure_does_not_condemn():
    r = Router(fresh(), default_fleet())
    r.buy_one()
    assert r.verdict("hollow-cheap")[0] == LEARNING


def test_exploration_is_breadth_first():
    """MEASURED: untried outranks learning, so every provider gets one call
    before any gets a second. Discovered by a test that assumed otherwise."""
    s = fresh()
    r = Router(s, default_fleet())
    for _ in range(3):
        r.buy_one()
    for p in r.providers:
        assert int(s.get_provider(p.name)["body"]["calls"]) == 1


def test_condemned_once_min_calls_of_evidence_exists():
    """Drives the evidence directly so the assertion tests the VERDICT rule,
    not the routing order."""
    s = fresh()
    for _ in range(3):
        s.record_purchase("hollow-cheap", delivered=False, usdc=0.003)
    r = Router(s, default_fleet())
    assert r.verdict("hollow-cheap")[0] == CONDEMNED
    assert r.choose()[0].name != "hollow-cheap"


def test_end_to_end_condemnation_takes_six_buys():
    """The number is measured, not chosen: three exploratory calls spread one
    each, then two more on hollow-cheap to reach min_calls, then it is skipped."""
    s = fresh()
    r = Router(s, default_fleet())
    for _ in range(5):
        r.buy_one()
    assert r.verdict("hollow-cheap")[0] == CONDEMNED
    assert r.choose()[0].name != "hollow-cheap"


def test_all_condemned_returns_a_reason_not_a_crash():
    r = Router(fresh(), [SimProvider("dead", 0.001, [False])])
    for _ in range(3):
        r.buy_one()
    p, why = r.choose()
    assert p is None and "condemned" in why


def test_null_store_never_learns():
    r = Router(NullStore(), default_fleet())
    for _ in range(6):
        r.buy_one()
    assert r.verdict("hollow-cheap")[0] == UNTRIED


def test_effective_cost_beats_per_call_price():
    """The dearest provider per call is the cheapest per payload, and the
    router must end up there. This is the whole thesis in one assertion."""
    r = Router(fresh(), default_fleet())
    for _ in range(12):
        r.buy_one()
    assert r.verdict("steady-dear")[0] == TRUSTED
    assert r.choose()[0].name == "steady-dear"


def test_deletion_test_memory_costs_strictly_less():
    r = run(wanted=20)
    assert r["with_memory"]["delivered"] == 20
    assert r["without_memory"]["delivered"] == 20
    assert r["with_memory"]["usdc"] < r["without_memory"]["usdc"]


def test_memory_is_overhead_when_no_provider_has_an_edge():
    """MEASURED NEGATIVE, kept deliberately. Every provider in flat_fleet
    costs the same per delivered payload, so exploration buys nothing and
    memory ends up strictly more expensive. Memory pays only when providers
    differ in cost per delivered payload."""
    r = run(wanted=20, fleet=flat_fleet)
    assert r["with_memory"]["usdc"] > r["without_memory"]["usdc"]
