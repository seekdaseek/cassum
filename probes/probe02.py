from __future__ import annotations
import json, os, tempfile, sys
from sibyl_memory_client import MemoryClient

R = []
def rec(c, s, d=""):
    d = str(d).replace("\n", " ")[:300]
    R.append({"call": c, "status": s, "detail": d})
    print(f"{s:8} {c:34} {d[:110]}")

def go(label, fn):
    try:
        out = fn()
    except Exception as e:
        rec(label, "FAILED", f"{type(e).__name__}: {e}")
        return None
    rec(label, "WORKS", repr(out)[:200])
    return out

tmp = os.path.join(tempfile.mkdtemp(prefix="cassum-p2-"), "p2.db")
mc = MemoryClient.local(tmp)

print("--- baseline tier ---")
go("get_tier", lambda: mc.get_tier())
go("free_tier_status", lambda: mc.free_tier_status())
go("schema_version", lambda: mc.schema_version())
go("get_tenant", lambda: mc.get_tenant())

print("\n--- delete on a LIVE entity ---")
go("set_live", lambda: mc.set_entity("provider", "live-y", {"a": 1}))
go("delete_live", lambda: mc.delete_entity("provider", "live-y"))
go("delete_again", lambda: mc.delete_entity("provider", "live-y"))
go("delete_never_existed", lambda: mc.delete_entity("provider", "ghost-z"))

print("\n--- reference type round-trip ---")
go("ref_dict_in", lambda: mc.set_reference("r-dict", {"k": [1, 2]}))
got = go("ref_dict_out", lambda: mc.get_reference("r-dict"))
if got:
    rec("ref_body_type", "NOTE", type(got.get("body")).__name__)

print("\n--- undocumented calls ---")
go("write_event_full", lambda: mc.write_event(
    evaluated=["provider A empty 4/10"], acted=["routed to B"],
    forward=["recheck A in 1h"], extra={"usdc": 0.002}))
go("read_events_since", lambda: mc.read_events(limit=5))
go("list_entities", lambda: mc.list_entities(limit=5))
go("list_entities_status", lambda: mc.list_entities(category="provider", status="archived", limit=5))
go("search_crosstier", lambda: mc.search("provider"))
go("search_tiers_arg", lambda: mc.search("provider", tiers=("entities", "journal")))

print("\n--- tenant isolation ---")
go("set_tenant", lambda: mc.set_tenant("11111111-1111-1111-1111-111111111111"))
go("entity_in_tenant2", lambda: mc.list_entities(limit=5))

print("\n--- tier-gated surface on FREE ---")
go("list_skill_proposals", lambda: mc.list_skill_proposals())
go("learn", lambda: mc.learn())
go("lint", lambda: mc.lint())

print("\n--- credentials path ---")
cp = os.path.expanduser("~/.sibyl-memory/credentials.json")
print("exists:", os.path.exists(cp), "bytes:", os.path.getsize(cp) if os.path.exists(cp) else 0)
if os.path.exists(cp):
    with open(cp) as f:
        cred = json.load(f)
    print("keys:", sorted(cred.keys()))
    tmp2 = os.path.join(tempfile.mkdtemp(prefix="cassum-p2b-"), "p2b.db")
    def authed():
        return MemoryClient.local(
            tmp2,
            tier=cred.get("tier", "free"),
            account_id=cred.get("account_id"),
            session_token=cred.get("session_token"),
            credentials_claim=cred.get("claim"),
            credentials_signature=cred.get("signature"),
        )
    mc2 = go("local_with_credentials", authed)
    if mc2:
        go("authed_get_tier", lambda: mc2.get_tier())
        go("authed_list_skill_proposals", lambda: mc2.list_skill_proposals())
        go("authed_lint", lambda: mc2.lint())

with open("probe02-report.json", "w") as f:
    json.dump({"results": R}, f, indent=2)
w = sum(1 for r in R if r["status"] == "WORKS")
print(f"\n{w} worked / {sum(1 for r in R if r['status']=='FAILED')} failed  ->  ~/cassum/probe02-report.json")
