"""cassum — buyer-side memory for paid data calls.

Every constant and call shape here was measured against
sibyl-memory-client 0.8.0 on 2026-09-01, not read from the docs.
See probes/probe02-report.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import sibyl_memory_client as _smc
from sibyl_memory_client import MemoryClient

CREDENTIALS = Path.home() / ".sibyl-memory" / "credentials.json"
PROVIDER = "provider"

# MEASURED: tier names are SINGULAR. search(tiers=("entities",)) raises
# ValueError: unknown tiers: entities; valid: entity, state, reference, journal
TIERS = ("entity", "state", "reference", "journal")

# MEASURED: get_entity raises rather than returning None on a miss.
# Import path not yet measured, so resolve it if exported and fall back to
# matching on the class name. Never silently swallow other exceptions.
_NOT_FOUND = getattr(_smc, "NotFoundError", None)


def _is_not_found(exc: BaseException) -> bool:
    if _NOT_FOUND is not None and isinstance(exc, _NOT_FOUND):
        return True
    return type(exc).__name__ == "NotFoundError"


def _credentials() -> dict[str, Any]:
    if not CREDENTIALS.exists():
        return {}
    with CREDENTIALS.open() as f:
        return json.load(f)


# Rails name the transaction differently. MEASURED on Base 2026-09-01: the CDP
# facilitator returns {"success", "payer", "transaction", "network"}. Solana's
# payload is not yet observed from this repo, so the other spellings are
# candidates, not measurements -- which is why an unrecognised payload keeps the
# whole settlement in `extra` rather than being silently dropped.
TX_KEYS = ("transaction", "txHash", "tx_hash", "transactionHash", "signature", "hash")


def tx_hash(settlement: Any) -> str | None:
    if not isinstance(settlement, dict):
        return None
    for key in TX_KEYS:
        value = settlement.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class Store:
    """Thin wrapper. Absence is a value here, never an exception."""

    def __init__(self, client: MemoryClient) -> None:
        self._c = client

    @classmethod
    def open(cls, path: str | os.PathLike[str] | None = None) -> "Store":
        cred = _credentials()
        kw: dict[str, Any] = {
            "tier": cred.get("tier", "free"),
            "account_id": cred.get("account_id"),
            "session_token": cred.get("session_token"),
        }
        if path is None:
            return cls(MemoryClient.local(**kw))
        return cls(MemoryClient.local(str(path), **kw))

    # --- absence-safe reads -------------------------------------------------

    def get_provider(self, name: str) -> dict[str, Any] | None:
        """None means the provider is ABSENT from memory.

        A provider that exists with an empty record is a different fact and
        comes back as a dict. Callers must not conflate the two.
        """
        try:
            return self._c.get_entity(PROVIDER, name)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise

    def providers(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._c.list_entities(PROVIDER, limit=limit)

    def read_reference(self, key: str) -> Any:
        """MEASURED: the reference tier returns body as a STRING even when a
        dict went in, unlike the state tier which returns a dict. Decode here
        so no caller has to know that.
        """
        row = self._c.get_reference(key)
        if row is None:
            return None
        body = row.get("body")
        if isinstance(body, str):
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return body
        return body

    # --- writes -------------------------------------------------------------

    def record_purchase(
        self,
        provider: str,
        *,
        delivered: bool,
        usdc: float,
        considered: list[str] | None = None,
        next_step: list[str] | None = None,
        note: str = "",
        settlement: dict[str, Any] | None = None,
    ) -> str:
        """One paid call. Updates the provider record and journals the decision.

        MEASURED: write_event accepts evaluated / acted / forward / extra and
        all four round-trip through read_events. Only `acted` is documented.

        `settlement` is the on-chain receipt for a real payment, journalled
        verbatim in `extra`. The transaction hash is lifted to `extra["tx"]` so
        an auditor can find it without knowing each rail's payload shape: the
        Base facilitator returns `transaction`, and nothing guarantees the next
        rail uses the same key. A simulated purchase has no settlement and the
        keys are simply absent -- absence is a value here, as everywhere else.
        """
        prior = self.get_provider(provider) or {}
        body = dict(prior.get("body") or {})
        body["calls"] = int(body.get("calls", 0)) + 1
        body["empty"] = int(body.get("empty", 0)) + (0 if delivered else 1)
        body["usdc_spent"] = round(float(body.get("usdc_spent", 0.0)) + usdc, 8)
        body["empty_rate"] = round(body["empty"] / body["calls"], 4)
        self._c.set_entity(PROVIDER, provider, body)
        extra: dict[str, Any] = {
            "provider": provider, "delivered": delivered, "usdc": usdc, "note": note,
        }
        if settlement:
            extra["settlement"] = settlement
            tx = tx_hash(settlement)
            if tx:
                extra["tx"] = tx
        return self._c.write_event(
            evaluated=considered or [],
            acted=[f"paid {usdc} USDC to {provider}"],
            forward=next_step or [],
            extra=extra,
        )

    def cap(self) -> dict[str, Any]:
        return self._c.free_tier_status()
