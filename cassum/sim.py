"""Deterministic fake providers. No network, no spend, reproducible."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SimProvider:
    name: str
    price: float
    pattern: list[bool]
    capability: str = "price"
    _i: int = field(default=0, repr=False)

    def fetch(self) -> tuple[bool, float]:
        """Returns (delivered, usdc_charged). Charged either way, which is the point."""
        delivered = self.pattern[self._i % len(self.pattern)]
        self._i += 1
        return delivered, self.price


def default_fleet() -> list[SimProvider]:
    """Real dispersion. Cheapest per call is dearest per payload.

    hollow-cheap 0.003 x 4 calls = 0.012 per payload
    mid          0.004 x 2 calls = 0.008 per payload
    steady-dear  0.005 x 1 call  = 0.005 per payload
    """
    return [
        SimProvider("hollow-cheap", 0.003, [False, False, False, True]),
        SimProvider("mid", 0.004, [True, False]),
        SimProvider("steady-dear", 0.005, [True]),
    ]


def flat_fleet() -> list[SimProvider]:
    """No edge to find: every provider costs 0.004 per delivered payload.
    Memory is pure overhead here and the tests say so."""
    return [
        SimProvider("a", 0.001, [False, False, False, True]),
        SimProvider("b", 0.002, [True, False]),
        SimProvider("c", 0.004, [True]),
    ]
