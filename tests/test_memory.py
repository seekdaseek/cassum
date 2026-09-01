from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cassum.memory import TIERS, Store, tx_hash


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


def test_a_settlement_is_journalled_with_its_tx_lifted_out():
    """Every paid call must leave its on-chain receipt in the journal. The hash
    is lifted to extra["tx"] so an auditor never has to know which key a given
    rail used -- Base returns `transaction`, and the next rail need not."""
    s = store()
    receipt = {
        "success": True,
        "payer": "0xFEfF369D5048b2Cf817d87467E48404b3ADfE4Ee",
        "transaction": "0x1f99b069600f32d890c710b3e8894e415cbe039c6e102750c28d7c9866a580cf",
        "network": "eip155:8453",
    }
    s.record_purchase("sol-price", delivered=True, usdc=0.001, settlement=receipt)
    extra = s._c.read_events(limit=1)[0]["extra"]
    assert extra["tx"] == receipt["transaction"]
    assert extra["settlement"] == receipt, "the whole receipt is kept, not just the hash"


def test_a_simulated_purchase_journals_no_settlement_keys():
    """Absence is a value. A sim purchase has no receipt and must not carry an
    empty one, or a reader cannot tell 'not paid' from 'paid, no hash'."""
    s = store()
    s.record_purchase("hollow-cheap", delivered=False, usdc=0.003)
    extra = s._c.read_events(limit=1)[0]["extra"]
    assert "tx" not in extra and "settlement" not in extra


def test_tx_hash_reads_the_spellings_rails_actually_use():
    assert tx_hash({"transaction": "0xabc"}) == "0xabc"      # MEASURED on Base
    assert tx_hash({"signature": "5XPKfw"}) == "5XPKfw"      # candidate, Solana
    assert tx_hash({"txHash": "0xdef"}) == "0xdef"
    assert tx_hash({"success": True}) is None                 # settled, no hash given
    assert tx_hash(None) is None and tx_hash("nope") is None


def test_an_unrecognised_receipt_is_kept_whole_rather_than_dropped():
    """If a rail names its hash something we have not seen, the receipt still
    reaches the journal intact -- losing it would destroy the audit trail for
    the one case we did not anticipate."""
    s = store()
    s.record_purchase("x", delivered=True, usdc=0.01, settlement={"weird_key": "0xzzz"})
    extra = s._c.read_events(limit=1)[0]["extra"]
    assert "tx" not in extra
    assert extra["settlement"] == {"weird_key": "0xzzz"}
