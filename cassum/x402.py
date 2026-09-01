"""A real paid provider behind the same interface as `sim.SimProvider`.

`sim.SimProvider` stays exactly as it is -- it is the deterministic control the
RESULTS.md negative depends on. This module offers the same three members:
`.name`, `.price`, `.fetch() -> (delivered, usdc)`, so `Router` cannot tell the
two apart.

Two stages, and the first does not spend.

STAGE 1, DISCOVERY, live and tested. `quote(path)` performs an unpaid GET,
reads the 402 challenge, and returns the price, description and tags the server
itself advertises. The Router's `.price` comes from that quote and from nothing
else. A price in a config file is a price nobody re-checked; a stale one makes
`effective_cost` wrong in exactly the direction that costs money.

STAGE 2, PAYMENT, NOT IMPLEMENTED. Settlement is behind `live=True` and refuses
to run, because signing spends real USDC from a treasury and the signer is a
decision for the treasury holder, not for this file. See `PaymentNotImplemented`
for the precise list of what is missing.

MEASURED 2026-09-01 against https://x402.ochinimus.app, recorded verbatim in
tests/fixtures/ by tools/record_402.py:
  - an unpaid GET returns HTTP 402 with body "{}"
  - the quote is base64 JSON in the `payment-required` HEADER, never the body
  - x402Version 2; `resource` carries url/description/tags; `accepts[]` holds
    one entry per rail
  - the Base rail is network eip155:8453, asset 0x8335..2913, and `amount` is
    in USDC base units at 6dp, so "3000" is 0.003 USDC

No new runtime dependency: this uses `urllib` from the standard library.
`httpx` is present in the venv only as a transitive dependency of
sibyl-memory-client, and importing it here would make it an undeclared one.
"""
from __future__ import annotations

import base64
import binascii
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BASE_URL = "https://x402.ochinimus.app"
CHALLENGE_HEADER = "payment-required"

# MEASURED 2026-09-01: the origin is behind Cloudflare, which answers the
# default `Python-urllib/3.12` User-Agent with 403 and no challenge header at
# all -- not 402. Identifying ourselves gets the real 402 back. Any stdlib x402
# client hits this and will misread it as "the endpoint is down".
USER_AGENT = "cassum/0.1 (+https://github.com/seekdaseek/cassum)"
REQUEST_HEADERS = {"Accept": "application/json", "User-Agent": USER_AGENT}
BASE_RAIL = "eip155:8453"
SOLANA_RAIL_PREFIX = "solana:"
USDC_DECIMALS = 6
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# The endpoints recorded under tests/fixtures/. Chosen for spread, not
# convenience: cheapest and dearest, the documented 3000-base-unit case, a path
# parameter, and the endpoint whose decline is this adapter's reference case.
RECORDED_PATHS = (
    "/api/sol-price",
    "/api/market-snapshot",
    "/api/liquidations",
    "/api/cascade-forecast",
    "/api/squeeze-score",
    "/api/token-risk/So11111111111111111111111111111111111111112",
)


class X402Error(Exception):
    """Anything wrong with a challenge or a paid response."""


class PaymentNotImplemented(X402Error):
    """Raised instead of spending. Deliberate, and not a TODO to be silenced."""


# Routes whose last segment is an ARGUMENT, not the endpoint. Read off the
# service catalogue at GET / : these are the `:mint` and `:wallet` routes.
PARAM_ROUTES = (
    "/api/token-risk/",
    "/api/token-metadata/",
    "/api/token-holders/",
    "/api/wallet-holdings/",
    "/api/wallet-activity/",
)


def endpoint_name(path: str) -> str:
    """The provider's identity, which is the ENDPOINT and never its argument.

    `Router` keys memory on `provider.name`. Naming a parameterised route after
    its mint would file every mint as a separate provider, so no provider would
    ever reach `min_calls` and nothing would leave LEARNING -- memory would
    accumulate and never once change a decision.
    """
    trimmed = path.rstrip("/")
    for prefix in PARAM_ROUTES:
        if trimmed.startswith(prefix):
            return prefix.strip("/").rsplit("/", 1)[-1]
    return trimmed.rsplit("/", 1)[-1]


def fixture_name(path: str) -> str:
    """`/api/token-risk/So111..` -> `api-token-risk-So111..json`. Stable, so a
    re-record overwrites rather than accumulating."""
    return path.strip("/").replace("/", "-") + ".json"


# --- the challenge ----------------------------------------------------------

@dataclass(frozen=True)
class Rail:
    """One entry of `accepts[]`. One way to pay for the same resource."""

    scheme: str
    network: str
    amount_base_units: str
    asset: str
    pay_to: str
    max_timeout_seconds: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def usdc(self) -> float:
        """MEASURED: `amount` is a decimal STRING in base units at 6dp.
        Parsed as int, never float, so "3000" cannot arrive as 2999.9999."""
        return int(self.amount_base_units) / (10**USDC_DECIMALS)


@dataclass(frozen=True)
class Quote:
    """What the server says this endpoint costs and returns, unpaid."""

    path: str
    resource_url: str
    description: str
    tags: tuple[str, ...]
    rails: tuple[Rail, ...]
    x402_version: int
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def rail(self, network: str = BASE_RAIL) -> Rail:
        for r in self.rails:
            if r.network == network:
                return r
        offered = ", ".join(r.network for r in self.rails) or "none"
        raise X402Error(f"{self.path}: no {network} rail. Offered: {offered}")

    @property
    def price(self) -> float:
        """Router-facing price, in USDC, off the Base rail."""
        return self.rail(BASE_RAIL).usdc

    def price_on(self, network: str) -> float:
        return self.rail(network).usdc


def decode_challenge_header(value: str) -> dict[str, Any]:
    """MEASURED: base64 JSON in the header. Padding is restored because a
    stripped '=' is a normal thing for a header value to lose in transit."""
    raw = value.strip()
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as err:
        raise X402Error(f"{CHALLENGE_HEADER} is not valid base64: {err}") from err
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError as err:
        raise X402Error(f"{CHALLENGE_HEADER} did not decode to JSON: {err}") from err
    if not isinstance(payload, dict):
        raise X402Error(f"{CHALLENGE_HEADER} decoded to {type(payload).__name__}, not an object")
    return payload


def parse_challenge(status: int, headers: dict[str, str], path: str) -> Quote:
    """Turn one 402 response into a Quote. Header lookup is case-insensitive
    because HTTP header names are, and the casing has already varied in
    practice between curl and urllib."""
    if status != 402:
        raise X402Error(f"{path}: expected HTTP 402, got {status}")
    lowered = {k.lower(): v for k, v in headers.items()}
    if CHALLENGE_HEADER not in lowered:
        raise X402Error(f"{path}: 402 carried no {CHALLENGE_HEADER} header")

    payload = decode_challenge_header(lowered[CHALLENGE_HEADER])
    resource = payload.get("resource") or {}
    accepts = payload.get("accepts") or []
    if not accepts:
        raise X402Error(f"{path}: challenge offers no rails, so it cannot be paid")

    rails = tuple(
        Rail(
            scheme=a.get("scheme", ""),
            network=a.get("network", ""),
            amount_base_units=str(a.get("amount", "")),
            asset=a.get("asset", ""),
            pay_to=a.get("payTo", ""),
            max_timeout_seconds=a.get("maxTimeoutSeconds"),
            extra=a.get("extra") or {},
        )
        for a in accepts
    )
    return Quote(
        path=path,
        resource_url=resource.get("url", ""),
        description=resource.get("description", ""),
        tags=tuple(resource.get("tags") or ()),
        rails=rails,
        x402_version=int(payload.get("x402Version", 0)),
        raw=payload,
    )


def quote(path: str, *, base_url: str = BASE_URL, timeout: int = 30) -> Quote:
    """LIVE, unpaid. Sends no X-PAYMENT header, so the server can only answer
    402 and nothing settles. The test suite never calls this."""
    req = urllib.request.Request(
        f"{base_url}{path}", method="GET", headers=dict(REQUEST_HEADERS)
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raise X402Error(
                f"{path}: expected a 402 challenge, got {resp.status}. "
                "A free endpoint has no price to quote."
            )
    except urllib.error.HTTPError as err:
        return parse_challenge(err.code, dict(err.headers), path)


def quote_from_fixture(path: str, *, fixture_dir: Path = FIXTURE_DIR) -> Quote:
    """The same parse, fed from a recorded challenge. This is what tests use."""
    data = json.loads((fixture_dir / fixture_name(path)).read_text())
    return parse_challenge(data["status"], data["headers"], path)


# --- usability: the part that decides `delivered` ----------------------------
#
# HTTP 200 is not delivery. The buyer paid for a usable answer, and every
# endpoint here can return 200 carrying nothing usable. Each predicate below is
# written against the SERVER SOURCE, cited by file, not against a guess at the
# field names -- agentfeed's own answer.js records that its first version was
# written against guessed names and returned null for two tools.

ENVELOPE_TOOL = "tool"
ENVELOPE_DATA = "data"


def _data(payload: Any) -> dict[str, Any]:
    """MEASURED on the free endpoint /api/fear-greed: the success envelope is
    {"tool": name, "data": {...}, "paid": true}. Documented as the route
    wrapper's contract in agentfeed answer.js. A payload that is not that shape
    is not usable, whatever its status code."""
    if not isinstance(payload, dict):
        return {}
    inner = payload.get(ENVELOPE_DATA)
    return inner if isinstance(inner, dict) else {}


# Evidence states, from caliper lib/answer.mjs. Its own comment: "Three cases,
# and collapsing any two of them would be a lie."
#   measured   -> a probability. The buyer got the answer they paid for.
#   absent     -> we cover this symbol and it genuinely has no volume. A true
#                 statement about the world, actionable, so it IS delivery.
#   unmeasured -> OUR gap: thin history, unknown symbol, model unavailable.
#                 The provider declined. NOT delivery, and this is the case the
#                 whole adapter exists to price correctly.
USABLE_EVIDENCE = frozenset({"measured", "absent"})
DECLINED_EVIDENCE = "unmeasured"


def usable_cascade_forecast(payload: Any) -> bool:
    """get_cascade_forecast, THE REFERENCE CASE.

    Source: agentfeed tools/cascade-forecast.js -> caliper lib/answer.mjs.
    Shape: data.answers[] with one entry per requested symbol, each carrying
    `evidence` and `p`. A decline is 200 + evidence "unmeasured" + p null, with
    a reason such as "only 3 windows of history for this symbol, minimum is 8".

    A batch is delivered if AT LEAST ONE answer is usable: one call was paid
    for, and one usable answer is worth the fee. Requiring all of them would
    charge the provider for symbols the buyer chose to add speculatively.
    """
    answers = _data(payload).get("answers")
    if not isinstance(answers, list) or not answers:
        return False
    return any(
        isinstance(a, dict) and a.get("evidence") in USABLE_EVIDENCE for a in answers
    )


def usable_price(payload: Any) -> bool:
    """get_sol_price / get_btc_price.

    Source: agentfeed answer.js SHAPERS.price, verified field order
    price > usd > value. A number that is not finite and positive is not a
    price, so a null or a zero is a non-delivery rather than a free quote.
    """
    data = _data(payload)
    for key in ("price", "usd", "value"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    return False


def usable_squeeze_score(payload: Any) -> bool:
    """get_squeeze_score.

    Source: agentfeed answer.js SHAPERS.squeeze, VERIFIED against
    tools/liqdb.js getSqueezeScore, which returns BOTH short_squeeze_score and
    long_flush_score. Zero is a legitimate score, so this tests presence and
    numeric type, NOT truthiness -- `if not score` would discard a real 0.
    """
    data = _data(payload)
    for key in ("short_squeeze_score", "squeeze_score", "score"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
    return False


def usable_liquidations(payload: Any) -> bool:
    """get_recent_liquidations, and the liq-history/heatmap family.

    Source: agentfeed tools/liqdb.js. An empty window is returned as 200 with
    an empty list and an explanatory note, e.g.
    `{symbol, hours, levels: [], note: 'no liquidations recorded in window'}`.
    That is the "200 with an empty result set" the brief names: the tape was
    read and had nothing in it, so there is no answer to act on.
    """
    data = _data(payload)
    for key in ("liquidations", "events", "buckets", "levels", "rows"):
        value = data.get(key)
        if isinstance(value, list):
            return len(value) > 0
    return False


def usable_token_risk(payload: Any) -> bool:
    """get_token_risk.

    Source: agentfeed answer.js SHAPERS.tokenRisk, VERIFIED against
    tools/tokenrisk.js: there is NO risk score in the payload. What exists is
    risk_flags (a string array) and risk_flag_count set unconditionally to
    flags.length. So an EMPTY flag list is a real answer -- "nothing wrong with
    this mint" -- and delivery is decided on the count being PRESENT, not on it
    being non-zero. Testing the list for emptiness here would mark every clean
    token as a non-delivery.
    """
    data = _data(payload)
    if isinstance(data.get("risk_flag_count"), int):
        return True
    return isinstance(data.get("risk_flags"), list)


def usable_nonempty_envelope(payload: Any) -> bool:
    """The default for an endpoint with no predicate of its own.

    Deliberately weak and deliberately named: it only proves the envelope
    arrived with a non-empty `data` object. It cannot tell a real answer from a
    declined one, so anything routed on this default will UNDER-report empties
    and make the provider look better than it is. Write a real predicate before
    spending against a new endpoint.
    """
    return bool(_data(payload))


USABILITY: dict[str, Callable[[Any], bool]] = {
    "/api/sol-price": usable_price,
    "/api/btc-price": usable_price,
    "/api/cascade-forecast": usable_cascade_forecast,
    "/api/squeeze-score": usable_squeeze_score,
    "/api/liquidations": usable_liquidations,
    "/api/liq-history": usable_liquidations,
    "/api/liq-heatmap": usable_liquidations,
    "/api/cascade-history": usable_liquidations,
}

# Path-parameter routes are matched on their prefix, since the mint or wallet
# is part of the path rather than the query string.
USABILITY_PREFIX: dict[str, Callable[[Any], bool]] = {
    "/api/token-risk/": usable_token_risk,
}
assert set(USABILITY_PREFIX) <= set(PARAM_ROUTES), "a prefix predicate names an unknown route"


def predicate_for(path: str) -> Callable[[Any], bool]:
    if path in USABILITY:
        return USABILITY[path]
    for prefix, fn in USABILITY_PREFIX.items():
        if path.startswith(prefix):
            return fn
    return usable_nonempty_envelope


def is_usable(path: str, payload: Any) -> bool:
    """Did the buyer get something worth the fee? Pure, offline, and the only
    thing that decides `delivered`."""
    return predicate_for(path)(payload)


# --- the provider -----------------------------------------------------------

class X402Provider:
    """Same three members as `sim.SimProvider`, so `Router` cannot tell them
    apart: `.name`, `.price`, `.fetch() -> (delivered, usdc)`.

    `.price` is whatever the 402 challenge said at construction time. Refresh
    it with `refresh_price()` rather than editing a constant.
    """

    def __init__(
        self,
        path: str,
        *,
        quote_: Quote,
        name: str | None = None,
        live: bool = False,
        base_url: str = BASE_URL,
        network: str = BASE_RAIL,
    ) -> None:
        self.path = path
        self.quote = quote_
        self.name = name or endpoint_name(path)
        self.live = live
        self.base_url = base_url
        self.network = network
        self.price = quote_.price_on(network)

    @classmethod
    def discover(
        cls, path: str, *, live: bool = False, base_url: str = BASE_URL,
        network: str = BASE_RAIL, name: str | None = None,
    ) -> "X402Provider":
        """LIVE discovery, no payment. The price comes from the challenge."""
        return cls(
            path, quote_=quote(path, base_url=base_url), name=name,
            live=live, base_url=base_url, network=network,
        )

    @property
    def description(self) -> str:
        return self.quote.description

    @property
    def tags(self) -> tuple[str, ...]:
        return self.quote.tags

    def refresh_price(self) -> float:
        """Re-quote. A price that moved is a fact the Router should learn from
        the server, not from a redeploy of this repo."""
        self.quote = quote(self.path, base_url=self.base_url)
        self.price = self.quote.price_on(self.network)
        return self.price

    def decide(self, payload: Any) -> bool:
        """The usability predicate, exposed so it is testable without paying."""
        return is_usable(self.path, payload)

    def fetch(self) -> tuple[bool, float]:
        """STAGE 2, NOT IMPLEMENTED. Charged either way is the point of the
        interface, so a half-built payment path that silently returns
        (False, price) would fabricate an empty_rate out of our own bug and
        teach the Router to condemn a provider that was never called."""
        if not self.live:
            raise PaymentNotImplemented(
                f"{self.name}: fetch() needs live=True, which is off by default. "
                "Discovery (quote/price/decide) works without it."
            )
        raise PaymentNotImplemented(
            f"{self.name}: settlement is not implemented and this call would spend "
            f"{self.price} USDC of real treasury funds.\n"
            "Missing, all three deliberately absent until the treasury holder decides:\n"
            f"  1. a signer for the {self.network} rail, paying to "
            f"{self.quote.rail(self.network).pay_to}\n"
            "  2. the X-PAYMENT header construction for x402Version "
            f"{self.quote.x402_version}, scheme {self.quote.rail(self.network).scheme}\n"
            "  3. a spend cap enforced against the quoted amount BEFORE signing, "
            "reading the amount from the payment-required header rather than a config."
        )
