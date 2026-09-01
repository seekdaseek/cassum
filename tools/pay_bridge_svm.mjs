// pay_bridge_svm.mjs — Solana settlement. Same contract as the other bridges.
//
// NO SIGNING CODE LIVES HERE. Signatures come from @seekdaseek/x402-wallet
// (src/svm/signer.js, a TransactionModifyingSigner) and @x402/svm's
// ExactSvmScheme, via the same keypairAdapter its own settle-mainnet.mjs uses.
// That library has a proven Solana mainnet settlement against this service:
// 5XPKFWmL937cUHF29koc26QeQTwBmzo3KCw88NdfhhpNtx1TUgKjvo6BtkJ1BD7TBDfbfpTHmQC7U7MUFzdxWqM2
//
// WHY NOT @seekdaseek/plugin-agentfeed, which tools/pay_bridge.mjs drives:
// it is installed nowhere on this machine and would need an npm install first.
// Both clients build an ExactSvmScheme payload through @x402/svm, so this
// exercises the same scheme the published plugin does -- but it is a different
// client, and that distinction belongs in any claim made from a run of it.
//
// Contract, stdout, one line, IDENTICAL to the other bridges:
//   {ok, status, data, paidUsd, quotedUsd, settlement, spentUsd, payer, error}

import { pathToFileURL } from "node:url";
import { join } from "node:path";
import fs from "node:fs";

const OUT = (o) => { process.stdout.write(JSON.stringify(o) + "\n"); };
const FAIL = (error) => { OUT({ ok: false, status: 0, data: null, paidUsd: 0, quotedUsd: null,
                                settlement: null, spentUsd: 0, payer: "", error }); process.exit(2); };

function arg(name, fallback = undefined) {
  const i = process.argv.indexOf(`--${name}`);
  return i === -1 ? fallback : process.argv[i + 1];
}

const path = arg("path");
const maxPerCall = Number(arg("max-usd"));
const walletDir = process.env.CASSUM_WALLET_DIR;

if (!path) FAIL("bridge-svm: --path is required");
if (!Number.isFinite(maxPerCall) || maxPerCall <= 0) FAIL("bridge-svm: --max-usd must be a positive number");
if (!walletDir) FAIL("bridge-svm: CASSUM_WALLET_DIR is not set. Point it at the @seekdaseek/x402-wallet checkout.");

// The key is read from a FILE and never echoed. Only its shape is reported.
const keyPath = process.env.SOLANA_PAYER || join(walletDir, "payer.json");
if (!fs.existsSync(keyPath)) FAIL(`bridge-svm: no payer key at ${keyPath}. Set SOLANA_PAYER to its path.`);
try {
  const raw = JSON.parse(fs.readFileSync(keyPath, "utf8"));
  if (!Array.isArray(raw)) FAIL(`bridge-svm: ${keyPath} is not a solana-keygen JSON byte array. Not attempting to sign.`);
} catch (e) { FAIL(`bridge-svm: cannot read ${keyPath}: ${e.message}`); }

let createX402Wallet, CapExceeded, createSvmSigner, keypairAdapter;
try {
  const wallet = await import(pathToFileURL(join(walletDir, "src/index.js")).href);
  ({ createX402Wallet, CapExceeded, createSvmSigner } = wallet);
  ({ keypairAdapter } = await import(pathToFileURL(join(walletDir, "keypair-adapter.mjs")).href));
} catch (e) {
  FAIL(`bridge-svm: cannot load the wallet library from ${walletDir}: ${e.message}`);
}

try {
  const svmSigner = await createSvmSigner(await keypairAdapter(keyPath));
  // Only svmSigner is provided, so the library registers solana:* and NOTHING
  // else. This bridge is structurally incapable of signing an EVM payment.
  const wallet = createX402Wallet({
    svmSigner,
    rpcUrl: process.env.SOL_RPC || "https://api.mainnet-beta.solana.com",
    caps: { maxPerCallUsd: maxPerCall, maxTotalUsd: maxPerCall },
  });

  const base = process.env.AGENTFEED_BASE_URL || "https://x402.ochinimus.app";

  // Read the challenge UNPAID to learn the exact amount, exactly as the EVM
  // bridge does. --max-usd is a ceiling, not a price; reporting it as the
  // amount paid writes a wrong usdc_spent that effective_cost divides by.
  let quotedUsd = null;
  try {
    const probe = await fetch(base + path, { headers: { Accept: "application/json" } });
    const hdr = probe.headers.get("payment-required");
    if (probe.status === 402 && hdr) {
      const challenge = JSON.parse(Buffer.from(hdr, "base64").toString("utf8"));
      const rail = (challenge.accepts || []).find((a) => String(a.network).startsWith("solana:"));
      if (rail) quotedUsd = Number(rail.amount) / 1e6;
    }
  } catch { /* leave null; reported as unknown rather than guessed */ }

  const res = await wallet.payFetch(base + path);
  const settleHeader = res.headers.get("payment-response") || res.headers.get("x-payment-response");
  let settlement = null;
  if (settleHeader) {
    try { settlement = JSON.parse(Buffer.from(settleHeader, "base64").toString("utf8")); }
    catch { settlement = String(settleHeader).slice(0, 200); }
  }
  const data = await res.json().catch(() => null);
  const reported = settlement && typeof settlement.amountUsd === "number" ? settlement.amountUsd : null;
  const paidUsd = settlement ? (reported ?? quotedUsd) : 0;

  OUT({
    ok: Boolean(res.ok), status: res.status, data, paidUsd, quotedUsd, settlement,
    spentUsd: wallet.spentUsd, payer: svmSigner.address,
    error: res.ok ? null : `AgentFeed HTTP ${res.status}`,
  });
  process.exit(0);
} catch (e) {
  const capped = CapExceeded && e instanceof CapExceeded;
  OUT({
    ok: false, status: 0, data: null, paidUsd: 0, quotedUsd: null, settlement: null,
    spentUsd: 0, payer: "",
    error: capped ? `bridge-svm: library cap refused pre-signature: ${e.message}`
                  : `bridge-svm: ${e?.message || String(e)}`,
  });
  process.exit(capped ? 0 : 2);
}
