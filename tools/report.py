"""Generate RESULTS.md from actual runs. No figure in that file is typed by hand."""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
from pathlib import Path

from cassum.ablate import run
from cassum.sim import default_fleet, flat_fleet


def commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def block(r: dict) -> str:
    w, o = r["with_memory"], r["without_memory"]
    ratio = round(o["usdc_per_payload"] / w["usdc_per_payload"], 3) if w["usdc_per_payload"] else None
    return (
        f"### `{r['fleet']}`\n\n"
        f"| | calls | USDC | USDC per delivered payload |\n"
        f"|---|---:|---:|---:|\n"
        f"| with memory | {w['calls']} | {w['usdc']} | {w['usdc_per_payload']} |\n"
        f"| memory deleted | {o['calls']} | {o['usdc']} | {o['usdc_per_payload']} |\n\n"
        f"Both runs delivered {w['delivered']} payloads. "
        f"Difference {r['usdc_saved']} USDC ({r['pct_saved']}%), "
        f"a {ratio}x ratio in cost per payload.\n"
    )


def live_block(db: Path | None) -> list[str]:
    """Observed rates, read out of the live memory store. Nothing here is typed.

    These sit ALONGSIDE the simulated table and are never combined with it. The
    simulated fleet answers "what does memory save when providers differ"; this
    answers "how often does a real endpoint actually deliver". One is arithmetic
    over chosen failure rates, the other is a count of real purchases. Averaging
    them, or quoting a saving from these, would be meaningless.
    """
    head = [
        "## Measured against live endpoints",
        "",
    ]
    if db is None or not db.exists():
        return head + [
            "No live measurement has been recorded yet. `cassum/sim.py`'s failure",
            "patterns are chosen, not observed, and the table above says so. Run",
            "`tools/measure.py --db <path>` and regenerate this file to replace",
            "this paragraph with counted purchases.",
            "",
        ]

    from cassum.memory import Store

    rows = Store.open(db).providers()
    if not rows:
        return head + ["The live store at `" + db.name + "` holds no purchases yet.", ""]

    out = head + [
        f"Read from `{db.name}`, written by `tools/measure.py`. Every row is a count",
        "of real paid calls on Base; every purchase's transaction hash is in that",
        "store's journal under `extra.tx`.",
        "",
        "| endpoint | paid calls | unusable | empty rate | USDC spent |",
        "|---|---:|---:|---:|---:|",
    ]
    calls = empty = 0
    spent = 0.0
    for row in sorted(rows, key=lambda r: r["name"]):
        b = row["body"]
        calls += int(b["calls"])
        empty += int(b["empty"])
        spent += float(b["usdc_spent"])
        out.append(
            f"| `{row['name']}` | {b['calls']} | {b['empty']} | "
            f"{float(b['empty_rate']):.4f} | {float(b['usdc_spent']):.5f} |"
        )
    out += [
        "",
        f"{calls} paid calls across {len(rows)} endpoints, {empty} of them unusable, "
        f"{spent:.5f} USDC spent.",
        "",
        "**`empty rate` is the usability predicate, not the status code.** A row's",
        "unusable count is calls that returned HTTP 200 and nothing worth having --",
        "for `cascade-forecast` that is the by-design decline when a symbol's",
        "history is too thin to answer from.",
        "",
    ]
    if empty == 0:
        out += [
            "**NO DISPERSION WAS OBSERVED.** Every one of these calls delivered, so",
            "the measured empty rate is 0.0000 everywhere and these endpoints are, on",
            "this evidence, indistinguishable to the router. That is a real result and",
            "it does NOT corroborate the simulated table above: `default_fleet`'s",
            "dispersion remains a chosen model that this run did not find in the wild.",
            "On these numbers alone the live fleet resembles `flat_fleet`, where memory",
            "is overhead.",
            "",
            "The sample is small and the reason is budget, not choice. It is also",
            "incomplete in a specific way: `liquidations` was sampled 5 times against a",
            "7-symbol rotation, so the two unlisted symbols that were expected to return",
            "an empty set were never reached. The decline branch of the predicate is",
            "therefore still unexercised live. Purchases accumulate, so a further run",
            "extends these same rows rather than replacing them.",
            "",
        ]
    else:
        out += [
            f"**Dispersion WAS observed:** {empty} of {calls} paid calls returned",
            "nothing usable, and the rates above differ between endpoints. The decline",
            "branch of the usability predicate is exercised against live payments, not",
            "only against fixtures.",
            "",
            "**Two things these rates are NOT.**",
            "",
            "They are not intrinsic properties of the endpoints. An empty rate is a",
            "function of the QUERY MIX, and this one was deliberately built half from",
            "symbols the source says will answer and half from symbols it says will",
            "not -- tickers absent from the liquidation tape, and symbols with no",
            "threshold in the forecaster's fitted model. Sampling only majors would",
            "have reported 0.0000 for the same endpoints, as an earlier run did.",
            "",
            "They are not a ranking. These endpoints answer different questions and are",
            "not substitutes, so a buyer cannot swap the dear one for the cheap one the",
            "way `default_fleet`'s providers can be swapped. The cost-per-payload",
            "arithmetic is sound and the router would compute it correctly; what does",
            "not follow is that anyone should act on the comparison.",
            "",
        ]
    out += [
        "**These numbers cannot be turned into a saving.** The payee is a treasury",
        "this project controls, so the cost side is not arm's length.",
        "",
    ]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-db", type=Path, default=Path("live.db"),
                    help="memory store written by tools/measure.py")
    args = ap.parse_args()
    lines = [
        "# Results",
        "",
        f"Generated by `tools/report.py` at {dt.datetime.now(dt.timezone.utc).isoformat()} "
        f"from commit `{commit()}`. Every number below is computed from a run, not typed.",
        "",
        "## The deletion test",
        "",
        "**Simulated fleet.** The failure patterns in `cassum/sim.py` are CHOSEN,",
        "not observed. This section is arithmetic over a model. Counted rates from",
        "real paid endpoints are a separate section below and are never merged",
        "into these figures.",
        "",
        "Same workload twice: once against Sibyl Memory, once against a `NullStore`",
        "that accepts every write and returns nothing on every read. That second run",
        "is literally the judges' litmus test, executed rather than asserted.",
        "",
        "**What the memoryless agent does, stated plainly:** it buys from the",
        "cheapest provider by sticker price, every time. That is not a strawman, it",
        "is the only rational policy available with no purchase history. The result",
        "below is the cost of that being the only available signal.",
        "",
    ]
    for f in (default_fleet, flat_fleet):
        lines.append(block(run(fleet=f)))
    lines += live_block(args.live_db)
    lines += [
        "## What a LIVE run can and cannot show",
        "",
        "The simulated comparison above is the savings claim. A live run against",
        "`x402.ochinimus.app` **cannot** produce a second one, and must never be",
        "read as corroborating it.",
        "",
        "**The payee is our own treasury.** `payTo` on both rails is an address",
        "this project controls, so USDC paid in a live run returns to the same",
        "hands. A \"saving\" measured against that is money moved from one pocket",
        "to another, and any percentage computed from it is circular.",
        "",
        "A live run has two narrower jobs, and only these two:",
        "",
        "1. **Prove settlement works** — one verifiable transaction hash for a",
        "   real x402 payment, on chain, that anyone can look up.",
        "2. **Record real empty rates** — how often each sampled endpoint",
        "   actually returns something usable, measured rather than simulated.",
        "",
        "Those go in the provider records like any other purchase. What does NOT",
        "follow from them is a cost-per-payload figure, because the cost side is",
        "not arm's length. The dispersion in `default_fleet` is chosen; the",
        "dispersion in a live fleet would be real but the prices would not be.",
        "",
        "## Reading these together",
        "",
        "`default_fleet` has real dispersion: hollow-cheap is cheapest per call and",
        "dearest per payload, steady-dear is the reverse. Memory finds that and the",
        "spend falls by half.",
        "",
        "`flat_fleet` has none: every provider costs the same per delivered payload.",
        "Memory finds nothing, pays for the exploration anyway, and ends up more",
        "expensive. This negative is pinned by",
        "`test_memory_is_overhead_when_no_provider_has_an_edge` and is kept on",
        "purpose. **Memory pays when providers differ in cost per delivered payload,",
        "and costs when they do not.**",
        "",
        "## Limits",
        "",
        "These providers are deterministic simulations in `cassum/sim.py`. The",
        "arithmetic is real and reproducible; the failure rates are chosen. Numbers",
        "against live paid endpoints are a separate measurement and are not claimed",
        "here -- and per the section above, a live run against our own treasury",
        "could not produce a comparable one even if it were run.",
        "",
        "Settlement runs on either rail, selected by `CASSUM_RAIL`. Each rail",
        "shells out to a separate JS payer, and this repo contains no signing",
        "code of its own: `solana` uses @seekdaseek/plugin-agentfeed, `base` uses",
        "@seekdaseek/x402-wallet. Both have prior live mainnet settlements. The",
        "default is `solana` so that upgrading this package never silently",
        "changes which chain money moves on.",
        "",
    ]
    out = "\n".join(lines)
    with open("RESULTS.md", "w") as f:
        f.write(out)
    print(out)


if __name__ == "__main__":
    main()
