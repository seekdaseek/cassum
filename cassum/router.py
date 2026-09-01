"""The core function: decide who to pay, using what memory says they delivered."""
from __future__ import annotations

from typing import Any, Protocol

UNTRIED = "untried"
LEARNING = "learning"
TRUSTED = "trusted"
CONDEMNED = "condemned"


class StoreLike(Protocol):
    def get_provider(self, name: str) -> dict[str, Any] | None: ...
    def record_purchase(self, provider: str, **kw: Any) -> str: ...


class Router:
    def __init__(
        self,
        store: StoreLike,
        providers: list[Any],
        *,
        threshold: float = 0.5,
        min_calls: int = 3,
    ) -> None:
        self.store = store
        self.providers = providers
        self.threshold = threshold
        self.min_calls = min_calls

    def verdict(self, name: str) -> tuple[str, dict[str, Any]]:
        """ABSENT is not the same as BAD. A provider never bought from has
        earned nothing, good or ill, and gets an exploratory call."""
        rec = self.store.get_provider(name)
        if rec is None:
            return UNTRIED, {}
        body = dict(rec.get("body") or {})
        calls = int(body.get("calls", 0))
        if calls < self.min_calls:
            return LEARNING, body
        if float(body.get("empty_rate", 0.0)) >= self.threshold:
            return CONDEMNED, body
        return TRUSTED, body

    def effective_cost(self, price: float, body: dict[str, Any]) -> float:
        """Price per DELIVERED payload. The per-call price is the number that
        lies; this one only exists because memory measured the empty rate."""
        rate = float(body.get("empty_rate", 0.0))
        if rate >= 1.0:
            return float("inf")
        return price / (1.0 - rate)

    def _rank(self, p: Any, state: str, body: dict[str, Any]) -> tuple[int, float, str]:
        if state == UNTRIED:
            return (0, p.price, p.name)
        if state == LEARNING:
            return (1, p.price, p.name)
        return (2, round(self.effective_cost(p.price, body), 12), p.name)

    def choose(self) -> tuple[Any | None, str]:
        live = []
        for p in self.providers:
            state, body = self.verdict(p.name)
            if state == CONDEMNED:
                continue
            live.append((self._rank(p, state, body), p, state, body))
        if not live:
            return None, "every provider condemned by measured empty rate"
        live.sort(key=lambda t: t[0])
        _, p, state, body = live[0]
        if state == TRUSTED:
            eff = self.effective_cost(p.price, body)
            return p, f"{p.name} at {p.price}/call, {eff:.5f}/payload ({state})"
        return p, f"{p.name} at {p.price}/call ({state}, {int(body.get('calls', 0))} prior)"

    def buy_one(self) -> tuple[bool, float, str]:
        p, reason = self.choose()
        if p is None:
            return False, 0.0, reason
        considered = [f"{q.name}:{self.verdict(q.name)[0]}" for q in self.providers]
        delivered, usdc = p.fetch()
        # fetch() returns (delivered, usdc) for EVERY provider -- that is the
        # interface sim and x402 share and it does not change. A real payment
        # leaves its receipt on the provider as a side channel; a simulated one
        # has no such attribute and journals nothing extra.
        self.store.record_purchase(
            p.name,
            delivered=delivered,
            usdc=usdc,
            considered=considered,
            next_step=[] if delivered else [f"re-evaluate {p.name}"],
            note=reason,
            settlement=getattr(p, "settlement", None),
        )
        return delivered, usdc, reason

    def collect(self, wanted: int, *, max_calls: int = 500) -> dict[str, Any]:
        """Buy until `wanted` usable payloads are in hand. Cost is the metric."""
        got = spent = calls = 0
        while got < wanted and calls < max_calls:
            delivered, usdc, _ = self.buy_one()
            calls += 1
            spent += usdc
            if delivered:
                got += 1
            if usdc == 0.0 and not delivered:
                break
        return {
            "delivered": got,
            "calls": calls,
            "usdc": round(spent, 8),
            "usdc_per_payload": round(spent / got, 8) if got else None,
        }
