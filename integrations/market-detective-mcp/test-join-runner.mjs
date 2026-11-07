// 가짜 키트로 실행기를 시험합니다. 실제 시장에 접속하지 않습니다.   node test-join-runner.mjs
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync, chmodSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as wait } from "node:timers/promises";

const base = mkdtempSync(join(tmpdir(), "join-runner-"));
const envFile = join(base, ".env");
const findings = join(base, "findings.jsonl");
process.env.MARKET_RUNNER_DIR = join(base, "runner");
process.env.MARKET_RUNNER_ENV = envFile;
process.env.MARKET_RUNNER_FINDINGS = findings;
const { agentStatus, joinMarket, leaveMarket, setAgentPolicy, syncFindings } = await import("./lib/join-runner.js");
const SECRET = "mk-TESTSECRET123";
const store = {
  board: { shops: [{ name: "real-shop" }] },
  resolveShop: (s) => (s === "상점-AAAAAA" ? "real-shop" : s),
  anon: { resolve: (s) => (s === "상점-AAAAAA" ? "real-shop" : s) },
};

function fakeKit() {
  const kit = join(base, "runner", "agent-market");
  mkdirSync(join(kit, ".venv", "bin"), { recursive: true });
  mkdirSync(join(kit, "market-kit"), { recursive: true });
  mkdirSync(join(kit, "bin"), { recursive: true });
  writeFileSync(join(kit, "bin", "cloudflared"), "");
  const python = join(kit, ".venv", "bin", "python");
  // 인자와 환경 변수 이름을 기록하고 대기합니다.
  writeFileSync(python, `#!/bin/sh\necho "[kit] args: $*" >> logs/market-kit.log\necho "[market] joined as ${SECRET}" >> logs/market-kit.log\necho "[seller] untrusted text" >> logs/market-kit.log\nenv | cut -d= -f1 | sort > env-names.txt\nprintf '%s' "$MARKET_OWNER" > owner-seen.txt\nexec -a market_kit.py sleep 30\n`);
  chmodSync(python, 0o755);
  writeFileSync(join(kit, "market-kit", "market_kit.py"), "");
}
const agentDir = (role) => join(base, "runner", "agents", role);
after(() => rmSync(base, { recursive: true, force: true }));

test("잘못된 닉네임은 거부", () => {
  for (const nick of ["a b", "a;rm -rf", "", "x".repeat(33), "$(id)", "한글"]) {
    assert.throws(() => joinMarket({ role: "shop", nickname: nick }, store), /닉네임/);
  }
});

test("확인 없으면 요약만, 설정 없으면 시장 접속 없이 오류", () => {
  const r = joinMarket({ role: "buyer", nickname: "tester" }, store);
  assert.equal(r.status, "not_started");
  assert.equal(r.ready.claim_code_set, false);
  assert.match(r.public_effect, /공개 리더보드/);
  assert.throws(() => joinMarket({ role: "buyer", nickname: "tester", confirmed: true }, store), /MARKET_OWNER_BUYER/);
  assert.equal(existsSync(agentDir("buyer")), false);
});

test("실행, 중복 차단, 비밀 미노출, 종료", async () => {
  fakeKit();
  writeFileSync(envFile, `MARKET_OWNER_BUYER="${SECRET}"\nMARKET_OWNER_SHOP=mk-OTHER\nMCP_PATH_TOKEN=do-not-pass\n`);
  const preview = joinMarket({ role: "buyer", nickname: "tester", orders: 3 }, store);
  assert.equal(preview.status, "not_started");
  assert.ok(!JSON.stringify(preview).includes(SECRET));
  assert.equal(existsSync(join(agentDir("buyer"), "agent.json")), false);

  const started = joinMarket({ role: "buyer", nickname: "tester", orders: 3, confirmed: true }, store);
  assert.equal(started.status, "started");
  assert.ok(!JSON.stringify(started).includes(SECRET));
  await wait(500);
  assert.equal(readFileSync(join(agentDir("buyer"), "owner-seen.txt"), "utf8"), SECRET);
  const names = readFileSync(join(agentDir("buyer"), "env-names.txt"), "utf8");
  assert.ok(!names.includes("MCP_PATH_TOKEN"));
  const log = readFileSync(join(agentDir("buyer"), "logs", "market-kit.log"), "utf8");
  assert.match(log, /join --dir .* --market https:\/\/market\.ourolabs\.ai --name tester --buyer-only --orders 3/);
  assert.ok(!log.includes(`--owner`));

  assert.throws(() => joinMarket({ role: "buyer", nickname: "tester", confirmed: true }, store), /이미 실행 중/);
  assert.throws(() => joinMarket({ role: "shop", nickname: "tester", confirmed: true }, store), /닉네임이/);

  const text = JSON.stringify(agentStatus());
  assert.equal(agentStatus().buyer.running, true);
  for (const leak of [SECRET, base, "shop_key", "buyer_key", "untrusted text"]) assert.ok(!text.includes(leak), leak);

  assert.equal(leaveMarket({ role: "buyer" }).status, "not_stopped");
  assert.equal(agentStatus().buyer.running, true);
  assert.equal(leaveMarket({ role: "buyer", confirmed: true }).status, "stopping");
  await wait(500);
  assert.equal(agentStatus().buyer.running, false);
  assert.equal(leaveMarket({ role: "buyer", confirmed: true }).status, "not_running");
});

test("방침: 가명 해석, 사건 기록 차단, 출력은 가명", () => {
  writeFileSync(findings, `${JSON.stringify({ shop: "상점-AAAAAA" })}\n`);
  const r = setAgentPolicy("buyer", { min_stars: 4, blocked_shops: ["상점-AAAAAA"] }, store);
  assert.equal(r.policy.min_stars, 4);
  assert.ok(!JSON.stringify(r).includes("real-shop"));
  const saved = JSON.parse(readFileSync(join(agentDir("buyer"), "policy.json"), "utf8"));
  assert.deepEqual(saved.blocked_shops, ["real-shop"]);
  assert.deepEqual(saved.blocked_by_findings, ["real-shop"]);
  setAgentPolicy("buyer", { avoid_findings: false }, store);
  assert.deepEqual(JSON.parse(readFileSync(join(agentDir("buyer"), "policy.json"), "utf8")).blocked_by_findings, []);
  assert.throws(() => setAgentPolicy("buyer", { blocked_shops: ["없는상점"] }, store), /찾을 수 없는/);
  assert.throws(() => setAgentPolicy("shop", { target_margin: 0.01, min_margin: 0.5 }, store), /target_margin/);
});
