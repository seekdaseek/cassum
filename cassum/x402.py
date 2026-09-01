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

STAGE 2, PAYMENT, behind `live=True` which is off by default. NO SIGNING CODE
LIVES IN THIS REPO. Each rail shells out to a JS payer that already has a live
mainnet settlement on that rail, and this module only translates its result:
  solana  @seekdaseek/plugin-agentfeed  via tools/pay_bridge.mjs
  base    @seekdaseek/x402-wallet       via tools/pay_bridge_evm.mjs
Chosen with `CASSUM_RAIL`, default `solana`. Pricing and settlement are separate
choices: `network` decides which rail is QUOTED, `rail` decides which is PAID.

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
import os
import shutil
import subprocess
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


# --- settlement rails -------------------------------------------------------
#
# Pricing and settlement are separate choices. A provider is PRICED on whatever
# rail you ask for (they quote identically), and SETTLED on whichever rail the
# configured bridge can actually sign for. Conflating the two is how a run gets
# signed against the wrong chain.
#
# Neither bridge contains signing code. Each shells out to a JS payer:
#   solana  @seekdaseek/plugin-agentfeed  (@solana/kit, ExactSvmScheme, bs58)
#   base    @seekdaseek/x402-wallet       (viem signTypedData, ExactEvmScheme)
# Both already have live mainnet settlements; see FINDINGS.md.
_TOOLS = Path(__file__).resolve().parent.parent / "tools"


@dataclass(frozen=True)
class RailBridge:
    """One way to actually pay. `network` is matched as a PREFIX against the
    rails a challenge offers."""

    name: str
    network: str
    script: Path
    dir_env: str
    dir_is_cwd: bool

    def directory(self, env: dict[str, str] | None = None) -> str:
        value = (env if env is not None else os.environ).get(self.dir_env)
        if not value:
            raise BridgeError(
                f"{self.dir_env} is not set, so the {self.name} bridge cannot run. "
                f"{self.hint}"
            )
        return value

    @property
    def hint(self) -> str:
        if self.name == "base":
            return "Point it at the @seekdaseek/x402-wallet checkout."
        return "Point it at a directory where @seekdaseek/plugin-agentfeed is installed."


SETTLEMENT_RAILS: dict[str, RailBridge] = {
    # cwd matters: the plugin is resolved as a bare specifier from there.
    "solana": RailBridge("solana", SOLANA_RAIL_PREFIX, _TOOLS / "pay_bridge.mjs",
                         "CASSUM_BRIDGE_DIR", dir_is_cwd=True),
    # the wallet library is resolved by absolute path, so cwd is irrelevant.
    "base": RailBridge("base", BASE_RAIL, _TOOLS / "pay_bridge_evm.mjs",
                       "CASSUM_WALLET_DIR", dir_is_cwd=False),
}

RAIL_ENV = "CASSUM_RAIL"
# Deliberately unchanged when nothing is set. Switching the chain money moves on
# is not something a library upgrade should do silently; base is one env var away.
DEFAULT_RAIL = "solana"

# The cumulative ceiling for one run, in USDC. ONE env var, so raising it is a
# shell edit and never a code change.
CAP_ENV = "CASSUM_MAX_USDC"
DEFAULT_CAP_USDC = 0.05


def rail_from_env(env: dict[str, str] | None = None) -> RailBridge:
    name = (env if env is not None else os.environ).get(RAIL_ENV) or DEFAULT_RAIL
    try:
        return SETTLEMENT_RAILS[name.strip().lower()]
    except KeyError:
        raise BridgeError(
            f"{RAIL_ENV}={name!r} is not a known rail. "
            f"Choose one of: {', '.join(sorted(SETTLEMENT_RAILS))}."
        ) from None


class X402Error(Exception):
    """Anything wrong with a challenge or a paid response."""


class PaymentNotImplemented(X402Error):
    """Raised instead of spending. Deliberate, and not a TODO to be silenced."""


class CapExceeded(X402Error):
    """The run's cumulative ceiling would be broken by the next call."""


class BridgeError(X402Error):
    """The payment bridge could not complete. NOT a delivery outcome.

    Raised rather than returned because `(False, price)` would invent an empty
    from our own failure and teach the Router to condemn a provider that was
    never asked anything.
    """


# Routes whose last segment is an ARGUMENT, not the endpoint. Read off the
# service catalogue at GET / : these are the `:mint` and `:wallet` routes.
PARAM_ROUTES = (
    "/api/token-risk/",
    "/api/token-metadata/",
    "/api/token-holders/",
    "/api/wallet-holdings/",
    "/api/wallet-activity/",
)


def route_of(path: str) -> str:
    """The path without its query string.

    A query is an ARGUMENT, exactly like a `:mint` path segment, and it must not
    reach the provider name for the same reason: Router keys memory on the name,
    so `?symbol=SOL` and `?symbol=BTC` would file as two providers, neither
    reaching min_calls. Same bug as the token-risk one in FINDINGS 10, one layer
    along.
    """
    return path.split("?", 1)[0]


def endpoint_name(path: str) -> str:
    """The provider's identity, which is the ENDPOINT and never its argument.

    `Router` keys memory on `provider.name`. Naming a parameterised route after
    its mint would file every mint as a separate provider, so no provider would
    ever reach `min_calls` and nothing would leave LEARNING -- memory would
    accumulate and never once change a decision.
    """
    trimmed = route_of(path).rstrip("/")
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
    route = route_of(path)
    if route in USABILITY:
        return USABILITY[route]
    for prefix, fn in USABILITY_PREFIX.items():
        if route.startswith(prefix):
            return fn
    return usable_nonempty_envelope


def is_usable(path: str, payload: Any) -> bool:
    """Did the buyer get something worth the fee? Pure, offline, and the only
    thing that decides `delivered`."""
    return predicate_for(path)(payload)


# --- the spend cap ----------------------------------------------------------

class SpendCap:
    """A cumulative ceiling for one run, read from ONE env var.

    Checked against the amount parsed out of the `payment-required` header
    BEFORE the bridge is invoked, so a call that would break the ceiling is
    never signed rather than being refunded afterwards. There is no refund.

    The per-call ceiling inside the JS client is a SEPARATE, independent guard.
    Neither one subsumes the other: per-call cannot stop a thousand cheap calls,
    and cumulative cannot stop one call quoted above what it was quoted at
    preflight. Both are wanted.
    """

    def __init__(self, limit_usdc: float | None = None) -> None:
        self.limit = self.from_env() if limit_usdc is None else float(limit_usdc)
        self.spent = 0.0

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> float:
        raw = (env if env is not None else os.environ).get(CAP_ENV)
        if raw is None or raw.strip() == "":
            return DEFAULT_CAP_USDC
        try:
            value = float(raw)
        except ValueError as err:
            raise CapExceeded(
                f"{CAP_ENV}={raw!r} is not a number. Refusing to spend on an "
                "unreadable ceiling."
            ) from err
        if value < 0:
            raise CapExceeded(f"{CAP_ENV}={raw!r} is negative.")
        return value

    @property
    def remaining(self) -> float:
        return max(0.0, round(self.limit - self.spent, 8))

    def check(self, usdc: float) -> None:
        """Raise if this call would break the ceiling. Call BEFORE paying."""
        if round(self.spent + usdc, 8) > self.limit:
            raise CapExceeded(
                f"{CAP_ENV} is {self.limit} USDC; {self.spent} already spent this run, "
                f"and this call is quoted at {usdc}. Refusing to sign. "
                f"Raise it with: export {CAP_ENV}=<usdc>"
            )

    def record(self, usdc: float) -> None:
        self.spent = round(self.spent + usdc, 8)


_DEFAULT_CAP: SpendCap | None = None


def default_cap() -> SpendCap:
    """The process-wide cap, which is what "per run" means: one interpreter
    invocation is one run.

    A provider that quietly built its OWN cap would turn a 0.05 ceiling into
    0.05 PER PROVIDER, so a six-endpoint fleet could spend 0.30 while every
    provider believed it was obeying the limit. Sharing one object is the only
    thing that makes the ceiling cumulative.
    """
    global _DEFAULT_CAP
    if _DEFAULT_CAP is None:
        _DEFAULT_CAP = SpendCap()
    return _DEFAULT_CAP


def reset_default_cap() -> None:
    """Test hook. Not for production: a run that resets its own ledger has no
    ceiling."""
    global _DEFAULT_CAP
    _DEFAULT_CAP = None


def bridge_command(path: str, *, max_per_call: float, rail: RailBridge) -> list[str]:
    node = shutil.which("node")
    if node is None:
        raise BridgeError("node is not on PATH, so the payment bridge cannot run.")
    if not rail.script.exists():
        raise BridgeError(f"{rail.name} bridge is missing at {rail.script}.")
    return [node, str(rail.script), "--path", path, "--max-usd", str(max_per_call)]


def run_bridge(
    path: str, *, max_per_call: float, rail: RailBridge | None = None, timeout: int = 120
) -> dict[str, Any]:
    """Invoke a Node payer and parse its single line of JSON.

    The child inherits this process's environment, which is how the payer key
    reaches the signer -- AGENTFEED_PRIVATE_KEY on Solana, or a key FILE named
    by EVM_PAYER on Base. Python reads neither, and never logs the environment.
    """
    rail = rail or rail_from_env()
    directory = rail.directory()
    try:
        proc = subprocess.run(
            bridge_command(path, max_per_call=max_per_call, rail=rail),
            cwd=directory if rail.dir_is_cwd else None,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as err:
        raise BridgeError(
            f"{rail.name} bridge timed out after {timeout}s on {path}. Spend is "
            "UNKNOWN: a signature may have been sent. Check the payer before retrying."
        ) from err

    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if not line:
        raise BridgeError(
            f"{rail.name} bridge printed nothing on {path}. "
            f"stderr: {proc.stderr.strip()[:400]}"
        )
    try:
        return json.loads(line)
    except json.JSONDecodeError as err:
        raise BridgeError(f"bridge stdout was not JSON: {line[:200]!r}") from err


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
        cap: "SpendCap | None" = None,
        bridge: Callable[..., dict[str, Any]] | None = None,
        rail: "str | RailBridge | None" = None,
    ) -> None:
        self.path = path
        self.quote = quote_
        self.name = name or endpoint_name(path)
        self.live = live
        self.base_url = base_url
        self.network = network
        self.price = quote_.price_on(network)
        # One cap object shared across a fleet is what makes the ceiling a RUN
        # ceiling rather than a per-provider one. Constructed lazily, so merely
        # importing this module cannot fail on a malformed CASSUM_MAX_USDC.
        self._cap = cap
        self._bridge = bridge or run_bridge
        # Which rail PAYS. Resolved lazily so importing the module cannot fail
        # on an unknown CASSUM_RAIL, and so a fleet built before the env is set
        # still constructs.
        self._rail = SETTLEMENT_RAILS[rail] if isinstance(rail, str) else rail

    @property
    def cap(self) -> SpendCap:
        return self._cap if self._cap is not None else default_cap()

    @cap.setter
    def cap(self, value: SpendCap) -> None:
        self._cap = value

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

    @property
    def rail(self) -> RailBridge:
        """Which bridge will sign. From CASSUM_RAIL unless set explicitly."""
        return self._rail if self._rail is not None else rail_from_env()

    def settlement_rail(self) -> Rail:
        """The offered rail that will actually be signed.

        NOT necessarily self.network: pricing and settlement are separate
        choices, and a provider priced on Base can be paid on Solana or the
        reverse depending on which bridge is configured.
        """
        wanted = self.rail
        for offered in self.quote.rails:
            if offered.network.startswith(wanted.network):
                return offered
        raise BridgeError(
            f"{self.name}: no {wanted.network}* rail offered, so the {wanted.name} "
            f"bridge cannot pay it. Offered: "
            f"{', '.join(r.network for r in self.quote.rails)}"
        )

    def fetch(self) -> tuple[bool, float]:
        """Buy once. Returns (delivered, usdc), same as `sim.SimProvider.fetch`.

        `delivered` is the usability predicate over the payload, never the
        status code. `usdc` is what was actually charged, which is why a paid
        call that returns a decline is (False, price) -- the case this whole
        project exists to measure.
        """
        if not self.live:
            raise PaymentNotImplemented(
                f"{self.name}: fetch() needs live=True, which is off by default. "
                "Discovery (quote/price/decide) works without it."
            )

        rail = self.settlement_rail()
        quoted = rail.usdc
        # BEFORE signing. There is no refund, so an after-the-fact check is a
        # log line, not a guard.
        self.cap.check(quoted)

        result = self._bridge(self.path, max_per_call=quoted, rail=self.rail)

        if not result.get("ok"):
            # MEASURED in plugin src/service.ts: paidGet returns early on a
            # non-2xx and never reports paidUsd, even when a settlement header
            # came back. So spend here is genuinely UNKNOWN and recording
            # either 0.0 or the price would be a fabricated data point.
            raise BridgeError(
                f"{self.name}: {self.rail.name} bridge reported failure, so spend is "
                f"UNKNOWN and no empty_rate is recorded. status={result.get('status')} "
                f"error={result.get('error')}"
            )

        reported = result.get("paidUsd")
        if reported is None:
            # The bridge settled but could not establish the amount. Guessing it
            # would write a fabricated usdc_spent that effective_cost then
            # divides by, so refuse the data point instead.
            raise BridgeError(
                f"{self.name}: settled but the bridge could not determine the amount "
                "charged, so no purchase is recorded. Check the payer."
            )
        paid = float(reported)
        if paid > quoted:
            raise BridgeError(
                f"{self.name}: bridge reports {paid} USDC paid but the challenge "
                f"quoted {quoted}. Refusing to record a purchase that exceeds its quote."
            )
        self.cap.record(paid)
        self.settlement = result.get("settlement")
        delivered = self.decide(result.get("data"))
        return delivered, paid
