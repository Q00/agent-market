// 수사 지표 계산. 위반 판정을 내리지 않고 지표와 근거 seq만 돌려줍니다.
import { untrusted, TRUST_NOTICE } from "./untrusted.js";

export const INDICATOR_NOTE = "지표와 근거만 제공합니다. 위반 여부는 판단하지 않으며, 판단은 수사관의 몫입니다.";
const round = (x, d = 4) => (x == null || Number.isNaN(x) ? null : Number(x.toFixed(d)));
const rate = (num, den) => ({ value: den ? round(num / den) : null, numerator: num, denominator: den });
const median = (xs) => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const percentile = (xs, x) => (xs.length ? round(xs.filter((v) => v <= x).length / xs.length, 3) : null);

function shopEvents(store, shopName) {
  const results = [];
  const settles = [];
  const broken = [];
  for (const e of store.ledger) {
    const url = e.data?.shop;
    if (typeof url !== "string" || store.shopName(url) !== shopName) continue;
    if (e.kind === "result") results.push(e);
    else if (e.kind === "settle") settles.push(e);
    else if (e.kind === "broken") broken.push(e);
  }
  return { results, settles, broken };
}

function priceVsList(store, settles) {
  const ratios = [];
  for (const e of settles) {
    const list = store.catalogBySku.get(e.data.sku)?.list_price;
    if (list && e.data.amount != null) ratios.push(e.data.amount / list);
  }
  return { median_paid_to_list: round(median(ratios)), sample_size: ratios.length };
}

export async function shopProfile(store, input) {
  const shopName = store.resolveShop(input);
  const a = store.anon;
  const row = (store.board?.shops ?? []).find((s) => s.name === shopName);
  const { results, settles, broken } = shopEvents(store, shopName);
  if (!row && !results.length && !settles.length) {
    return { found: false, message: "해당 상점을 찾지 못했습니다. 이름이나 가명을 확인하세요." };
  }
  const peers = (store.board?.shops ?? []).filter((s) => s.deals >= 100);
  const peerRate = (f) => peers.map(f).filter((v) => v != null);
  const brokenRates = peerRate((s) => s.broken / s.deals);
  const stars = peerRate((s) => s.stars);
  const notHonest = settles.filter((e) => e.data.claim_honest === false);
  const withReceipt = results.filter((e) => e.data.receipt);
  const unverified = withReceipt.filter((e) => e.data.verified === false);
  const failedChecks = {};
  for (const e of unverified) {
    for (const [k, v] of Object.entries(e.data.checks ?? {})) if (v === false) failedChecks[k] = (failedChecks[k] || 0) + 1;
  }
  const rev = await store.reviews(shopName);
  const authors = {};
  const dist = {};
  const owners = new Set();
  for (const r of rev.items) {
    authors[r.author] = (authors[r.author] || 0) + 1;
    dist[r.stars] = (dist[r.stars] || 0) + 1;
    if (r.owner?.login) owners.add(r.owner.login);
  }
  const topAuthors = Object.entries(authors).sort((x, y) => y[1] - x[1]);
  const flagged = rev.items
    .map((r) => ({ r, u: untrusted(r.review, "shop_review", a, 300) }))
    .filter((x) => x.u.injection_signals.length);
  return {
    found: true,
    data_source: store.sourceInfo(),
    note: INDICATOR_NOTE,
    trust_notice: TRUST_NOTICE,
    shop: a.name(shopName, "shop"),
    owner: a.name(store.shopOwner.get(shopName.toLowerCase()), "owner"),
    round_totals: row
      ? {
          sample: "현재 라운드 누적 (/api/board)",
          deals: row.deals,
          reviews: row.reviews,
          stars: row.stars,
          revenue: row.revenue,
          book: row.book,
          bankruptcies: row.bankruptcies,
          honest_claim_rate: rate(row.honest_claims, row.claims),
          broken_rate: rate(row.broken, row.deals),
          refund_rate: rate(row.refunds, row.deals),
        }
      : null,
    peer_comparison: row
      ? {
          peer_group: "라운드 누적 거래 100건 이상 상점",
          peer_count: peers.length,
          stars: { shop: row.stars, peer_median: round(median(stars), 3), percentile: percentile(stars, row.stars) },
          broken_rate: {
            shop: row.deals ? round(row.broken / row.deals) : null,
            peer_median: round(median(brokenRates)),
            percentile: row.deals ? percentile(brokenRates, row.broken / row.deals) : null,
          },
        }
      : null,
    ledger_window: {
      results: results.length,
      results_with_receipt: withReceipt.length,
      settled: settles.length,
      broken_events: broken.length,
      verified_rate_among_receipts: rate(withReceipt.length - unverified.length, withReceipt.length),
      failed_checks: failedChecks,
      price: priceVsList(store, settles),
      not_honest_claim_seqs: notHonest.slice(0, 20).map((e) => e.seq),
      broken_seqs: broken.slice(0, 20).map((e) => e.seq),
      unverified_seqs: unverified.slice(0, 20).map((e) => e.seq),
    },
    review_sample: {
      available: rev.available,
      sample_size: rev.items.length,
      star_distribution: dist,
      unique_authors: topAuthors.length,
      unique_owners: owners.size,
      top_author_share: rate(topAuthors[0]?.[1] ?? 0, rev.items.length),
      flagged_for_injection: flagged.length,
      flagged_samples: flagged.slice(0, 5).map((x) => ({ author: a.name(x.r.author, "buyer"), stars: x.r.stars, ...x.u })),
    },
  };
}

function buyerShares(store, shopName) {
  const counts = new Map();
  for (const e of store.ledger) {
    if (e.kind !== "settle" || store.shopName(e.data.shop) !== shopName) continue;
    counts.set(e.data.buyer, (counts.get(e.data.buyer) || 0) + 1);
  }
  return counts;
}

// 윈도 안의 정산 기록으로 상점별 최대 구매자 비중 분포를 만듭니다.
function windowBaseline(store, minDeals) {
  const byShop = new Map();
  for (const e of store.ledger) {
    if (e.kind !== "settle") continue;
    const s = store.shopName(e.data.shop);
    const m = byShop.get(s) ?? new Map();
    m.set(e.data.buyer, (m.get(e.data.buyer) || 0) + 1);
    byShop.set(s, m);
  }
  const shares = [];
  for (const m of byShop.values()) {
    const total = [...m.values()].reduce((a, b) => a + b, 0);
    if (total >= minDeals) shares.push(Math.max(...m.values()) / total);
  }
  return shares;
}

export function relationships(store, input, topN = 5, minDeals = 20) {
  const shopName = store.resolveShop(input);
  const a = store.anon;
  const counts = buyerShares(store, shopName);
  const total = [...counts.values()].reduce((x, y) => x + y, 0);
  const results = store.ledger.filter((e) => e.kind === "result" && e.data?.shop && store.shopName(e.data.shop) === shopName);
  if (!total && !results.length) {
    return { found: false, message: "윈도 안에 이 상점의 거래가 없습니다.", data_source: store.sourceInfo() };
  }
  const shopOwner = store.shopOwner.get(shopName.toLowerCase());
  const sorted = [...counts.entries()].sort((x, y) => y[1] - x[1]);
  const starsByBuyer = new Map();
  for (const e of results) {
    if (e.data.stars == null) continue;
    const l = starsByBuyer.get(e.data.buyer) ?? [];
    l.push(e.data.stars);
    starsByBuyer.set(e.data.buyer, l);
  }
  const avg = (l) => (l?.length ? round(l.reduce((x, y) => x + y, 0) / l.length, 2) : null);
  const allStars = [...starsByBuyer.values()].flat();
  const top = sorted.slice(0, topN).map(([buyer, deals]) => {
    const owner = store.buyerOwner.get(String(buyer).toLowerCase());
    const seqs = store.ledger
      .filter((e) => e.kind === "settle" && e.data.buyer === buyer && store.shopName(e.data.shop) === shopName)
      .slice(-5)
      .map((e) => e.seq);
    return {
      buyer: a.name(buyer, "buyer"),
      owner: a.name(owner, "owner"),
      same_owner_as_shop: Boolean(owner && shopOwner && owner === shopOwner),
      settled_deals: deals,
      share: rate(deals, total),
      avg_stars_given: avg(starsByBuyer.get(buyer)),
      stars_sample_size: starsByBuyer.get(buyer)?.length ?? 0,
      evidence_seqs: seqs,
    };
  });
  // 소유자 간 상호 거래: 상점 소유자의 구매자가 상대 소유자의 상점에서도 샀는지 봅니다.
  const reciprocal = [];
  const myBuyers = store.ownerAgents.get(shopOwner)?.buyers ?? new Set();
  const otherOwners = new Map();
  for (const [buyer, deals] of counts) {
    const o = store.buyerOwner.get(String(buyer).toLowerCase());
    if (o && o !== shopOwner) otherOwners.set(o, (otherOwners.get(o) || 0) + deals);
  }
  for (const [other, inbound] of otherOwners) {
    const theirShops = store.ownerAgents.get(other)?.shops ?? new Set();
    const back = store.ledger.filter(
      (e) => e.kind === "settle" && myBuyers.has(e.data.buyer) && theirShops.has(store.shopName(e.data.shop))
    );
    if (back.length) {
      reciprocal.push({
        other_owner: a.name(other, "owner"),
        deals_their_buyers_to_this_shop: inbound,
        deals_this_owner_buyers_to_their_shops: back.length,
        evidence_seqs: back.slice(-5).map((e) => e.seq),
      });
    }
  }
  // 검증에 실패한 영수증과 똑같이 반복되는 리뷰를 구매자별로 묶습니다.
  const failedByBuyer = new Map();
  const reviewCounts = new Map();
  for (const e of results) {
    if (typeof e.data.review === "string" && e.data.review.trim()) {
      const r = reviewCounts.get(e.data.review) ?? { count: 0, seqs: [], signals: null };
      r.count += 1;
      r.seqs.push(e.seq);
      reviewCounts.set(e.data.review, r);
    }
    if (!e.data.receipt || e.data.verified !== false) continue;
    const f = failedByBuyer.get(e.data.buyer) ?? { count: 0, seqs: [], stars: [], checks: {} };
    f.count += 1;
    f.seqs.push(e.seq);
    if (e.data.stars != null) f.stars.push(e.data.stars);
    for (const [k, v] of Object.entries(e.data.checks ?? {})) if (v === false) f.checks[k] = (f.checks[k] || 0) + 1;
    failedByBuyer.set(e.data.buyer, f);
  }
  const unverifiedReceipts = [...failedByBuyer.entries()]
    .sort((x, y) => y[1].count - x[1].count)
    .slice(0, 10)
    .map(([buyer, f]) => {
      const owner = store.buyerOwner.get(String(buyer).toLowerCase());
      return {
        buyer: a.name(buyer, "buyer"),
        owner: a.name(owner, "owner"),
        unverified_receipts: f.count,
        avg_stars_given: avg(f.stars),
        failed_checks: f.checks,
        evidence_seqs: f.seqs.slice(-5),
      };
    });
  const repeatedReviews = [...reviewCounts.entries()]
    .filter(([, r]) => r.count >= 3)
    .sort((x, y) => y[1].count - x[1].count)
    .slice(0, 5)
    .map(([text, r]) => ({ count: r.count, evidence_seqs: r.seqs.slice(-5), ...untrusted(text, "ledger_review", a, 200) }));
  reciprocal.sort((x, y) => y.deals_their_buyers_to_this_shop + y.deals_this_owner_buyers_to_their_shops - x.deals_their_buyers_to_this_shop - x.deals_this_owner_buyers_to_their_shops);
  const shares = windowBaseline(store, minDeals);
  const topShare = total ? sorted[0][1] / total : null;
  const hhi = total ? sorted.reduce((s, [, d]) => s + (d / total) ** 2, 0) : null;
  const sameOwnerDeals = sorted
    .filter(([b]) => store.buyerOwner.get(String(b).toLowerCase()) === shopOwner && shopOwner)
    .reduce((s, [, d]) => s + d, 0);
  return {
    found: true,
    data_source: store.sourceInfo(),
    note: `${INDICATOR_NOTE} 소유자 일치는 GitHub 계정 기준이며 실제 인물과 같다는 뜻이 아닙니다.`,
    shop: a.name(shopName, "shop"),
    owner: a.name(shopOwner, "owner"),
    sample: { settled_deals: total, unique_buyers: counts.size, results: results.length },
    small_sample_warning: total < minDeals ? `정산 ${total}건으로 표본이 작아 비중 지표를 신뢰하기 어렵습니다.` : null,
    concentration: {
      top1_share: round(topShare),
      herfindahl: round(hhi),
      same_owner_deals: rate(sameOwnerDeals, total),
      baseline: {
        group: `윈도 내 정산 ${minDeals}건 이상 상점`,
        shop_count: shares.length,
        median_top1_share: round(median(shares)),
        this_shop_percentile: topShare == null ? null : percentile(shares, topShare),
      },
    },
    stars: { average_all: avg(allStars), sample_size: allStars.length },
    top_buyers: top,
    reciprocal_owner_links: reciprocal.slice(0, 5),
    unverified_receipts_by_buyer: unverifiedReceipts,
    repeated_reviews: repeatedReviews,
    trust_notice: TRUST_NOTICE,
  };
}

function sanitizeEntry(store, e) {
  const a = store.anon;
  const d = structuredClone(e.data ?? {});
  const texts = [];
  if (d.review != null) {
    texts.push({ field: "review", ...untrusted(d.review, "ledger_review", a) });
    delete d.review;
  }
  if (typeof d.shop === "string") d.shop = a.name(store.shopName(d.shop), "shop");
  if (typeof d.buyer === "string") d.buyer = a.name(d.buyer, "buyer");
  if (e.kind === "join") {
    d.name = a.name(d.name, d.role === "shop" ? "shop" : "buyer");
    delete d.url;
    delete d.pubkey;
  }
  if (d.receipt && typeof d.receipt === "object") {
    if (d.receipt.shop) d.receipt.shop = a.name(store.shopName(e.data.shop), "shop");
    if (d.receipt.sig_seller) d.receipt.sig_seller = `${d.receipt.sig_seller.slice(0, 12)}…`;
  }
  if (d.sig_buyer) d.sig_buyer = `${d.sig_buyer.slice(0, 12)}…`;
  return { data: a.deep(d), untrusted_texts: texts };
}

export function evidence(store, seqs = [], forumQuery, forumLimit = 10, fixtures = []) {
  const a = store.anon;
  const entries = seqs.map((seq) => {
    const e = store.bySeq.get(seq);
    if (!e) return { seq, found: false, message: "현재 데이터 윈도에 없는 번호입니다." };
    const prev = store.bySeq.get(seq - 1);
    const { data, untrusted_texts } = sanitizeEntry(store, e);
    return {
      seq,
      found: true,
      kind: e.kind,
      round: e.round,
      time_utc: new Date(e.ts * 1000).toISOString(),
      data,
      untrusted_texts,
      hash: e.hash,
      prev: e.prev,
      chain_link: prev ? { checked_against_seq: prev.seq, prev_matches: prev.hash === e.prev } : { checked_against_seq: null, prev_matches: null },
    };
  });
  let posts = store.forum ?? [];
  const totalScanned = posts.length;
  if (forumQuery) {
    const raw = store.anon.resolve(forumQuery).toLowerCase();
    posts = posts.filter((p) => (p.text ?? "").toLowerCase().includes(raw) || String(p.author).toLowerCase() === raw);
  }
  const forum = forumQuery != null || fixtures.length
    ? posts.slice(0, forumLimit).map((p) => ({
        id: p.id,
        author: a.name(p.author, p.role === "shop" ? "shop" : "buyer"),
        role: p.role,
        owner: a.name(p.owner, "owner"),
        likes: p.likes,
        dislikes: p.dislikes,
        time_utc: p.ts ? new Date(p.ts * 1000).toISOString() : null,
        ref_seqs: (p.refs ?? []).map((r) => r.seq),
        ...untrusted(p.text, "forum_post", a),
      }))
    : [];
  const demo = fixtures.map((f) => ({
    id: f.id,
    author: f.author,
    synthetic_demo_data: true,
    disclosure: "데모용으로 만든 시뮬레이션 글이며 실제 시장 기록이 아닙니다.",
    ...untrusted(f.text, "demo_fixture_forum_post", a),
  }));
  return {
    data_source: store.sourceInfo(),
    note: "서명과 URL은 축약·가명 처리되어 원본 hash와 재계산으로 대조할 수 없습니다. chain_link는 앞 기록 hash와의 연결만 확인하며, 해시 공식이 없어 내용 재계산은 하지 않았습니다.",
    trust_notice: TRUST_NOTICE,
    ledger_entries: entries,
    forum: { scanned: totalScanned, matched: forumQuery != null ? posts.length : null, posts: forum },
    demo_fixture_posts: demo,
  };
}

// 상점명을 몰라도 조사를 시작할 수 있는 목록. 실명, URL, 소유자, 상점이 쓴 글은 내보내지 않습니다.
const LIST_SORT = {
  deals: (s) => s.deals,
  revenue: (s) => s.revenue,
  stars: (s) => s.stars,
  reviews: (s) => s.reviews,
  honest_claim_rate: (s) => s.honest_claim_rate,
  broken_rate: (s) => s.broken_rate,
  refund_rate: (s) => s.refund_rate,
};

function categorySignal(store, name) {
  const row = (store.shops?.shops ?? []).find((s) => s.name === name);
  const units = new Map();
  for (const [sku, qty] of Object.entries(row?.stock ?? {})) {
    const category = store.catalogBySku?.get?.(sku)?.category;
    if (!category || !(qty > 0)) continue;
    const label = category.split(">").slice(0, 2).join(">");
    units.set(label, (units.get(label) ?? 0) + qty);
  }
  return [...units.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3).map(([category, stock_units]) => ({ category, stock_units }));
}

export function listShops(store, { sort_by = "deals", include_house = true, min_deals = 0, limit = 30 } = {}) {
  const key = LIST_SORT[sort_by];
  const rows = (store.board?.shops ?? [])
    .filter((s) => (include_house || !s.house) && (s.deals ?? 0) >= min_deals)
    .map((s) => ({
      shop: store.anon.name(s.name, "shop"),
      house: Boolean(s.house),
      alive: s.alive ?? null,
      deals: s.deals ?? 0,
      revenue: s.revenue ?? null,
      stars: s.stars ?? null,
      reviews: s.reviews ?? 0,
      honest_claim_rate: s.claims ? round(s.honest_claims / s.claims) : null,
      broken_rate: s.deals ? round(s.broken / s.deals) : null,
      refund_rate: s.deals ? round(s.refunds / s.deals) : null,
      stock_units: s.stock_units ?? null,
      category_signal: categorySignal(store, s.name),
    }))
    .sort((a, b) => (key(b) ?? -Infinity) - (key(a) ?? -Infinity));
  return {
    total_matching: rows.length,
    shops: rows.slice(0, limit),
    data_source: store.sourceInfo(),
    notes: [
      INDICATOR_NOTE,
      "category_signal은 현재 재고의 카테고리에서 추정한 업종 신호이며, 상점이 밝힌 업종이 아닙니다. 재고가 없으면 비어 있습니다.",
      "house: true는 시장 운영 측(팀원/운영사)이 띄운 상점 표시입니다. 일반 참가자가 아니라 운영 측이 통제하는 에이전트이므로, 부정행위 판정의 대상이 아니라 통제된 시연·시드 상점으로 다루세요.",
      "자세한 조사는 가명을 get_shop_profile과 analyze_relationships에 넘기세요.",
    ],
  };
}
