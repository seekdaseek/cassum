"""The deletion test, run as code. Same workload, memory on and memory off."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

from cassum.memory import Store
from cassum.router import Router
from cassum.sim import default_fleet, flat_fleet


class NullStore:
    """Accepts every write, remembers nothing. This is 'delete the memory layer'."""

    def get_provider(self, name: str) -> dict[str, Any] | None:
        return None

    def record_purchase(self, provider: str, **kw: Any) -> str:
        return ""


def run(wanted: int = 20, fleet: Callable[[], list[Any]] = default_fleet) -> dict[str, Any]:
    db = Path(tempfile.mkdtemp(prefix="cassum-ablate-")) / "a.db"
    with_mem = Router(Store.open(db), fleet()).collect(wanted)
    without = Router(NullStore(), fleet()).collect(wanted)
    saved = round(without["usdc"] - with_mem["usdc"], 8)
    return {
        "wanted": wanted,
        "fleet": fleet.__name__,
        "with_memory": with_mem,
        "without_memory": without,
        "usdc_saved": saved,
        "pct_saved": round(100 * saved / without["usdc"], 2) if without["usdc"] else None,
    }


if __name__ == "__main__":
    import json

    for f in (default_fleet, flat_fleet):
        r = run(fleet=f)
        print(json.dumps(r, indent=2))
        print(
            f"-> {f.__name__}: {r['with_memory']['usdc']} USDC with memory, "
            f"{r['without_memory']['usdc']} without. "
            f"Saved {r['usdc_saved']} ({r['pct_saved']}%).\n"
        )
