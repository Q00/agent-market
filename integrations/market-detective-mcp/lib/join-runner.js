// 시장 참가 실행기. Market Kit를 인자 배열로 실행하고 상점과 구매자를 각각 최대 1개만 띄웁니다.
// 클레임 코드는 호출할 때 .env에서 읽어 자식 프로세스 환경으로만 넘깁니다. 어떤 출력에도 넣지 않습니다.
import { spawn, execFileSync } from "node:child_process";
import { chmodSync, closeSync, copyFileSync, existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from "node:fs";
import { delimiter, join } from "node:path";
import { DATA_DIR, ROOT } from "./paths.js";
import { pseudonym } from "./anon.js";

export const MARKET = process.env.MARKET_BASE || "https://hack.ourolabs.ai";
const RUNNER = process.env.MARKET_RUNNER_DIR || join(ROOT, "runner");
const ENV_FILE = process.env.MARKET_RUNNER_ENV || join(ROOT, ".env");
const DEMO = join(ROOT, "demo-agent");
const KIT = process.env.MARKET_KIT_DIR || join(RUNNER, "kit");
const PYTHON = join(KIT, ".venv", "bin", "python");
const KIT_PY = join(KIT, "market_kit.py");
const STATE_FILE = join(RUNNER, "state.json");
const FINDINGS_FILE = process.env.MARKET_RUNNER_FINDINGS || join(DATA_DIR, "findings.jsonl");
const AGENT_FILES = ["rules.py", "seller.py", "market_buyer.py", "restock.py", "review.py", "policy.default.json"];
const NICK = /^[a-zA-Z0-9-]{1,32}$/;
const ROLES = {
  shop: { env: "MARKET_OWNER_SHOP", args: ["--tunnel", "--shop-only"] },
  buyer: { env: "MARKET_OWNER_BUYER", args: ["--buyer-only"] },
};
const PUBLIC_EFFECT = "참가하면 닉네임, 거래, 리뷰가 공개 리더보드와 해시 연결 원장에 기록되고 지울 수 없습니다.";

const dirOf = (role) => join(RUNNER, "agents", role);
const quote = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;

function readJson(path, fallback) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return fallback;
  }
}
function writePrivate(path, value) {
  writeFileSync(path, `${JSON.stringify(value, null, 1)}\n`, { mode: 0o600 });
}
const readState = () => readJson(STATE_FILE, {});
function writeState(state) {
  mkdirSync(RUNNER, { recursive: true, mode: 0o700 });
  writePrivate(STATE_FILE, state);
}

function claimCode(role) {
  let text = "";
  try {
    text = readFileSync(ENV_FILE, "utf8");
  } catch {
    return "";
  }
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^\s*(?:export\s+)?([A-Z_]+)\s*=\s*(.*?)\s*$/);
    if (m && m[1] === ROLES[role].env) return m[2].replace(/^(['"])(.*)\1$/, "$2");
  }
  return "";
}

function isOurKit(pid) {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return execFileSync("ps", ["-o", "command=", "-p", String(pid)], { encoding: "utf8" }).includes("market_kit.py");
  } catch {
    return false;
  }
}
const aliveRole = (state, role) => isOurKit(state[role]?.pid);

function hasTunnelTool() {
  const paths = [join(KIT, "bin"), ...(process.env.PATH || "").split(delimiter)];
  return paths.some((p) => p && existsSync(join(p, "cloudflared")));
}
const kitReady = () => existsSync(PYTHON) && existsSync(KIT_PY);

function prepareAgent(role) {
  const dir = dirOf(role);
  mkdirSync(join(dir, "logs"), { recursive: true, mode: 0o700 });
  chmodSync(dir, 0o700);
  for (const f of AGENT_FILES) copyFileSync(join(DEMO, f), join(dir, f));
  const agent = readFileSync(join(DEMO, "agent.template.json"), "utf8").replaceAll("__PY__", quote(PYTHON).replace(/["\\]/g, "\\$&"));
  writeFileSync(join(dir, "agent.json"), agent, { mode: 0o600 });
  const policy = join(dir, "policy.json");
  if (!existsSync(policy)) writePrivate(policy, {});
  return dir;
}

function validate(role, nickname) {
  if (!ROLES[role]) throw new Error("role은 shop 또는 buyer여야 합니다.");
  if (!NICK.test(nickname || "")) throw new Error("닉네임은 영문, 숫자, 하이픈으로 1~32자여야 합니다.");
}

function effectivePolicy(role) {
  const defaults = readJson(join(DEMO, "policy.default.json"), {})[role] ?? {};
  return { ...defaults, ...readJson(join(dirOf(role), "policy.json"), {}) };
}
function publicPolicy(role) {
  const policy = effectivePolicy(role);
  if (role === "buyer") {
    policy.blocked_shops = (policy.blocked_shops ?? []).map((n) => pseudonym("shop", n));
    policy.blocked_by_findings = (policy.blocked_by_findings ?? []).map((n) => pseudonym("shop", n));
  }
  return policy;
}

export function joinMarket({ role, nickname, orders = 20, confirmed = false }, store) {
  validate(role, nickname);
  const state = readState();
  const other = role === "shop" ? "buyer" : "shop";
  const code = claimCode(role);
  const missing = [];
  if (!kitReady()) missing.push("서버에 Market Kit 준비 (install.sh --no-join)");
  if (!code) missing.push(`서버 .env의 ${ROLES[role].env} (참가 페이지 GitHub 로그인에서 받은 클레임 코드)`);
  if (role === "shop" && !hasTunnelTool()) missing.push("서버에 cloudflared 설치");
  const warnings = [];
  if (code && code === claimCode(other)) warnings.push("상점과 구매자의 클레임 코드가 같습니다. 같은 GitHub 계정의 상점과 구매자는 서로 거래할 수 없습니다.");
  const conflicts = [];
  if (aliveRole(state, role)) conflicts.push(`${role} 에이전트가 이미 실행 중입니다. 역할별 최대 1개만 실행합니다.`);
  if (state[other]?.nickname === nickname) conflicts.push(`닉네임이 ${other} 에이전트와 같습니다. 다른 닉네임을 쓰세요.`);

  const summary = {
    action: "join_market",
    role,
    nickname,
    mode: role === "shop" ? "상점 공개 주소(터널)를 열고 방침대로 입고·판매" : `구매자로 주문 최대 ${orders}건 처리 후 자동 종료`,
    agent: "규칙 기반 데모 에이전트 (LLM 없음)",
    policy: role === "buyer" ? (syncFindings(store), publicPolicy(role)) : publicPolicy(role),
    public_effect: PUBLIC_EFFECT,
    ready: { kit: kitReady(), claim_code_set: Boolean(code), ...(role === "shop" ? { tunnel_tool: hasTunnelTool() } : {}) },
    missing,
    conflicts,
    warnings,
  };
  if (!confirmed) {
    return { status: "not_started", needs_user_confirmation: true, ...summary, next: "사용자에게 이 내용을 보여 주고 승인받은 뒤 confirmed: true로 다시 호출하세요." };
  }
  if (missing.length) {
    throw new Error(`시장에 접속하지 않았습니다. 필요한 설정: ${missing.join("; ")}. 값을 채팅에 붙여넣지 말고 서버 관리자가 설정하세요.`);
  }
  if (conflicts.length) throw new Error(`시장에 접속하지 않았습니다. ${conflicts.join(" ")}`);

  const dir = prepareAgent(role);
  if (role === "buyer") syncFindings(store);
  const args = [KIT_PY, "join", "--dir", dir, "--market", MARKET, "--name", nickname, ...ROLES[role].args];
  if (role === "buyer") args.push("--orders", String(orders));
  const out = openSync(join(dir, "logs", "join.out.log"), "a", 0o600);
  // 서버 비밀 값이 자식에게 넘어가지 않도록 환경 변수를 필요한 것만 전달합니다.
  const env = {
    PATH: [join(KIT, "bin"), process.env.PATH].join(delimiter),
    HOME: process.env.HOME,
    LANG: "en_US.UTF-8",
    PYTHONUTF8: "1",
    MARKET_OWNER: code,
    DEMO_SHOP_NAME: nickname,
  };
  const child = spawn(PYTHON, args, { cwd: dir, env, detached: true, stdio: ["ignore", out, out], shell: false });
  closeSync(out);
  child.on("error", (err) => console.error(`${role} 실행 실패: ${err.code ?? "unknown"}`));
  if (!child.pid) throw new Error("에이전트 프로세스를 시작하지 못했습니다.");
  child.unref();
  writeState({ ...readState(), [role]: { pid: child.pid, nickname, started_at: new Date().toISOString(), orders: role === "buyer" ? orders : null } });
  return { status: "started", ...summary, missing: undefined, conflicts: undefined, next: "등록 결과는 get_my_agent_status로 확인하세요. 터널과 등록에 1분 정도 걸립니다." };
}

export function leaveMarket({ role, confirmed = false }) {
  if (!ROLES[role]) throw new Error("role은 shop 또는 buyer여야 합니다.");
  const state = readState();
  const alive = aliveRole(state, role);
  if (!confirmed) {
    return {
      status: "not_stopped",
      needs_user_confirmation: true,
      role,
      running: alive,
      effect: "에이전트를 멈춥니다. 원장과 리더보드의 기존 기록은 남고, 상점의 진행 중 거래는 끊길 수 있습니다.",
      next: "사용자 승인 뒤 confirmed: true로 다시 호출하세요.",
    };
  }
  if (!alive) return { status: "not_running", role };
  process.kill(-state[role].pid, "SIGTERM");
  return { status: "stopping", role, nickname: state[role].nickname };
}

export function setAgentPolicy(role, changes, store) {
  if (!ROLES[role]) throw new Error("role은 shop 또는 buyer여야 합니다.");
  const next = { ...changes };
  if (role === "buyer" && next.blocked_shops) {
    const known = new Set((store.board?.shops ?? []).map((s) => s.name));
    next.blocked_shops = next.blocked_shops.map((input) => {
      const name = store.resolveShop(input);
      if (!known.has(name)) throw new Error(`찾을 수 없는 상점입니다: ${input}. list_shops의 가명을 쓰세요.`);
      return name;
    });
  }
  if (role === "shop") {
    const target = next.target_margin ?? effectivePolicy(role).target_margin;
    const floor = next.min_margin ?? effectivePolicy(role).min_margin;
    if (target < floor) throw new Error("target_margin은 min_margin보다 작을 수 없습니다.");
  }
  const dir = prepareAgent(role);
  const policy = join(dir, "policy.json");
  writePrivate(policy, { ...readJson(policy, {}), ...next });
  if (role === "buyer") syncFindings(store);
  return {
    status: "saved",
    role,
    applies: "다음 협상·주문부터 적용됩니다. 실행 중인 에이전트도 다시 시작할 필요가 없습니다.",
    policy: publicPolicy(role),
  };
}

// 사건 기록이 있는 상점을 구매자의 차단 목록에 반영합니다. avoid_findings를 끄면 비웁니다.
export function syncFindings(store) {
  const policyFile = join(dirOf("buyer"), "policy.json");
  if (!existsSync(policyFile)) return 0;
  const policy = readJson(policyFile, {});
  const blocked = new Set();
  if (policy.avoid_findings !== false && existsSync(FINDINGS_FILE)) {
    for (const line of readFileSync(FINDINGS_FILE, "utf8").split("\n")) {
      if (!line.trim()) continue;
      const shop = parseLine(line)?.shop;
      const real = shop && store?.anon?.resolve(shop);
      if (real && real !== shop) blocked.add(real);
    }
  }
  policy.blocked_by_findings = [...blocked];
  writePrivate(policyFile, policy);
  return blocked.size;
}
function parseLine(line) {
  try {
    return JSON.parse(line);
  } catch {
    return null;
  }
}

function sanitize(line, secrets) {
  let out = line;
  for (const s of secrets) if (s) out = out.replaceAll(s, "[비공개]");
  return out
    .replace(/https?:\/\/[^\s"'<>)]+/g, "[URL]")
    .replace(/(?:\/(?:Users|home|private|tmp|var|opt)\/)[^\n"']*/g, "[경로]")
    .replace(/Bearer\s+\S+/gi, "Bearer [비공개]")
    .replace(/\bmk-[A-Za-z0-9_-]+/g, "[비공개]")
    .replace(/[A-Za-z0-9+/_=-]{32,}/g, "[비공개]")
    .slice(0, 200);
}

function recentEvents(role, limit = 12) {
  const secrets = [ROOT, RUNNER, process.env.HOME, process.env.MCP_PATH_TOKEN, claimCode("shop"), claimCode("buyer")];
  const file = join(dirOf(role), "logs", "market-kit.log");
  let lines = [];
  try {
    lines = readFileSync(file, "utf8").split("\n");
  } catch {
    return { registered: false, events: [] };
  }
  return {
    registered: lines.some((l) => /\[market\].*(joined|registered|join .*ok)/i.test(l)),
    // 키트와 규칙 코드의 줄만 내보내고, 상대가 보낼 수 있는 글이 섞이는 다른 줄은 뺍니다.
    events: lines.filter((l) => /\[(market|kit|rule)\]/.test(l)).slice(-limit).map((l) => sanitize(l, secrets)),
  };
}

function shopInventory(dir) {
  const inv = readJson(join(dir, ".market", "inventory.json"), null);
  if (!inv) return null;
  const grades = {};
  for (const u of inv.units ?? []) grades[u.grade] = (grades[u.grade] ?? 0) + 1;
  return {
    balance: inv.balance ?? null,
    units: (inv.units ?? []).length,
    units_by_grade: grades,
    restock_open: inv.restock?.open ?? null,
    restock_opens_at: inv.restock?.opens_at ?? null,
  };
}

function buyerPayments(dir) {
  let rows = [];
  try {
    rows = readFileSync(join(dir, ".market", "payments.jsonl"), "utf8").split("\n").filter(Boolean).map(parseLine).filter(Boolean);
  } catch {
    return { attempts: 0, paid: 0, spent: 0 };
  }
  const paid = rows.filter((r) => r.paid);
  return { attempts: rows.length, paid: paid.length, spent: paid.reduce((n, r) => n + (Number(r.price) || 0), 0) };
}

// 개인키, 토큰, 클레임 코드, 로컬 경로를 돌려주지 않습니다.
export function agentStatus() {
  const state = readState();
  const roleStatus = (role) => {
    const dir = dirOf(role);
    if (!state[role] && !existsSync(dir)) return { configured: false, running: false };
    const { registered, events } = recentEvents(role);
    return {
      configured: true,
      running: aliveRole(state, role),
      nickname: state[role]?.nickname ?? null,
      started_at: state[role]?.started_at ?? null,
      registered_in_log: registered,
      ...(role === "shop" ? { inventory: shopInventory(dir) } : { payments: buyerPayments(dir), order_limit: state[role]?.orders ?? null }),
      policy: publicPolicy(role),
      recent_events: events,
    };
  };
  return {
    kit_ready: kitReady(),
    claim_codes_set: { shop: Boolean(claimCode("shop")), buyer: Boolean(claimCode("buyer")) },
    shop: roleStatus("shop"),
    buyer: roleStatus("buyer"),
    notes: ["규칙 기반 데모 에이전트의 상태입니다. 시장의 공개 성과는 get_shop_profile로 확인하세요."],
  };
}
