"""x402 adapter, offline. Every challenge here was recorded from the live
service by tools/record_402.py and is parsed from disk.

Network is blocked for the whole module by an autouse fixture, so "no network
in the test suite" is enforced rather than promised. `test_the_network_block_is_live`
proves the block itself works, because a guard nobody tests is decoration.
"""
from __future__ import annotations

import json
import tempfile
import urllib.request
from pathlib import Path

import pytest

from cassum.memory import Store
from cassum.router import UNTRIED, Router
from cassum.sim import SimProvider
from cassum.x402 import (
    BASE_RAIL,
    FIXTURE_DIR,
    PaymentNotImplemented,
    Quote,
    X402Error,
    X402Provider,
    decode_challenge_header,
    endpoint_name,
    fixture_name,
    is_usable,
    parse_challenge,
    predicate_for,
    quote,
    quote_from_fixture,
    usable_nonempty_envelope,
)

# Documented Base-rail facts. If the service changes any of these, the fixtures
# are re-recorded and these move with them -- deliberately not read from the
# fixture, or the assertion would be circular.
BASE_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_PAY_TO = "0x22DB3A9686EE5261e7Bf3ed4f91277232E8076e6"

TOKEN_RISK_PATH = "/api/token-risk/So11111111111111111111111111111111111111112"

# path -> (base units as the server writes them, USDC)
DOCUMENTED_PRICES = {
    "/api/sol-price": ("1000", 0.001),
    "/api/market-snapshot": ("3000", 0.003),
    "/api/liquidations": ("3000", 0.003),
    "/api/cascade-forecast": ("20000", 0.02),
    "/api/squeeze-score": ("100000", 0.1),
    TOKEN_RISK_PATH: ("10000", 0.01),
}


class NetworkUsedInTests(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **kw):
        raise NetworkUsedInTests("the suite touched the network; parse a fixture instead")

    monkeypatch.setattr(urllib.request, "urlopen", boom)


def test_the_network_block_is_live():
    with pytest.raises(NetworkUsedInTests):
        quote("/api/sol-price")


# --- stage 1: discovery -----------------------------------------------------

@pytest.mark.parametrize("path", sorted(DOCUMENTED_PRICES))
def test_quote_prices_match_the_documented_base_units(path):
    """The required test, over all six recorded endpoints. `amount` is a string
    in USDC base units at 6dp; both the raw string and the derived float are
    pinned so a decimal-shift bug cannot pass by rounding."""
    units, usdc = DOCUMENTED_PRICES[path]
    q = quote_from_fixture(path)
    assert q.rail(BASE_RAIL).amount_base_units == units
    assert q.price == usdc


def test_the_documented_three_thousand_is_three_thousandths():
    """The brief's worked example, kept as its own assertion because it is the
    one number a reader will check by hand."""
    assert quote_from_fixture("/api/market-snapshot").price == 0.003


@pytest.mark.parametrize("path", sorted(DOCUMENTED_PRICES))
def test_base_rail_carries_the_documented_asset_and_treasury(path):
    rail = quote_from_fixture(path).rail(BASE_RAIL)
    assert rail.network == BASE_RAIL
    assert rail.asset == BASE_ASSET
    assert rail.pay_to == BASE_PAY_TO
    assert rail.scheme == "exact"


@pytest.mark.parametrize("path", sorted(DOCUMENTED_PRICES))
def test_every_challenge_is_version_2_and_offers_both_rails(path):
    q = quote_from_fixture(path)
    assert q.x402_version == 2
    networks = {r.network for r in q.rails}
    assert BASE_RAIL in networks
    assert any(n.startswith("solana:") for n in networks)


def test_quote_carries_the_description_and_tags_the_server_advertises():
    q = quote_from_fixture("/api/cascade-forecast")
    assert "FORWARD-LOOKING" in q.description
    assert "forecast" in q.tags and "cascade" in q.tags
    assert q.resource_url.endswith("/api/cascade-forecast")


def test_price_is_derived_from_the_challenge_not_from_config():
    """Rewrite the amount in a copy of a real challenge and the provider's
    price must move with it. A price read from a constant would not."""
    raw = json.loads((FIXTURE_DIR / fixture_name("/api/sol-price")).read_text())
    q = parse_challenge(raw["status"], raw["headers"], "/api/sol-price")
    assert X402Provider("/api/sol-price", quote_=q).price == 0.001

    import base64

    payload = json.loads(base64.b64decode(raw["headers"]["payment-required"] + "=="))
    for accept in payload["accepts"]:
        accept["amount"] = "7000"
    raw["headers"]["payment-required"] = base64.b64encode(
        json.dumps(payload).encode()
    ).decode()
    moved = parse_challenge(raw["status"], raw["headers"], "/api/sol-price")
    assert X402Provider("/api/sol-price", quote_=moved).price == 0.007


def test_every_recorded_fixture_parses():
    """Guards a bad re-record. A fixture that no longer parses is a broken
    suite pretending to be a broken service."""
    files = sorted(FIXTURE_DIR.glob("*.json"))
    assert len(files) >= 3
    for f in files:
        raw = json.loads(f.read_text())
        assert raw["status"] == 402
        assert raw["body"] == "{}"
        q = parse_challenge(raw["status"], raw["headers"], "/x")
        assert q.price > 0


# --- challenge parsing, the failure modes -----------------------------------

def test_a_non_402_is_refused():
    with pytest.raises(X402Error, match="expected HTTP 402"):
        parse_challenge(200, {"payment-required": "e30="}, "/api/sol-price")


def test_a_402_without_the_header_is_refused():
    with pytest.raises(X402Error, match="no payment-required header"):
        parse_challenge(402, {"content-type": "application/json"}, "/api/sol-price")


def test_header_lookup_is_case_insensitive():
    raw = json.loads((FIXTURE_DIR / fixture_name("/api/sol-price")).read_text())
    shouty = {k.upper(): v for k, v in raw["headers"].items()}
    assert parse_challenge(402, shouty, "/api/sol-price").price == 0.001


def test_stripped_base64_padding_still_decodes():
    """Header values lose trailing '=' in transit. Losing the quote with it
    would read as 'this endpoint is unpriced'."""
    raw = json.loads((FIXTURE_DIR / fixture_name("/api/sol-price")).read_text())
    unpadded = raw["headers"]["payment-required"].rstrip("=")
    assert decode_challenge_header(unpadded)["x402Version"] == 2


def test_garbage_in_the_header_is_an_error_not_a_free_endpoint():
    with pytest.raises(X402Error):
        decode_challenge_header("not-base64-at-all!!")


def test_a_challenge_with_no_rails_cannot_be_paid():
    import base64

    blob = base64.b64encode(json.dumps({"x402Version": 2, "accepts": []}).encode()).decode()
    with pytest.raises(X402Error, match="offers no rails"):
        parse_challenge(402, {"payment-required": blob}, "/api/sol-price")


def test_asking_for_an_unoffered_rail_names_what_was_offered():
    q = quote_from_fixture("/api/sol-price")
    with pytest.raises(X402Error, match="eip155:1"):
        q.rail("eip155:1")


# --- the usability predicate: what `delivered` actually means ---------------
#
# Payload shapes below are taken from the server source, not invented:
# agentfeed tools/cascade-forecast.js, caliper lib/answer.mjs, agentfeed
# tools/liqdb.js and answer.js. Field names guessed rather than read are how
# agentfeed's own answer.js shipped two tools returning null.

def envelope(tool: str, data) -> dict:
    """MEASURED on the free endpoint /api/fear-greed."""
    return {"tool": tool, "data": data, "paid": True}


def forecast(evidence: str, p=None) -> dict:
    answer = {"symbol": "SOLUSDT", "evidence": evidence, "p": p}
    if evidence != "measured":
        answer["reason"] = "only 3 windows of history for this symbol, minimum is 8"
    return envelope("get_cascade_forecast", {"model": {"scheme": "q90"}, "answers": [answer]})


def test_a_declined_forecast_is_not_delivered_despite_http_200():
    """THE REFERENCE CASE. get_cascade_forecast declines by design when history
    is thin: 200, evidence 'unmeasured', p null. Charged, and worth nothing."""
    assert is_usable("/api/cascade-forecast", forecast("unmeasured")) is False


def test_a_measured_forecast_is_delivered():
    assert is_usable("/api/cascade-forecast", forecast("measured", 0.31)) is True


def test_absent_evidence_is_delivered_because_it_is_a_fact_about_the_world():
    """caliper lib/answer.mjs separates 'absent' (we cover this symbol and it
    genuinely has no volume) from 'unmeasured' (our own gap) and calls
    collapsing them a lie. 'absent' answers the buyer's question."""
    assert is_usable("/api/cascade-forecast", forecast("absent")) is True


def test_a_batch_is_delivered_if_any_single_answer_is_usable():
    payload = envelope("get_cascade_forecast", {"answers": [
        {"symbol": "SOLUSDT", "evidence": "unmeasured", "p": None},
        {"symbol": "BTCUSDT", "evidence": "measured", "p": 0.42},
    ]})
    assert is_usable("/api/cascade-forecast", payload) is True


def test_a_batch_of_nothing_but_declines_is_not_delivered():
    payload = envelope("get_cascade_forecast", {"answers": [
        {"symbol": "AAAUSDT", "evidence": "unmeasured", "p": None},
        {"symbol": "BBBUSDT", "evidence": "unmeasured", "p": None},
    ]})
    assert is_usable("/api/cascade-forecast", payload) is False


def test_an_empty_answers_list_is_not_delivered():
    assert is_usable("/api/cascade-forecast", envelope("x", {"answers": []})) is False


def test_an_empty_result_set_is_not_delivered():
    """agentfeed tools/liqdb.js returns 200 with an empty list and a note when
    the window held nothing. The brief names this case explicitly."""
    empty = envelope("get_liq_heatmap", {
        "symbol": "SOLUSDT", "hours": 24, "levels": [],
        "note": "no liquidations recorded in window",
    })
    assert is_usable("/api/liq-heatmap", empty) is False
    filled = envelope("get_liq_heatmap", {"symbol": "SOLUSDT", "levels": [{"lo": 1, "hi": 2}]})
    assert is_usable("/api/liq-heatmap", filled) is True


def test_a_squeeze_score_of_zero_is_delivered():
    """Zero is a real score. `if not score` would bin it as an empty and teach
    the Router to condemn a provider that answered correctly."""
    assert is_usable("/api/squeeze-score", envelope("s", {"short_squeeze_score": 0})) is True
    assert is_usable("/api/squeeze-score", envelope("s", {"short_squeeze_score": None})) is False


def test_a_clean_token_is_delivered_even_with_no_risk_flags():
    """agentfeed answer.js, VERIFIED against tools/tokenrisk.js: there is no
    risk score, only risk_flags and risk_flag_count. An empty flag list means
    'nothing wrong with this mint', which is the answer, not a non-delivery."""
    clean = envelope("get_token_risk", {"risk_flags": [], "risk_flag_count": 0})
    assert is_usable(TOKEN_RISK_PATH, clean) is True
    assert is_usable(TOKEN_RISK_PATH, envelope("get_token_risk", {})) is False


def test_a_null_or_zero_price_is_not_delivered():
    assert is_usable("/api/sol-price", envelope("p", {"price": 214.5})) is True
    assert is_usable("/api/sol-price", envelope("p", {"price": None})) is False
    assert is_usable("/api/sol-price", envelope("p", {"price": 0})) is False


def test_a_missing_envelope_is_never_usable():
    for junk in (None, "", {}, [], {"data": None}, {"data": []}, {"no": "envelope"}):
        assert is_usable("/api/sol-price", junk) is False


def test_an_unregistered_endpoint_falls_back_to_the_weak_default():
    """Documented as weak on purpose: it cannot tell an answer from a decline,
    so it under-reports empties. Pinned so nobody mistakes it for a real one."""
    assert predicate_for("/api/never-registered") is usable_nonempty_envelope
    assert is_usable("/api/never-registered", envelope("x", {"anything": 1})) is True
    assert is_usable("/api/never-registered", envelope("x", {})) is False


def test_path_parameter_routes_resolve_by_prefix():
    from cassum.x402 import usable_token_risk

    assert predicate_for("/api/token-risk/AnyMintAddressAtAll") is usable_token_risk


def test_a_path_parameter_never_becomes_the_provider_name():
    """Found by running tools/quote.py: naming this provider after its last
    path segment called it `So1111..112`. Router keys memory on the name, so
    every mint would file as its own provider, none would ever reach min_calls,
    and memory would accumulate without changing a single decision -- the exact
    failure the deletion test is supposed to detect, hidden behind real writes.
    """
    assert endpoint_name(TOKEN_RISK_PATH) == "token-risk"
    assert provider(TOKEN_RISK_PATH).name == "token-risk"
    other = "/api/token-risk/EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert endpoint_name(other) == endpoint_name(TOKEN_RISK_PATH)
    assert endpoint_name("/api/sol-price") == "sol-price"


# --- stage 2: payment is gated ----------------------------------------------

def provider(path: str = "/api/cascade-forecast", **kw) -> X402Provider:
    return X402Provider(path, quote_=quote_from_fixture(path), **kw)


def test_fetch_refuses_when_live_is_off_which_is_the_default():
    p = provider()
    assert p.live is False
    with pytest.raises(PaymentNotImplemented, match="live=True"):
        p.fetch()


def test_fetch_still_refuses_with_live_on_because_signing_is_not_implemented():
    with pytest.raises(PaymentNotImplemented, match="not implemented"):
        provider(live=True).fetch()


def test_the_refusal_names_the_spend_and_the_payee():
    with pytest.raises(PaymentNotImplemented) as err:
        provider(live=True).fetch()
    message = str(err.value)
    assert "0.02 USDC" in message
    assert BASE_PAY_TO in message


def test_discovery_works_with_payment_off():
    p = provider()
    assert p.price == 0.02
    assert p.name == "cascade-forecast"
    assert "cascade" in p.tags
    assert p.decide(forecast("measured", 0.3)) is True


# --- interface parity with the simulated fleet ------------------------------

def test_x402_provider_offers_the_same_members_as_simprovider():
    """The Router only ever touches .name, .price and .fetch(). If this drifts,
    a live provider stops being drop-in and the whole adapter is pointless."""
    sim = SimProvider("steady-dear", 0.005, [True])
    live = provider()
    for member in ("name", "price", "fetch"):
        assert hasattr(sim, member) and hasattr(live, member)
    assert isinstance(live.name, str)
    assert isinstance(live.price, float)


def test_router_ranks_a_live_provider_without_paying_anything():
    """The Router reaches a verdict and a choice from memory and price alone.
    fetch() is never called, so this spends nothing and proves discovery is
    enough to be routed on."""
    store = Store.open(Path(tempfile.mkdtemp(prefix="cassum-x-")) / "x.db")
    cheap = provider("/api/sol-price")
    dear = provider("/api/squeeze-score")
    router = Router(store, [dear, cheap])

    assert router.verdict(cheap.name)[0] == UNTRIED
    chosen, why = router.choose()
    assert chosen is cheap, "untried providers are explored cheapest first"
    assert "sol-price" in why
    assert store.get_provider(cheap.name) is None, "choosing must not write"


def test_a_quote_is_frozen_so_a_price_cannot_be_edited_in_place():
    q = quote_from_fixture("/api/sol-price")
    with pytest.raises(Exception):
        q.price = 0.0  # type: ignore[misc]
    assert isinstance(q, Quote)
