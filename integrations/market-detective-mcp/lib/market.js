// Agent Market 공개 GET API 호출. 호출 사이 간격을 강제해 시장 서버 부담을 줄입니다.
const MARKET = process.env.MARKET_BASE || "https://hack.ourolabs.ai";
const MIN_GAP_MS = Number(process.env.MARKET_GAP_MS || 700);
let queue = Promise.resolve();
let lastCall = 0;

export function marketGet(path) {
  const run = async () => {
    const wait = lastCall + MIN_GAP_MS - Date.now();
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    lastCall = Date.now();
    let lastError;
    for (let attempt = 0; attempt < 3; attempt++) {
      if (attempt) await new Promise((r) => setTimeout(r, 2000 * attempt));
      try {
        const res = await fetch(`${MARKET}${path}`, {
          signal: AbortSignal.timeout(30_000),
          headers: { "User-Agent": "market-detective-mcp/0.2" },
        });
        if (res.ok) return await res.json();
        lastError = new Error(`마켓 API 오류: ${path.split("?")[0]} → HTTP ${res.status}`);
        if (res.status < 500 && res.status !== 429) throw lastError;
      } catch (err) {
        lastError = err;
        if (err.message?.startsWith("마켓 API 오류") && !/HTTP (5|429)/.test(err.message)) throw err;
      } finally {
        lastCall = Date.now();
      }
    }
    throw lastError;
  };
  const p = queue.then(run, run);
  queue = p.catch(() => {});
  return p;
}

// 최근 entries건을 오래된 순으로 받아옵니다. API는 호출당 최대 500건입니다.
export async function fetchRecentLedger(entries) {
  const newest = await marketGet("/api/ledger?after=-500");
  if (!newest.length || entries <= 500) return newest.slice(-entries);
  const latest = newest.at(-1).seq;
  const out = [];
  let after = latest - entries;
  while (true) {
    const page = await marketGet(`/api/ledger?after=${after}`);
    if (!page.length) break;
    out.push(...page);
    after = page.at(-1).seq;
    if (after >= latest || page.length < 500) break;
  }
  return out;
}

export { MARKET };
