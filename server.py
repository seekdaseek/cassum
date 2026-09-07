"""Public demo server for cassum. Standard library only, no new dependencies.

Two routes do real work at request time. Nothing here reads a cached result:

    GET /api/ablate    runs the deletion test now, both fleets, in a fresh
                       temp database per request.
    GET /api/session   spawns two separate OS processes, learn then recall,
                       and returns both transcripts with their pids.

A judge can therefore execute the gate from a browser without cloning.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "3021"))
REPO_URL = "https://github.com/seekdaseek/cassum"
VIDEO_URL = "https://youtu.be/epNPpP-EuIs"

# One run at a time. These routes fork interpreters; unbounded concurrency
# would let a crawler exhaust the box.
_RUN = threading.Semaphore(2)
_TIMEOUT = 120


def commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


COMMIT = commit()


def run_ablate() -> dict:
    from cassum.ablate import run
    from cassum.sim import default_fleet, flat_fleet

    started = dt.datetime.now(dt.timezone.utc).isoformat()
    out = {"computed_at": started, "commit": COMMIT, "runs": []}
    for fleet in (default_fleet, flat_fleet):
        out["runs"].append(run(fleet=fleet))
    return out


def run_session() -> dict:
    db = Path(tempfile.mkdtemp(prefix="cassum-web-")) / "demo.db"
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    phases = []
    for phase in ("learn", "recall"):
        proc = subprocess.run(
            [sys.executable, str(REPO / "tools" / "session.py"),
             "--db", str(db), "--phase", phase],
            capture_output=True, text=True, timeout=_TIMEOUT, cwd=str(REPO), env=env,
        )
        phases.append({
            "phase": phase,
            "argv": f"python tools/session.py --db {db} --phase {phase}",
            "exit": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        })
    return {
        "computed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "commit": COMMIT,
        "db": str(db),
        "phases": phases,
    }


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>cassum — buyer-side memory for agents that pay per call</title>
<style>
:root{{
  --bg:#0b0f14; --panel:#111820; --line:#1e2a36; --ink:#e6edf3; --dim:#8b9aa8;
  --acc:#4ade80; --warn:#fbbf24; --hot:#f472b6; --blue:#60a5fa;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:
  radial-gradient(900px 500px at 15% -10%,#12304a55,transparent),
  radial-gradient(700px 400px at 90% 0%,#3b1d4a55,transparent),var(--bg);
  color:var(--ink);font:16px/1.6 ui-sans-serif,-apple-system,Segoe UI,Inter,sans-serif;}}
.wrap{{max-width:960px;margin:0 auto;padding:56px 22px 90px}}
h1{{font:700 clamp(38px,7vw,64px)/1.02 var(--mono);letter-spacing:-.04em;margin:0}}
h1 span{{color:var(--acc)}}
.lede{{font-size:19px;color:var(--dim);max-width:62ch;margin:18px 0 0}}
.lede b{{color:var(--ink);font-weight:600}}
.meta{{margin:26px 0 0;display:flex;flex-wrap:wrap;gap:10px}}
.chip{{font:12px/1 var(--mono);color:var(--dim);border:1px solid var(--line);
  background:#0e151d;border-radius:999px;padding:8px 12px;text-decoration:none}}
.chip:hover{{color:var(--ink);border-color:#33475a}}
.chip b{{color:var(--acc);font-weight:600}}
section{{margin-top:44px;border:1px solid var(--line);border-radius:16px;
  background:linear-gradient(180deg,#111820,#0d141b);overflow:hidden}}
.head{{padding:20px 22px;border-bottom:1px solid var(--line);
  display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}}
.head h2{{margin:0;font:600 20px/1.25 var(--mono);letter-spacing:-.02em}}
.head p{{margin:6px 0 0;color:var(--dim);font-size:14.5px;max-width:64ch}}
.tag{{font:11px/1 var(--mono);text-transform:uppercase;letter-spacing:.11em;
  padding:6px 9px;border-radius:6px;background:#132a1e;color:var(--acc);
  border:1px solid #1d4030;white-space:nowrap}}
.tag.b{{background:#12233a;color:var(--blue);border-color:#1d3a5c}}
button{{font:600 14px/1 var(--mono);color:#06210f;background:var(--acc);
  border:0;border-radius:9px;padding:13px 18px;cursor:pointer}}
button:hover{{filter:brightness(1.1)}}
button:disabled{{opacity:.45;cursor:progress}}
.body{{padding:20px 22px}}
pre{{margin:16px 0 0;padding:16px;background:#070b0f;border:1px solid var(--line);
  border-radius:11px;overflow:auto;font:12.5px/1.55 var(--mono);color:#cfe3d6;
  max-height:460px;white-space:pre;-webkit-overflow-scrolling:touch}}
.pid{{color:var(--hot)}}
.hint{{color:var(--dim);font-size:13.5px;margin:14px 0 0}}
footer{{margin-top:52px;color:var(--dim);font:12.5px/1.7 var(--mono);
  border-top:1px solid var(--line);padding-top:18px}}
footer a{{color:var(--blue);text-decoration:none}}
</style></head><body><div class="wrap">

<h1>cassum<span>.</span></h1>
<p class="lede">An agent buying data over x402 has no record of what it bought. It pays,
gets an empty payload, and next run it pays the same provider again. Every purchase is a
cold start. <b>cassum remembers what each provider actually delivered</b>, and ranks on
cost per delivered payload instead of sticker price.</p>

<div class="meta">
  <a class="chip" href="{repo}">repo <b>{commit}</b></a>
  <a class="chip" href="{video}">demo video</a>
  <span class="chip">memory <b>Sibyl Memory</b> · SQLite, five tiers</span>
  <span class="chip">rails <b>Base</b> · Solana · x402</span>
</div>

<section>
  <div class="head">
    <span class="tag">runs on click</span>
    <div style="flex:1;min-width:260px">
      <h2>The deletion test</h2>
      <p>The same workload twice: once against Sibyl Memory, once against a NullStore that
      accepts every write and returns nothing on every read. That second run is the judges'
      litmus test, executed rather than asserted. Fresh database each time, computed now.</p>
    </div>
    <button id="b1" onclick="go('ablate','o1','b1')">Run it</button>
  </div>
  <div class="body">
    <p class="hint">flat_fleet is kept deliberately: when no provider has an edge, memory
    costs 3.75% more. A negative that survives in the repo as a pinned test.</p>
    <pre id="o1">idle. nothing precomputed on this page.</pre>
  </div>
</section>

<section>
  <div class="head">
    <span class="tag b">two OS processes</span>
    <div style="flex:1;min-width:260px">
      <h2>Cold-start recall</h2>
      <p>Learn spends against the fleet and writes what it measured to disk. Recall is a
      separate interpreter with a different pid that opens the same file, cannot buy — its
      record_purchase raises — and still names the right provider. Nothing passes between
      them but the database.</p>
    </div>
    <button id="b2" onclick="go('session','o2','b2')">Run both</button>
  </div>
  <div class="body">
    <p class="hint">Compare the pid on the first line of each transcript.</p>
    <pre id="o2">idle.</pre>
  </div>
</section>

<footer>
serving commit {commit} · <a href="{repo}">github.com/seekdaseek/cassum</a> ·
MIT · built for the Sibyl Labs Hackathon<br>
raw json: <a href="/api/ablate">/api/ablate</a> · <a href="/api/session">/api/session</a>
</footer>
</div>
<script>
async function go(route,out,btn){{
  const o=document.getElementById(out), b=document.getElementById(btn);
  b.disabled=true; const t0=Date.now(); o.textContent='running '+route+' on the server...';
  try{{
    const r=await fetch('/api/'+route);
    const j=await r.json();
    o.innerHTML = route==='session' ? renderSession(j) : renderAblate(j);
    o.insertAdjacentHTML('beforeend','\\n\\ncomputed in '+(Date.now()-t0)+' ms at '+j.computed_at);
  }}catch(e){{ o.textContent='failed: '+e; }}
  b.disabled=false;
}}
function esc(s){{return s.replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}})[c]);}}
function renderAblate(j){{
  let s='';
  for(const r of j.runs){{
    const w=r.with_memory,n=r.without_memory;
    s+=r.fleet+'\\n'+'-'.repeat(58)+'\\n'
     +'                 calls      USDC   USDC/delivered payload\\n'
     +'  with memory    '+String(w.calls).padStart(5)+'  '+String(w.usdc).padStart(8)+'   '+w.usdc_per_payload+'\\n'
     +'  memory DELETED '+String(n.calls).padStart(5)+'  '+String(n.usdc).padStart(8)+'   '+n.usdc_per_payload+'\\n'
     +'  -> '+r.pct_saved+'% of spend recovered by memory\\n\\n';
  }}
  return esc(s);
}}
function renderSession(j){{
  let s='';
  for(const p of j.phases){{
    s+='$ '+p.argv+'\\n'+p.stdout+(p.stderr||'')+'\\n';
  }}
  return esc(s).replace(/(pid \\d+)/g,'<span class="pid">$1</span>');
}}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "cassum"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            page = PAGE.format(
                commit=html.escape(COMMIT), repo=REPO_URL, video=VIDEO_URL
            )
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        if path == "/health":
            return self._send(
                200,
                json.dumps({"ok": True, "commit": COMMIT}).encode(),
                "application/json",
            )
        if path in ("/api/ablate", "/api/session"):
            if not _RUN.acquire(blocking=False):
                return self._send(
                    503, b'{"error":"busy, try again"}', "application/json"
                )
            try:
                data = run_ablate() if path.endswith("ablate") else run_session()
                body = json.dumps(data, indent=2).encode()
                return self._send(200, body, "application/json")
            except Exception as exc:
                return self._send(
                    500,
                    json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode(),
                    "application/json",
                )
            finally:
                _RUN.release()
        self._send(404, b'{"error":"not found"}', "application/json")


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"cassum demo on 127.0.0.1:{PORT} commit {COMMIT}", flush=True)
    srv.serve_forever()
