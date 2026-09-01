// pay_bridge_evm.mjs — Base/EVM settlement. Same contract as pay_bridge.mjs.
//
// NO SIGNING CODE LIVES HERE. Every signature is produced by
// @seekdaseek/x402-wallet (src/evm/signer.js, EIP-712 signTypedData via viem)
// and by @x402/evm's ExactEvmScheme. This file resolves that library, hands it
// a key it read from a FILE, and translates its result into the one line of
// JSON the Python side already parses.
//
// That library has already settled on Base mainnet against this same service:
// 0.001 USDC, eip155:8453, tx 0xe49b8c75de425c52b321fa1df5c428a18e51ca11e3b982fc80b6ea2ed24c4a31.
// The payer holds no native gas; the CDP facilitator sponsors it.
//
// Contract, stdout, one line, IDENTICAL to the SVM bridge:
//   {ok, status, data, paidUsd, settlement, spentUsd, payer, error}
// Exit 0 whenever the contract was honoured, including ok:false. Non-zero means
// the harness failed and no payment conclusion can be drawn.

import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { join } from "node:path";
import fs from "node:fs";

const OUT = (o) => { process.stdout.write(JSON.stringify(o) + "\n"); };
const FAIL = (error) => { OUT({ ok: false, status: 0, data: null, paidUsd: 0,
                                settlement: null, spentUsd: 0, payer: "", error }); process.exit(2); };

function arg(name, fallback = undefined) {
  const i = process.argv.indexOf(`--${name}`);
  return i === -1 ? fallback : process.argv[i + 1];
}

const path = arg("path");
const maxPerCall = Number(arg("max-usd"));
const walletDir = process.env.CASSUM_WALLET_DIR;

if (!path) FAIL("bridge-evm: --path is required");
if (!Number.isFinite(maxPerCall) || maxPerCall <= 0) FAIL("bridge-evm: --max-usd must be a positive number");
if (!walletDir) FAIL("bridge-evm: CASSUM_WALLET_DIR is not set. Point it at the @seekdaseek/x402-wallet checkout.");

// The key is read from a FILE, never from an env var holding the secret itself,
// and never echoed. Only its presence and length are ever reported.
const keyPath = process.env.EVM_PAYER || join(walletDir, "payer-evm.key");
if (!fs.existsSync(keyPath)) FAIL(`bridge-evm: no payer key at ${keyPath}. Set EVM_PAYER to its path.`);
let pk;
try {
  pk = fs.readFileSync(keyPath, "utf8").trim();
} catch (e) {
  FAIL(`bridge-evm: cannot read ${keyPath}: ${e.message}`);
}
if (!/^0x[0-9a-fA-F]{64}$/.test(pk)) {
  FAIL(`bridge-evm: ${keyPath} is not a 0x-prefixed 32-byte hex key (read ${pk.length} chars). Not attempting to sign.`);
}

// Resolve BOTH the wallet library and viem from the wallet checkout. ESM
// resolves bare specifiers relative to the importing FILE, and this file lives
// in cassum, which has no node_modules -- so viem is resolved through a require
// anchored at the wallet package instead of imported by name.
let createX402Wallet, CapExceeded, fromViemWalletClient, viem, viemAccounts, viemChains;
try {
  const wallet = await import(pathToFileURL(join(walletDir, "src/index.js")).href);
  ({ createX402Wallet, CapExceeded, fromViemWalletClient } = wallet);
  const req = createRequire(join(walletDir, "package.json"));
  viem = await import(pathToFileURL(req.resolve("viem")).href);
  viemAccounts = await import(pathToFileURL(req.resolve("viem/accounts")).href);
  viemChains = await import(pathToFileURL(req.resolve("viem/chains")).href);
} catch (e) {
  FAIL(`bridge-evm: cannot load the wallet library from ${walletDir}: ${e.message}`);
}

try {
  const account = viemAccounts.privateKeyToAccount(pk);
  const walletClient = viem.createWalletClient({
    account,
    chain: viemChains.base,
    transport: viem.http(process.env.BASE_RPC || "https://mainnet.base.org"),
  });
  const evmSigner = fromViemWalletClient(walletClient, account);

  // Only evmSigner is provided, so the library registers eip155:* and NOTHING
  // else. This bridge is structurally incapable of signing a Solana payment.
  //
  // maxTotalUsd is set to this single call: the cumulative ceiling for the run
  // is Python's, already checked before this process started. Bounding the
  // library to one call keeps the two ledgers from disagreeing about what a
  // "session" is.
  const wallet = createX402Wallet({
    evmSigner,
    caps: { maxPerCallUsd: maxPerCall, maxTotalUsd: maxPerCall },
  });

  const base = process.env.AGENTFEED_BASE_URL || "https://x402.ochinimus.app";

  // Read the challenge ourselves, UNPAID, to learn the exact amount this call
  // will cost. Do NOT infer it from --max-usd: that is a ceiling, not a price,
  // and the two are equal only by coincidence. Reporting the ceiling as the
  // amount paid writes a wrong usdc_spent into memory, and effective_cost
  // divides by it. MEASURED 2026-09-01: reported 0.05 for a 0.003 call.
  let quotedUsd = null;
  try {
    const probe = await fetch(base + path, { headers: { Accept: "application/json" } });
    const hdr = probe.headers.get("payment-required");
    if (probe.status === 402 && hdr) {
      const challenge = JSON.parse(Buffer.from(hdr, "base64").toString("utf8"));
      const rail = (challenge.accepts || []).find((a) => String(a.network).startsWith("eip155:"));
      if (rail) quotedUsd = Number(rail.amount) / 1e6;
    }
  } catch { /* leave null; reported below as unknown rather than guessed */ }

  const res = await wallet.payFetch(base + path);
  const settleHeader = res.headers.get("payment-response") || res.headers.get("x-payment-response");
  let settlement = null;
  if (settleHeader) {
    try { settlement = JSON.parse(Buffer.from(settleHeader, "base64").toString("utf8")); }
    catch { settlement = String(settleHeader).slice(0, 200); }
  }
  const data = await res.json().catch(() => null);

  // MEASURED in the library's src/core.js: spentUsd only advances when the
  // decoded settlement carries amountUsd, and no receipt is pushed at all when
  // the response has no payment-response header. So a settlement WITHOUT
  // amountUsd reports paidUsd from the quoted amount Python passed in, which is
  // the exact rail amount off the payment-required header, not a guess.
  // MEASURED in the library's src/core.js: spentUsd only advances when the
  // decoded settlement carries amountUsd, and this facilitator does not send
  // one. So the amount comes from the challenge we just read -- never from the
  // cap. If both are unavailable the field is null, which the Python side
  // treats as an unknown rather than a number.
  const reported = settlement && typeof settlement.amountUsd === "number" ? settlement.amountUsd : null;
  const paidUsd = settlement ? (reported ?? quotedUsd) : 0;

  OUT({
    ok: Boolean(res.ok),
    status: res.status,
    data,
    paidUsd,
    quotedUsd,
    settlement,
    spentUsd: wallet.spentUsd,
    payer: evmSigner.address,
    error: res.ok ? null : `AgentFeed HTTP ${res.status}`,
  });
  process.exit(0);
} catch (e) {
  // CapExceeded is the library refusing BEFORE any signature was requested.
  const capped = CapExceeded && e instanceof CapExceeded;
  OUT({
    ok: false, status: 0, data: null, paidUsd: 0, settlement: null, spentUsd: 0, payer: "",
    error: capped ? `bridge-evm: library cap refused pre-signature: ${e.message}`
                  : `bridge-evm: ${e?.message || String(e)}`,
  });
  process.exit(capped ? 0 : 2);
}
