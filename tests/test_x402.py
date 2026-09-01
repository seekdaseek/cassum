"""x402 adapter, offline. Every challenge here was recorded from the live
service by tools/record_402.py and is parsed from disk.

Network is blocked for the whole module by an autouse fixture, so "no network
in the test suite" is enforced rather than promised. `test_the_network_block_is_live`
proves the block itself works, because a guard nobody tests is decoration.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import urllib.request
from pathlib import Path

import pytest

from cassum.memory import Store
from cassum.router import UNTRIED, Router
from cassum.sim import SimProvider
from cassum.x402 import (
    BASE_RAIL,
    CAP_ENV,
    BridgeError,
    CapExceeded,
    RAIL_ENV,
    SETTLEMENT_RAILS,
    SpendCap,
    rail_from_env,
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


def test_a_peg_no_data_or_stale_pool_is_not_delivered():
    """agentfeed tools/peg.js answers 200 with an explicit status. Only 'ok' is
    an answer; 'no_data' and 'stale_pool' are the tool saying it has nothing."""
    ok = envelope("get_peg_deviation", {"symbol": "USDe", "status": "ok", "window_hours": 24})
    assert is_usable("/api/peg-deviation", ok) is True
    for status in ("no_data", "stale_pool"):
        bad = envelope("get_peg_deviation", {"symbol": "X", "status": status, "window_hours": 24})
        assert is_usable("/api/peg-deviation", bad) is False
        assert is_usable("/api/peg-sessions", bad) is False


def test_a_warming_oi_spike_scan_is_not_delivered():
    """derivs.js builds its 30-minute baseline from the first call AFTER BOOT,
    so a freshly redeployed server sells nothing usable for half an hour. It
    answers 200 the whole time."""
    warming = envelope("get_oi_spike_scan", {"warming": True, "ready_in_min": 26,
                                             "note": "spike baseline builds from first call after boot"})
    assert is_usable("/api/oi-spike-scan", warming) is False
    ready = envelope("get_oi_spike_scan", {"source": "bybit_linear_universe",
                                           "baseline_min_ago": 31,
                                           "spikes": [{"symbol": "SOLUSDT", "oi_change_pct": 12.4}]})
    assert is_usable("/api/oi-spike-scan", ready) is True
    empty = envelope("get_oi_spike_scan", {"source": "x", "baseline_min_ago": 31, "spikes": []})
    assert is_usable("/api/oi-spike-scan", empty) is False


def test_a_squeeze_score_decline_is_not_delivered():
    """MEASURED from liqdb.js:161. A decline carries `decline` and `reason` and
    NO score field at all, which is why requiring a numeric score is the right
    test. Its own note: 'a decline is an answer, not an error'."""
    decline = envelope("get_squeeze_score", {
        "symbol": "NOSUCHCOINUSDT", "decline": "symbol_not_found",
        "reason": "no venue quotes NOSUCHCOINUSDT and it has never appeared in the liquidation tape",
        "missing_inputs": ["funding_rate_8h", "oi_change_24h_pct", "long_account_pct"],
        "recorded_in_tape": False,
    })
    assert is_usable("/api/squeeze-score", decline) is False
    partial = envelope("get_squeeze_score", {"symbol": "X", "decline": "insufficient_inputs",
                                             "missing_inputs": ["funding_rate_8h"]})
    assert is_usable("/api/squeeze-score", partial) is False


def test_an_unregistered_endpoint_falls_back_to_the_weak_default():
    """Documented as weak on purpose: it cannot tell an answer from a decline,
    so it under-reports empties. Pinned so nobody mistakes it for a real one."""
    assert predicate_for("/api/never-registered") is usable_nonempty_envelope
    assert is_usable("/api/never-registered", envelope("x", {"anything": 1})) is True
    assert is_usable("/api/never-registered", envelope("x", {})) is False


def test_path_parameter_routes_resolve_by_prefix():
    from cassum.x402 import usable_token_risk

    assert predicate_for("/api/token-risk/AnyMintAddressAtAll") is usable_token_risk


def test_a_query_string_never_becomes_the_provider_name():
    """Same bug as the token-risk one, one layer along. The measurement run
    calls /api/cascade-forecast?symbol=X to exercise the decline; if the query
    reached the name, every symbol would file as its own provider and no
    empty_rate would ever accumulate."""
    assert endpoint_name("/api/cascade-forecast?symbol=SOLUSDT") == "cascade-forecast"
    assert endpoint_name("/api/cascade-forecast?symbol=BTCUSDT") == "cascade-forecast"
    assert endpoint_name("/api/token-risk/SoMint?verbose=1") == "token-risk"
    # and the predicate must still resolve past the query
    assert predicate_for("/api/cascade-forecast?symbol=X") is not usable_nonempty_envelope
    assert is_usable("/api/cascade-forecast?symbol=X", forecast("unmeasured")) is False
    assert is_usable("/api/cascade-forecast?symbol=X", forecast("measured", 0.3)) is True


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


class FakeBridge:
    """Stands in for tools/pay_bridge.mjs. Records every invocation, so a test
    can assert the bridge was NEVER reached -- which is the only way to prove
    the cap guards before signing rather than after."""

    def __init__(self, result: dict):
        self.result = result
        self.calls: list[tuple[str, float]] = []
        self.rails: list[str | None] = []

    def __call__(self, path: str, *, max_per_call: float, rail=None, **kw) -> dict:
        self.calls.append((path, max_per_call))
        self.rails.append(getattr(rail, "name", rail))
        return self.result


def paid(data, usd: float = 0.02) -> dict:
    return {"ok": True, "status": 200, "data": data, "paidUsd": usd,
            "settlement": {"transaction": "5xFakeSig"}, "spentUsd": usd,
            "payer": "GBFo...SQKz", "error": None}


def test_fetch_refuses_when_live_is_off_which_is_the_default():
    p = provider()
    assert p.live is False
    with pytest.raises(PaymentNotImplemented, match="live=True"):
        p.fetch()


def test_a_paid_call_that_returns_a_decline_is_charged_and_not_delivered():
    """The whole thesis in one assertion. 200, real money moved, evidence
    'unmeasured', so the buyer paid 0.02 for nothing usable."""
    bridge = FakeBridge(paid(forecast("unmeasured")))
    p = provider(live=True, cap=SpendCap(1.0), bridge=bridge)
    delivered, usdc = p.fetch()
    assert delivered is False
    assert usdc == 0.02
    assert p.cap.spent == 0.02


def test_a_paid_call_that_returns_a_measured_answer_is_delivered():
    bridge = FakeBridge(paid(forecast("measured", 0.31)))
    delivered, usdc = provider(live=True, cap=SpendCap(1.0), bridge=bridge).fetch()
    assert delivered is True
    assert usdc == 0.02


def test_the_cap_refuses_BEFORE_the_bridge_is_ever_invoked():
    """There is no refund, so a cap checked after signing is a log line. The
    assertion that matters is bridge.calls being empty."""
    bridge = FakeBridge(paid(forecast("measured", 0.3)))
    p = provider(live=True, cap=SpendCap(0.01), bridge=bridge)
    with pytest.raises(CapExceeded, match="Refusing to sign"):
        p.fetch()
    assert bridge.calls == [], "the cap let a call through to the signer"


def test_the_cap_is_cumulative_across_a_fleet_not_per_provider():
    """One SpendCap shared by several providers is what makes it a RUN cap."""
    cap = SpendCap(0.06)  # exactly three calls at 0.02
    bridge = FakeBridge(paid(forecast("measured", 0.3)))
    fleet = [provider(live=True, cap=cap, bridge=bridge) for _ in range(3)]
    for p in fleet:
        p.fetch()
    assert cap.spent == 0.06
    assert cap.remaining == 0.0
    # Spending the ceiling exactly is allowed; the call after it is not, and no
    # single provider had spent more than 0.02 of its own.
    with pytest.raises(CapExceeded):
        fleet[0].fetch()
    assert len(bridge.calls) == 3, "the refused call must not reach the signer"


def test_providers_without_an_explicit_cap_share_ONE_run_ceiling():
    """Found in tools/quote.py, which built six providers and so six caps: a
    0.05 ceiling silently became 0.05 per provider, or 0.30 for the fleet,
    with every provider believing it was obeying the limit."""
    from cassum.x402 import default_cap, reset_default_cap

    reset_default_cap()
    try:
        a = provider("/api/sol-price")
        b = provider("/api/squeeze-score")
        assert a.cap is b.cap is default_cap()
        assert a.cap.limit == 0.05
    finally:
        reset_default_cap()


def test_an_unreadable_cap_does_not_explode_at_construction_time(monkeypatch):
    """The ceiling is read lazily, so a bad CASSUM_MAX_USDC surfaces when
    something tries to spend rather than when the module is imported."""
    from cassum.x402 import reset_default_cap

    monkeypatch.setenv(CAP_ENV, "banana")
    reset_default_cap()
    try:
        p = provider()  # must not raise
        with pytest.raises(CapExceeded, match="not a number"):
            _ = p.cap
    finally:
        reset_default_cap()


def test_the_cap_comes_from_one_env_var_with_a_documented_default():
    """Raising the ceiling must be a shell edit, never a code change."""
    assert SpendCap.from_env({}) == 0.05
    assert SpendCap.from_env({CAP_ENV: "0.01"}) == 0.01
    assert SpendCap.from_env({CAP_ENV: ""}) == 0.05
    with pytest.raises(CapExceeded):
        SpendCap.from_env({CAP_ENV: "not-a-number"})
    with pytest.raises(CapExceeded):
        SpendCap.from_env({CAP_ENV: "-1"})


def test_an_amount_the_bridge_could_not_determine_is_not_guessed():
    """MEASURED 2026-09-01: the EVM bridge reported paidUsd 0.05 for a 0.003
    call, because it fell back to --max-usd, which is a CEILING and not a price.
    The chain charged 0.003. A wrong usdc_spent goes straight into memory and
    effective_cost divides by it, so an undetermined amount must refuse."""
    bridge = FakeBridge({"ok": True, "status": 200, "data": forecast("measured", 0.3),
                         "paidUsd": None, "settlement": {"transaction": "0xabc"},
                         "spentUsd": 0, "payer": "0x", "error": None})
    p = provider(live=True, cap=SpendCap(1.0), bridge=bridge)
    with pytest.raises(BridgeError, match="could not determine the amount"):
        p.fetch()
    assert p.cap.spent == 0.0


def test_a_charge_above_the_quote_is_refused():
    """The quote came off the payment-required header. Anything larger means the
    two disagree, and recording it would understate the endpoint's real cost."""
    bridge = FakeBridge({"ok": True, "status": 200, "data": forecast("measured", 0.3),
                         "paidUsd": 0.05, "settlement": {"transaction": "0xabc"},
                         "spentUsd": 0, "payer": "0x", "error": None})
    p = provider(live=True, cap=SpendCap(1.0), bridge=bridge)   # quoted at 0.02
    with pytest.raises(BridgeError, match="exceeds its quote"):
        p.fetch()
    assert p.cap.spent == 0.0


def test_a_bridge_failure_raises_rather_than_inventing_an_empty():
    """plugin service.ts returns early on a non-2xx without reporting paidUsd,
    so spend is genuinely unknown. Recording (False, 0.0) would understate cost
    and (False, price) would overstate it; both are fabricated data points."""
    bridge = FakeBridge({"ok": False, "status": 500, "data": None,
                         "paidUsd": 0, "error": "AgentFeed HTTP 500"})
    p = provider(live=True, cap=SpendCap(1.0), bridge=bridge)
    with pytest.raises(BridgeError, match="UNKNOWN"):
        p.fetch()
    assert p.cap.spent == 0.0, "an unknown outcome must not move the ledger"


def test_pricing_and_settlement_are_separate_rails():
    """A provider priced on Base settles on whichever rail the configured
    bridge can sign. Conflating the two is how a run gets signed against the
    wrong chain."""
    p = provider(live=True, cap=SpendCap(1.0), rail="solana",
                 bridge=FakeBridge(paid(forecast("measured", 0.3))))
    assert p.network == BASE_RAIL
    assert p.settlement_rail().network.startswith("solana:")
    assert p.settlement_rail().usdc == p.price, "both rails quote the same amount"


def test_the_base_rail_settles_on_eip155():
    p = provider(live=True, cap=SpendCap(1.0), rail="base",
                 bridge=FakeBridge(paid(forecast("measured", 0.3))))
    assert p.rail.name == "base"
    assert p.settlement_rail().network == BASE_RAIL
    assert p.settlement_rail().pay_to == BASE_PAY_TO


def test_the_rail_defaults_to_solana_and_switches_on_one_env_var():
    """Changing the chain money moves on must be a deliberate shell edit, never
    a side effect of upgrading this package."""
    assert rail_from_env({}).name == "solana"
    assert rail_from_env({RAIL_ENV: "base"}).name == "base"
    assert rail_from_env({RAIL_ENV: "solana-plugin"}).name == "solana-plugin"
    assert rail_from_env({RAIL_ENV: "BASE"}).name == "base"
    with pytest.raises(BridgeError, match="not a known rail"):
        rail_from_env({RAIL_ENV: "ethereum"})


def test_each_rail_names_its_own_bridge_and_directory():
    solana, base = SETTLEMENT_RAILS["solana"], SETTLEMENT_RAILS["base"]
    plugin = SETTLEMENT_RAILS["solana-plugin"]
    assert solana.script.name == "pay_bridge_svm.mjs"
    assert base.script.name == "pay_bridge_evm.mjs"
    assert plugin.script.name == "pay_bridge.mjs"
    assert all(r.script.exists() for r in (solana, base, plugin))
    assert solana.dir_env == base.dir_env == "CASSUM_WALLET_DIR"
    assert plugin.dir_env == "CASSUM_BRIDGE_DIR"
    # cwd matters only where the payer is resolved as a bare specifier.
    assert solana.dir_is_cwd is False and base.dir_is_cwd is False
    assert plugin.dir_is_cwd is True


def test_both_solana_rails_settle_on_an_svm_network():
    """Two clients, one rail. `solana` drives x402-wallet, `solana-plugin`
    drives the published elizaOS client; both must pick the SVM accepts entry."""
    for name in ("solana", "solana-plugin"):
        p = provider(live=True, cap=SpendCap(1.0), rail=name)
        assert p.settlement_rail().network.startswith("solana:")
        assert p.rail.network == "solana:"


def test_the_bridge_is_selected_by_rail_not_hardcoded():
    bridge = FakeBridge(paid(forecast("measured", 0.3)))
    provider(live=True, cap=SpendCap(1.0), rail="base", bridge=bridge).fetch()
    assert bridge.rails == ["base"]


def test_a_rail_the_challenge_does_not_offer_is_refused():
    """If the server ever stops quoting a rail, the bridge for it must refuse
    rather than sign against whatever is left."""
    q = quote_from_fixture("/api/sol-price")
    svm_only = Quote(path=q.path, resource_url=q.resource_url, description=q.description,
                     tags=q.tags, x402_version=q.x402_version, raw=q.raw,
                     rails=tuple(r for r in q.rails if r.network.startswith("solana:")))
    p = X402Provider("/api/sol-price", quote_=svm_only, network="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                     live=True, rail="base", cap=SpendCap(1.0))
    with pytest.raises(BridgeError, match="cannot pay it"):
        p.settlement_rail()


def test_live_base_fetch_without_a_wallet_directory_fails_loudly():
    p = provider(live=True, cap=SpendCap(1.0), rail="base")
    with pytest.raises(BridgeError, match="CASSUM_WALLET_DIR"):
        p.fetch()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_the_real_evm_bridge_honours_the_same_json_contract(tmp_path, monkeypatch):
    """End to end into pay_bridge_evm.mjs WITHOUT spending: an empty directory
    holds no payer key, so the bridge refuses before it loads a signer."""
    monkeypatch.setenv("CASSUM_WALLET_DIR", str(tmp_path))
    monkeypatch.delenv("EVM_PAYER", raising=False)
    p = provider(live=True, cap=SpendCap(1.0), rail="base")
    with pytest.raises(BridgeError, match="no payer key"):
        p.fetch()
    assert p.cap.spent == 0.0


def test_the_bridge_is_told_the_per_call_ceiling():
    """The per-call guard inside the JS client is independent of the run cap;
    neither subsumes the other, so both must actually be wired."""
    bridge = FakeBridge(paid(forecast("measured", 0.3)))
    provider(live=True, cap=SpendCap(1.0), bridge=bridge).fetch()
    assert bridge.calls == [("/api/cascade-forecast", 0.02)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_the_real_plugin_bridge_honours_its_json_contract(tmp_path, monkeypatch):
    """End to end into the actual Node script, WITHOUT spending: an empty
    directory cannot resolve @seekdaseek/plugin-agentfeed, so the bridge fails
    at import and can never reach a signer. What is under test is that its
    one-line JSON contract survives the process boundary."""
    monkeypatch.setenv("CASSUM_BRIDGE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTFEED_PRIVATE_KEY", "not-a-real-key-and-never-parsed")
    p = provider(live=True, cap=SpendCap(1.0), rail="solana-plugin")
    with pytest.raises(BridgeError, match="cannot resolve"):
        p.fetch()
    assert p.cap.spent == 0.0


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
@pytest.mark.parametrize("rail", ["solana", "base"])
def test_the_wallet_bridges_honour_the_same_json_contract(tmp_path, monkeypatch, rail):
    """Same, for the two x402-wallet bridges. An empty directory holds no payer
    key, so each refuses before it loads a signer."""
    monkeypatch.setenv("CASSUM_WALLET_DIR", str(tmp_path))
    monkeypatch.delenv("EVM_PAYER", raising=False)
    monkeypatch.delenv("SOLANA_PAYER", raising=False)
    p = provider(live=True, cap=SpendCap(1.0), rail=rail)
    with pytest.raises(BridgeError, match="no payer key"):
        p.fetch()
    assert p.cap.spent == 0.0


def test_live_fetch_without_a_bridge_directory_fails_loudly():
    """No silent fallback to some other install. If the operator has not said
    where the payer lives, nothing is signed -- and each rail names ITS OWN
    directory variable, so the operator is told which one is missing."""
    with pytest.raises(BridgeError, match="CASSUM_WALLET_DIR"):
        provider(live=True, cap=SpendCap(1.0), rail="solana").fetch()
    with pytest.raises(BridgeError, match="CASSUM_BRIDGE_DIR"):
        provider(live=True, cap=SpendCap(1.0), rail="solana-plugin").fetch()


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
