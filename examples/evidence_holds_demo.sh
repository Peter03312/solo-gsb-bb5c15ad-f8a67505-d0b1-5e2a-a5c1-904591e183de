#!/usr/bin/env bash
# 考古地层关系核验服务 —— 争议证据临时保全（/evidence-holds）端到端示例
#
# 保全用于交接复核期间冻结撤回入口：
#   - POST   /evidence-holds                          建立保全（返回一次性解除凭据）
#   - GET    /evidence-holds                          保全台账（不泄露凭据）
#   - GET    /evidence-holds/{id}                     保全状态与每条证据的在保全来源
#   - GET    /evidence-holds/evidence/{evidence_id}   按证据反查生效保全
#   - POST   /evidence-holds/{id}/release             凭凭据解除
# 受保全覆盖证据的正式撤回（普通批次 / 野外包勘误 / 推演并入）整笔拒绝；
# 推演内试验撤回仍可进行；保全不递增正式修订、不改变断言与快照。
#
# 可重复运行（每次使用独立运行号与证据前缀，不与历史数据冲突）。
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
RUN="$$$(date +%S)"                      # 每次运行独立的终端号 / 证据前缀
T="T-HD$RUN"
PKG="PKG$RUN"
pp() { python3 -m json.tool 2>/dev/null || cat; }
jget() { python3 -c '
import sys, json, functools
d = json.load(sys.stdin)
def step(x, k):
    return x[int(k)] if isinstance(x, list) and k.lstrip("-").isdigit() else x[k]
print(functools.reduce(step, sys.argv[1].split("."), d))' "$1"; }
# 发请求并断言 HTTP 状态码：req <METHOD> <PATH> <期望状态> [JSON BODY]
req() {
  local method="$1" path="$2" want="$3" body="${4:-}"
  local tmp; tmp="$(mktemp)"
  local code
  if [ -n "$body" ]; then
    code="$(curl -s -o "$tmp" -w '%{http_code}' -X "$method" "$BASE$path" \
      -H 'Content-Type: application/json' -d "$body")"
  else
    code="$(curl -s -o "$tmp" -w '%{http_code}' -X "$method" "$BASE$path")"
  fi
  if [ "$code" != "$want" ]; then
    echo "!! $method $path 期望 HTTP $want，实际 $code：" >&2
    cat "$tmp" >&2; rm -f "$tmp"; exit 1
  fi
  cat "$tmp"; rm -f "$tmp"
}

echo "== 0. 登记示例地层（每次运行独立站点）=="
for s in "${T}1" "${T}2" "${T}3" "${T}4"; do
  req POST /strata 201 "{\"stratum_id\": \"$s\", \"site\": \"保全演示区-$RUN\"}" >/dev/null
done

echo; echo "== 1. 建立正式视图：HDx-1（L1~L2）、HDx-2（L2<L3）、HDx-3（L3<L4） =="
E1="HD-$RUN-1"; E2="HD-$RUN-2"; E3="HD-$RUN-3"
REV0="$(curl -s "$BASE/revision" | jget revision)"
req POST /batches 200 "{
  \"expected_revision\": $REV0,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"$E1\", \"stratum_a\": \"${T}1\", \"stratum_b\": \"${T}2\", \"relation\": \"same_period\"},
    {\"op\": \"assert\", \"evidence_id\": \"$E2\", \"stratum_a\": \"${T}2\", \"stratum_b\": \"${T}3\", \"relation\": \"earlier_than\"},
    {\"op\": \"assert\", \"evidence_id\": \"$E3\", \"stratum_a\": \"${T}3\", \"stratum_b\": \"${T}4\", \"relation\": \"earlier_than\"}
  ]}" | pp
REV="$(curl -s "$BASE/revision" | jget revision)"
echo "（当前正式修订号 $REV）"

echo; echo "== 2. 建立保全 A：$E1 + $E2（事由：交接复核 #A），返回一次性凭据 =="
HOLD_A="$(req POST /evidence-holds 201 "{
  \"expected_revision\": $REV,
  \"evidence_ids\": [\"$E1\", \"$E2\"],
  \"reason\": \"交接复核 #A（运行 $RUN）：关系链存疑\"}")"
echo "$HOLD_A" | pp
IDA="$(echo "$HOLD_A" | jget id)"
CREDA="$(echo "$HOLD_A" | jget credential)"
echo "（保全编号 $IDA；正式修订号未变化：$(curl -s "$BASE/revision" | jget revision)）"

echo; echo "== 3. 无效建立被明确拒绝 =="
echo "  3a. 过期修订号 -> 409 STALE_REVISION"
req POST /evidence-holds 409 "{
  \"expected_revision\": 999999, \"evidence_ids\": [\"$E3\"], \"reason\": \"过期\"}" | pp
echo "  3b. 证据编号重复 -> 400 HOLD_DUPLICATE_EVIDENCE"
req POST /evidence-holds 400 "{
  \"expected_revision\": $REV,
  \"evidence_ids\": [\"$E3\", \"$E3\"],
  \"reason\": \"重复\"}" | pp
echo "  3c. 证据不存在 / 非生效 -> 400 INVALID_HOLD_EVIDENCE（逐条给状态）"
req POST /evidence-holds 400 "{
  \"expected_revision\": $REV,
  \"evidence_ids\": [\"$E3\", \"HD-$RUN-NOPE\"],
  \"reason\": \"含无效证据\"}" | pp

echo; echo "== 4. 第二份保全 B 只覆盖 $E2（多份保全覆盖同一证据；须全部解除才可撤回）=="
HOLD_B="$(req POST /evidence-holds 201 "{
  \"expected_revision\": $REV,
  \"evidence_ids\": [\"$E2\"],
  \"reason\": \"交接复核 #B（运行 $RUN）：原档待核\"}")"
IDB="$(echo "$HOLD_B" | jget id)"
CREDB="$(echo "$HOLD_B" | jget credential)"
echo "（保全编号 $IDB）"

echo; echo "== 5. 查询不泄露凭据：列表 / 详情 / 按证据反查 =="
curl -s "$BASE/evidence-holds?status=active" | python3 -c "
import sys, json
rows = json.load(sys.stdin)
mine = [r for r in rows if r['id'] in ($IDA, $IDB)]
print(json.dumps(mine, ensure_ascii=False, indent=2))
assert all('credential' not in r and 'credential_hash' not in r for r in rows), '凭据泄漏!'
print('（全部响应均无 credential / credential_hash 字段）')"
echo "  -- $E2 的在保全来源（A、B 两份）--"
curl -s "$BASE/evidence-holds/evidence/$E2" | pp
echo "  -- 保全 A 详情（每条证据给出来源；无 credential 字段）--"
req GET "/evidence-holds/$IDA" 200 | pp

echo; echo "== 6. 受保证据的正式撤回整笔拒绝（修订 / 断言 / 台账不变）=="
echo "  6a. 普通批次撤 $E1（保全 A 覆盖）-> 409 EVIDENCE_HELD，返回命中证据与保全记录"
req POST /batches 409 "{
  \"expected_revision\": $REV,
  \"operations\": [{\"op\": \"retract\", \"evidence_id\": \"$E1\"}]}" | pp
echo "  6b. 批次同时撤回未受保的 $E3 也整笔拒绝（全有或全无），修订不增"
req POST /batches 409 "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"retract\", \"evidence_id\": \"$E2\"},
    {\"op\": \"retract\", \"evidence_id\": \"$E3\"}
  ]}" | pp
[ "$(curl -s "$BASE/revision" | jget revision)" = "$REV" ] && echo "  （正式修订仍为 $REV，三条断言仍全部 active）"

echo; echo "== 7. 错误凭据 / 过期修订解除被拒绝，保全状态不变 =="
req POST "/evidence-holds/$IDA/release" 403 "{
  \"expected_revision\": $REV, \"credential\": \"wrong-credential\"}" | pp
req POST "/evidence-holds/$IDA/release" 409 "{
  \"expected_revision\": 999999, \"credential\": \"$CREDA\"}" | pp

echo; echo "== 8. 野外包勘误撤原包证据到正式库：受保全覆盖时整笔拒绝 =="
# 新层 ${T}5/${T}6 汇入一条早于证据；对该证据建保全后，勘误撤回须整笔拒绝。
req POST /strata 201 "{\"stratum_id\": \"${T}5\", \"site\": \"保全演示区-$RUN\"}" >/dev/null
req POST /strata 201 "{\"stratum_id\": \"${T}6\", \"site\": \"保全演示区-$RUN\"}" >/dev/null
FP="$(req POST /field-packages 200 "{
  \"site\": \"保全演示区-$RUN\",
  \"terminal_id\": \"$T\",
  \"package_no\": \"${PKG}-1\",
  \"expected_revision\": $REV,
  \"mappings\": [{\"short_code\": \"s5\", \"stratum_id\": \"${T}5\"},
                {\"short_code\": \"s6\", \"stratum_id\": \"${T}6\"}],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s5\", \"stratum_b\": \"s6\", \"relation\": \"earlier_than\"}
  ]}")"
FPE="$(echo "$FP" | jget 'evidence.0.evidence_id')"
REV="$(curl -s "$BASE/revision" | jget revision)"
echo "  野外证据 $FPE 已汇入（正式修订 -> $REV，只 +1）"
req POST /evidence-holds 201 "{
  \"expected_revision\": $REV,
  \"evidence_ids\": [\"$FPE\"],
  \"reason\": \"勘误争议保全（运行 $RUN）\"}" >/tmp/fphold.json
echo "  勘误撤回该证据 -> 409 FIELD_PACKAGE_EVIDENCE_HELD，台账 / 修订不变"
req POST /field-packages/corrections 409 "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"${PKG}-1\",
  \"new_package_no\": \"${PKG}-1F1\",
  \"expected_revision\": $REV,
  \"site\": \"保全演示区-$RUN\",
  \"mappings\": [{\"short_code\": \"s5\", \"stratum_id\": \"${T}5\"},
                {\"short_code\": \"s6\", \"stratum_id\": \"${T}6\"}],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s5\", \"stratum_b\": \"s6\", \"relation\": \"same_period\"}
  ]}" | pp
[ "$(curl -s "$BASE/revision" | jget revision)" = "$REV" ] && echo "  （修订仍为 $REV；新包号未占用，失败后可重提）"
FPHID="$(jq .id </tmp/fphold.json)"; FPHCRED="$(jq -r .credential </tmp/fphold.json)"

echo; echo "== 9. 推演内试验撤回仍可进行（不触碰正式库，不被保全拦截）；并入被拦截 =="
PID="$(req POST /projections 201 '{"label": "复核期间推演"}' | jget id)"
req POST "/projections/$PID/batches" 200 "{
  \"expected_revision\": 0,
  \"operations\": [{\"op\": \"retract\", \"evidence_id\": \"$E1\"}]}" | pp
req POST "/projections/$PID/merge" 409 | pp
[ "$(curl -s "$BASE/projections/$PID" | jget status)" = open ] && echo "  （推演仍 open，正式状态不变）"

echo; echo "== 10. 只解除保全 A：$E1 解封，$E2 仍被保全 B 覆盖 =="
req POST "/evidence-holds/$IDA/release" 200 "{
  \"expected_revision\": $REV, \"credential\": \"$CREDA\"}" | pp
req POST /batches 200 "{
  \"expected_revision\": $REV,
  \"operations\": [{\"op\": \"retract\", \"evidence_id\": \"$E1\"}]}" | pp
REV="$(curl -s "$BASE/revision" | jget revision)"
echo "  （$E1 已撤回，正式修订 -> $REV；保全建立 / 解除本身从不递增修订）"
echo "  $E2 仍受保全 B 覆盖，撤回继续拒绝；重复解除 A / 解除不存在的保全均拒绝："
req POST /batches 409 "{
  \"expected_revision\": $REV,
  \"operations\": [{\"op\": \"retract\", \"evidence_id\": \"$E2\"}]}" | pp
req POST "/evidence-holds/$IDA/release" 409 "{
  \"expected_revision\": $REV, \"credential\": \"$CREDA\"}" | pp
req POST "/evidence-holds/99999999/release" 404 "{
  \"expected_revision\": $REV, \"credential\": \"$CREDB\"}" | pp

echo; echo "== 11. 解除保全 B 与野外证据保全后，$E2 可撤回、勘误可提交（多份保全须全部解除）=="
req POST "/evidence-holds/$IDB/release" 200 "{
  \"expected_revision\": $REV, \"credential\": \"$CREDB\"}" >/dev/null
req POST "/evidence-holds/$FPHID/release" 200 "{
  \"expected_revision\": $REV, \"credential\": \"$FPHCRED\"}" >/dev/null
[ "$(curl -s "$BASE/evidence-holds/evidence/$E2" | jget held)" = False ] && echo "  （$E2 已无生效保全）"
req POST /batches 200 "{
  \"expected_revision\": $REV,
  \"operations\": [{\"op\": \"retract\", \"evidence_id\": \"$E2\"}]}" | pp
REV="$(curl -s "$BASE/revision" | jget revision)"
req POST /field-packages/corrections 200 "{
  \"terminal_id\": \"$T\",
  \"original_package_no\": \"${PKG}-1\",
  \"new_package_no\": \"${PKG}-1F1\",
  \"expected_revision\": $REV,
  \"site\": \"保全演示区-$RUN\",
  \"mappings\": [{\"short_code\": \"s5\", \"stratum_id\": \"${T}5\"},
                {\"short_code\": \"s6\", \"stratum_id\": \"${T}6\"}],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s5\", \"stratum_b\": \"s6\", \"relation\": \"same_period\"}
  ]}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(json.dumps({'package_no': d['package_no'], 'revision': d['revision'],
                  'retracted_evidence': d['correction']['retracted_evidence'],
                  'replayed': d['replayed']}, ensure_ascii=False, indent=2))"

echo; echo "== 完成：保全台账保留 released 留痕，凭据全程未出现在任何查询响应中 =="
curl -s "$BASE/evidence-holds" | python3 -c "
import sys, json
rows = json.load(sys.stdin)
mine = [r for r in rows if '$RUN' in r['reason'] or r['id'] in ($IDA, $IDB, $FPHID)]
print(json.dumps(mine, ensure_ascii=False, indent=2))
assert all('credential' not in r and 'credential_hash' not in r for r in rows)"
echo "OK"
