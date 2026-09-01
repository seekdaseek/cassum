from __future__ import annotations
import importlib.metadata as md
import inspect, json, tempfile, os, sys

R = []
def rec(call, status, detail=""):
    d = str(detail).replace("\n", " ")[:300]
    R.append({"call": call, "status": status, "detail": d})
    print(f"{status:8} {call:32} {d[:110]}")

def attempt(label, fn):
    try:
        out = fn()
    except Exception as e:
        rec(label, "FAILED", f"{type(e).__name__}: {e}")
        return None
    rec(label, "WORKS", repr(out)[:200])
    return out

print("python", sys.version.split()[0])
for pkg in ("sibyl-memory-client", "sibyl-memory-cli", "sibyl-memory-mcp"):
    try:
        print(pkg, md.version(pkg))
    except Exception as e:
        print(pkg, "NOT INSTALLED", e)

from sibyl_memory_client import MemoryClient

tmp = os.path.join(tempfile.mkdtemp(prefix="cassum-probe-"), "probe.db")
print("probe db:", tmp)

mc = attempt("MemoryClient.local", lambda: MemoryClient.local(tmp))
if mc is None:
    sys.exit("cannot open a client; nothing else can be measured")

print("\n--- public surface ---")
surface = {}
for n in sorted(dir(mc)):
    if n.startswith("_"):
        continue
    a = getattr(mc, n)
    if not callable(a):
        continue
    try:
        surface[n] = str(inspect.signature(a))
    except (TypeError, ValueError):
        surface[n] = "(signature unavailable)"
    print(f"  {n}{surface[n]}")

print("\n--- documented calls ---")
attempt("set_entity", lambda: mc.set_entity("provider", "probe-x", {"empty_rate": 0.4}))
attempt("get_entity", lambda: mc.get_entity("provider", "probe-x"))
attempt("search_entities", lambda: mc.search_entities("probe"))
attempt("set_state", lambda: mc.set_state("budget", {"usdc": 1.0}))
attempt("get_state", lambda: mc.get_state("budget"))
attempt("write_event", lambda: mc.write_event(acted=["probe paid call"]))
attempt("read_events", lambda: mc.read_events())
attempt("set_reference", lambda: mc.set_reference("probe-ref", {"note": "x"}))
attempt("get_reference", lambda: mc.get_reference("probe-ref"))
attempt("archive_entity", lambda: mc.archive_entity("provider", "probe-x"))
attempt("get_after_archive", lambda: mc.get_entity("provider", "probe-x"))
attempt("delete_entity", lambda: mc.delete_entity("provider", "probe-x"))

out = {"surface": surface, "results": R, "db": tmp}
with open("probe01-report.json", "w") as f:
    json.dump(out, f, indent=2)
w = sum(1 for r in R if r["status"] == "WORKS")
print(f"\n{w} worked / {len(R) - w} failed  ->  ~/cassum/probe01-report.json")
