// payer_info.mjs — READ ONLY. Derives the payer address and reads its Base
// balances. Signs nothing, sends nothing, spends nothing.
//
// Prints the ADDRESS (public) and balances only. The key is read to derive the
// address and is never printed, logged, or returned.
//
//   node tools/payer_info.mjs            # human readable
//   node tools/payer_info.mjs --json     # {address, usdc, eth} for a caller
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { join } from "node:path";
import fs from "node:fs";
const W = process.env.CASSUM_WALLET_DIR;
const keyPath = process.env.EVM_PAYER;
const raw = fs.readFileSync(keyPath, "utf8").trim();
const JSON_OUT = process.argv.includes("--json");
const say = (...a) => { if (!JSON_OUT) console.log(...a); };
say("key file      :", keyPath);
say("key format    :", /^0x[0-9a-fA-F]{64}$/.test(raw) ? "valid 0x + 32-byte hex" : `INVALID (${raw.length} chars)`);
if (!/^0x[0-9a-fA-F]{64}$/.test(raw)) { console.error("payer_info: key is not a 0x-prefixed 32-byte hex string"); process.exit(2); }
const req = createRequire(join(W, "package.json"));
const viem = await import(pathToFileURL(req.resolve("viem")).href);
const acc = await import(pathToFileURL(req.resolve("viem/accounts")).href);
const chains = await import(pathToFileURL(req.resolve("viem/chains")).href);
const account = acc.privateKeyToAccount(raw);
say("PAYER ADDRESS :", account.address);
const client = viem.createPublicClient({ chain: chains.base, transport: viem.http(process.env.BASE_RPC || "https://mainnet.base.org") });
const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const bal = await client.readContract({ address: USDC, abi: [{name:"balanceOf",type:"function",stateMutability:"view",inputs:[{name:"a",type:"address"}],outputs:[{type:"uint256"}]}], functionName: "balanceOf", args: [account.address] });
const eth = Number(await client.getBalance({ address: account.address })) / 1e18;
say("USDC on Base  :", (Number(bal) / 1e6).toFixed(6), "USDC");
say("native ETH    :", eth.toFixed(8), "ETH  (facilitator sponsors gas; 0 is expected)");
if (JSON_OUT) console.log(JSON.stringify({ address: account.address, usdc: Number(bal) / 1e6, eth }));
