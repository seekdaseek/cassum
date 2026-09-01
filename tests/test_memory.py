from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cassum.memory import TIERS, Store


def store() -> Store:
    return Store.open(Path(tempfile.mkdtemp(prefix="cassum-t-")) / "t.db")


def test_absent_provider_is_none_not_an_exception():
    assert store().get_provider("never-seen") is None


def test_present_but_empty_is_not_absent():
    s = store()
    s._c.set_entity("provider", "hollow", {})
    got = s.get_provider("hollow")
    assert got is not None
    assert got["body"] == {}


def test_tier_names_are_singular():
    s = store()
    s._c.search("x", tiers=TIERS)
    with pytest.raises(ValueError):
        s._c.search("x", tiers=("entities",))


def test_reference_tier_is_decoded_back_to_a_dict():
    s = store()
    s._c.set_reference("k", {"a": [1, 2]})
    assert isinstance(s._c.get_reference("k")["body"], str)
    assert s.read_reference("k") == {"a": [1, 2]}


def test_purchase_accumulates_and_journals_all_four_fields():
    s = store()
    s.record_purchase("prov-a", delivered=False, usdc=0.001, considered=["prov-a", "prov-b"], next_step=["retry in 1h"])
    s.record_purchase("prov-a", delivered=True, usdc=0.001)
    body = s.get_provider("prov-a")["body"]
    assert body["calls"] == 2
    assert body["empty"] == 1
    assert body["empty_rate"] == 0.5
    ev = s._c.read_events(limit=2)[-1]
    assert ev["evaluated"] == ["prov-a", "prov-b"]
    assert ev["forward"] == ["retry in 1h"]
    assert ev["extra"]["provider"] == "prov-a"


def test_read_events_returns_newest_first():
    s = store()
    first = s._c.write_event(acted=["one"])
    second = s._c.write_event(acted=["two"])
    rows = s._c.read_events(limit=2)
    assert [r["id"] for r in rows] == [second, first]
