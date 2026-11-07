# 마켓 수사대 MCP (Market Detective)

OBA.RUN 해커톤 — **vooy ↔ Agent Market(hack.ourolabs.ai, Maroo 테스트넷)** 연동.
사람이 메신저(vooy)로 말하면, 그 뒤의 MCP 서버가 A2A 에이전트 시장을 **수사하고(조회·사건 기록)**, 필요하면 **판매자/구매자로 참가**합니다. 시장의 신뢰 계층(영수증 위조·별점 거래·자전거래 적발)을 사람이 다룰 수 있게 하는 게 목적입니다.

## 구성

```
server.js            # MCP 서버(Streamable HTTP). 공개 GET API만 읽고, 사건은 로컬에만 기록
lib/
  market.js          # Agent Market 공개 API 호출(간격 제한), MARKET_BASE로 대상 전환
  analysis.js        # 상점 프로필/관계/증거/상점목록 분석(가명 처리)
  findings.js        # record_finding — 로컬 사건 기록
  join-runner.js     # 판매자/구매자 참가 실행기(Maroo 키트 호출). 비밀값 격리
  store.js anon.js untrusted.js paths.js chain-adapter.js
collect.js           # 공개 API 스냅샷 수집(data/는 커밋 제외)
demo-agent/          # 규칙 기반 데모 에이전트(LLM 없음). 판매자 서명=Maroo 지갑 EIP-191
board/               # 간단한 보드 페이지
참가-준비.md         # 실제 참가(join) 준비 체크리스트
demo-cases.md        # 시연 후보 사건 정리
```

## MCP 도구

조회/수사: `get_market_status`, `list_shops`, `get_shop_profile`, `analyze_relationships`, `get_evidence`, `record_finding`
참가/운영: `join_market`, `get_my_agent_status`, `set_agent_policy`, `leave_market`

## 실행

```bash
npm install
# 필요한 환경변수는 .env (커밋 안 됨)
#   MCP_PATH_TOKEN   : 엔드포인트 경로 토큰(비밀)
#   MARKET_BASE      : 대상 시장(기본값 https://hack.ourolabs.ai)
#   MARKET_OWNER_SHOP / MARKET_OWNER_BUYER : 참가 클레임 코드(참가 시)
node --env-file=.env server.js
```

## 대상 시장 (Maroo 테스트넷)

- base: `https://hack.ourolabs.ai` — 같은 Agent Market 앱을 운영사 **Maroo 테스트넷**(chain_id 450815, 자산 tOKRW)으로 올린 버전
- 라운드 4부터 거래가 지갑→지갑 **온체인 tOKRW 전송**. 참가 키트는 `github.com/Q00/agent-market`의 Maroo 키트 사용
- 참가 실행기는 `runner/kit`(=그 레포의 `publish/scripts`)를 호출. `runner/`는 키·venv라 커밋 제외

## 비밀값 원칙

`.env`, `.mcp_url`, `*.key`, `*.pem`, `data/`, `runner/`, `*.log`는 `.gitignore`로 저장소에서 제외됩니다.
개인키·토큰·클레임 코드는 서버에만 두고 도구 출력/대화에 노출하지 않습니다.
