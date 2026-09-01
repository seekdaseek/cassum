// pay_bridge.mjs — the ONLY thing in this repo that can spend.
//
// cassum's Python side never sees a private key. It invokes this script, which
// reads AGENTFEED_PRIVATE_KEY from its own environment, hands it to the already
// proven @seekdaseek/plugin-agentfeed client, and prints ONE line of JSON on
// stdout. Nothing else goes to stdout, so the Python side can parse it whole.
//
// RAIL: Solana. The plugin signs with @solana/kit / toClientSvmSigner /
// ExactSvmScheme and parses its key with bs58 — it is an SVM client and cannot
// settle on Base. See FINDINGS.md.
//
// Two independent ceilings guard a run:
//   AGENTFEED_MAX_SPEND_PER_CALL  per call, enforced inside the plugin, which
//                                 preflights the 402 quote and refuses to sign
//                                 above it (shaw reproduced this himself)
//   CASSUM_MAX_USDC               cumulative per run, enforced in Python BEFORE
//                                 this script is ever invoked
//
// Contract, stdout, one line:
//   {ok, status, data, paidUsd, settlement, spentUsd, payer, error}
// Exit 0 whenever the contract was honoured, INCLUDING ok:false. A non-zero
// exit means the harness itself failed and no payment conclusion can be drawn.

const OUT = (o) => { process.stdout.write(JSON.stringify(o) + "\n"); };

function arg(name, fallback = undefined) {
  const i = process.argv.indexOf(`--${name}`);
  return i === -1 ? fallback : process.argv[i + 1];
}

const path = arg("path");
const maxPerCall = arg("max-usd");

if (!path) {
  OUT({ ok: false, status: 0, data: null, error: "bridge: --path is required" });
  process.exit(2);
}
if (!process.env.AGENTFEED_PRIVATE_KEY) {
  OUT({ ok: false, status: 0, data: null,
        error: "bridge: AGENTFEED_PRIVATE_KEY is not set in this process's environment" });
  process.exit(2);
}

let AgentFeedService;
try {
  ({ AgentFeedService } = await import("@seekdaseek/plugin-agentfeed"));
} catch (e) {
  OUT({ ok: false, status: 0, data: null,
        error: `bridge: cannot resolve @seekdaseek/plugin-agentfeed from ${process.cwd()}. ` +
               `Install it there and point CASSUM_BRIDGE_DIR at that directory. (${e.message})` });
  process.exit(2);
}

// The plugin takes an elizaOS runtime, but only ever calls getSetting. A stub
// is what the published adversarial cap test uses, so this is the reproduced
// path and not a new one.
const runtime = {
  getSetting: (k) => ({
    AGENTFEED_BASE_URL: process.env.AGENTFEED_BASE_URL,
    AGENTFEED_MAX_SPEND_PER_CALL: maxPerCall ?? process.env.AGENTFEED_MAX_SPEND_PER_CALL,
    AGENTFEED_PRIVATE_KEY: process.env.AGENTFEED_PRIVATE_KEY,
  })[k],
};

try {
  const svc = await AgentFeedService.start(runtime);
  const result = await svc.paidGet(path);
  OUT({
    ok: Boolean(result.ok),
    status: result.status ?? 0,
    data: result.data ?? null,
    // MEASURED in plugin src/service.ts: paidGet returns early on !res.ok and
    // never sets paidUsd, even when a settlement header came back. So a failed
    // paid call reports 0 here while money may in fact have moved. Python
    // treats ok:false as UNKNOWN spend and refuses to record a data point.
    paidUsd: typeof result.paidUsd === "number" ? result.paidUsd : 0,
    settlement: result.settlement ?? null,
    spentUsd: svc.spentUsd,
    payer: svc.payer,
    error: result.error ?? null,
  });
  process.exit(0);
} catch (e) {
  OUT({ ok: false, status: 0, data: null, error: `bridge: ${e?.message || String(e)}` });
  process.exit(2);
}
