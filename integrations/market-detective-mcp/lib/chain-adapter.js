// 팀원의 체인·세금 기능을 연결할 자리입니다. 규격 합의 전에는 시뮬레이션 결과만 돌려줍니다.
// 세율과 보상 수치는 여기서 만들지 않습니다.
export async function anchorFinding(_record) {
  return {
    simulated: true,
    tx_hash: null,
    payout: null,
    note: "체인·세금 연동 전입니다. 온체인 기록과 보상 분배는 실행되지 않았습니다.",
  };
}
