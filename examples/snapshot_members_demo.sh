#!/usr/bin/env bash
# 考古地层关系核验服务 —— 定版成员时点一致性端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 修复的缺陷：定版只封存断言与已确定关系，历史关系查询与成果包封存却按当前
# 地层表校验——先定版、再登记新地层后，用旧版查询或封包时，新地层会被误当作
# 旧版的“未定”成员。
#
# 本脚本演示修复后口径：
#   1) 登记 M1/M2、提交断言并定版 v1 —— v1 冻结当时全部已登记地层（成员清单）；
#   2) 定版后登记 M3：用 v1 查询 / 封包涉及 M3 时明确拒绝（400
#      STRATUM_NOT_IN_VERSION），不再误当版内未定；
#   3) v1 内成员查询 / 封包照常；新定版 v2 含 M3，v2 中 M1-M3 为“版内真实未定”
#      （对照：版内未定是 200 undetermined，版外是 4xx 明确拒绝）；
#   4) 可选：用 PSQL_CMD 把 v1 的 members 置 NULL 模拟“既有定版”（成员清单列
#      引入前封存）——冻结记录可证实的成员照常查询；无法证实的地层按 409
#      SNAPSHOT_MEMBERSHIP_UNDETERMINABLE 明确拒绝，不伪造关系。
#
# 每次运行使用独立地层编号（TAG），可重复执行；既有定版演练默认跳过，
# 设置 RUN_LEGACY_SCENARIO=1 并提供 PSQL_CMD 后执行，例如：
#   RUN_LEGACY_SCENARIO=1 PSQL_CMD='docker compose exec -T db psql -U strat -d stratigraphy' \
#     bash examples/snapshot_members_demo.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
TAG="${DEMO_TAG:-SM-$(date +%s)}"
M1="$TAG-M1"; M2="$TAG-M2"; M3="$TAG-M3"
pp() { python3 -m json.tool 2>/dev/null || cat; }
jget() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }
code_of() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error',{}).get('code',''))"; }

echo "== 0. 健康检查（本轮编号前缀: $TAG） =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 $M1 / $M2 并提交断言（$TAG-E1: M1 早于 M2） =="
for s in "$M1" "$M2"; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"成员核查遗址\"}" >/dev/null
done
REV=$(curl -s "$BASE/revision" | jget "['revision']")
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"$TAG-E1\", \"stratum_a\": \"$M1\", \"stratum_b\": \"$M2\", \"relation\": \"earlier_than\"}
  ]}" | pp
curl -s "$BASE/relationship?a=$M1&b=$M2" | pp

echo; echo "== 2. 定版 v1：冻结当时全部已登记地层（成员清单） =="
RESP=$(curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' \
  -d '{"label": "成员核查定版 v1"}')
echo "$RESP" | pp
V1=$(echo "$RESP" | jget "['version']")
echo "-- 定版详情：members 为封存时刻全部已登记地层（按编号排序） --"
curl -s "$BASE/snapshots/$V1" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(json.dumps({'version': d['version'], 'revision': d['revision'],
                  'members': d['members']}, ensure_ascii=False, indent=2))"

echo; echo "== 3. 定版后登记新地层 $M3 =="
curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
  -d "{\"stratum_id\": \"$M3\", \"site\": \"成员核查遗址\"}" | pp

echo; echo "== 4. 旧版查询涉及版外地层：明确拒绝，不误当未定 =="
RESP=$(curl -s "$BASE/snapshots/$V1/relationship?a=$M1&b=$M3")
echo "$RESP" | pp
CODE=$(echo "$RESP" | code_of)
[ "$CODE" = "STRATUM_NOT_IN_VERSION" ] && echo "OK: $M3 不在 v1 成员清单内，400 STRATUM_NOT_IN_VERSION"

echo; echo "== 5. 旧版查询版内成员：照常返回封存结论 =="
curl -s "$BASE/snapshots/$V1/relationship?a=$M1&b=$M2" | pp

echo; echo "== 6. 旧版封包含版外地层：明确拒绝，不留半成品 =="
RESP=$(curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d "{\"version\": $V1, \"strata\": [\"$M1\", \"$M3\"]}")
echo "$RESP" | pp
CODE=$(echo "$RESP" | code_of)
[ "$CODE" = "STRATUM_NOT_IN_VERSION" ] && echo "OK: 含版外地层的封存被拒绝"

echo; echo "== 7. 旧版封包版内成员：201 首次封存；重复封存幂等返回同一份 =="
RESP=$(curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d "{\"version\": $V1, \"strata\": [\"$M1\", \"$M2\"]}")
echo "$RESP" | pp
PKG=$(echo "$RESP" | jget "['id']")
curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d "{\"version\": $V1, \"strata\": [\"$M2\", \"$M1\"]}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['already_exists'] is True and d['id'] == $PKG, d
print('OK: 重复封存返回同一份（already_exists=true, id=%s）' % d['id'])"

echo; echo "== 8. 新定版 v2（成员含 $M3）：版内真实未定 vs 版外拒绝 对照 =="
RESP=$(curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' \
  -d '{"label": "成员核查定版 v2"}')
V2=$(echo "$RESP" | jget "['version']")
echo "$RESP" | pp
echo "-- v2 查询 $M1 - $M3：两者均为 v2 成员但无证据 -> 200 undetermined（版内真实未定） --"
curl -s "$BASE/snapshots/$V2/relationship?a=$M1&b=$M3" | pp
echo "-- 对照：v1 查询同一对 -> 400 STRATUM_NOT_IN_VERSION（$M3 是 v1 版外地层） --"
curl -s "$BASE/snapshots/$V1/relationship?a=$M1&b=$M3" | pp

echo; echo "== 9. 未登记地层错误约定不变：400 INVALID_STRATUM；未知定版 404 =="
curl -s "$BASE/snapshots/$V1/relationship?a=$M1&b=${TAG}-NOPE" | pp
curl -s "$BASE/snapshots/999999/relationship?a=$M1&b=$M2" | pp

# ------------------------------------------------- 可选：既有定版（无成员清单）演练
if [ "${RUN_LEGACY_SCENARIO:-0}" = "1" ]; then
  : "${PSQL_CMD:?需设置 PSQL_CMD，例如 PSQL_CMD='docker compose exec -T db psql -U strat -d stratigraphy'}"

  echo; echo "== 10. 模拟既有定版：把 v1 的 members 置 NULL（成员清单列引入前的封存形态） =="
  # shellcheck disable=SC2086
  $PSQL_CMD -c "UPDATE snapshots SET members = NULL WHERE version = $V1;"
  curl -s "$BASE/snapshots/$V1" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['members'] is None, d
print('OK: v1 现为缺少成员清单的既有定版（members=null）')"

  echo; echo "== 11. 既有定版：冻结记录可证实的成员照常查询（$M1-$M2 在冻结断言中） =="
  curl -s "$BASE/snapshots/$V1/relationship?a=$M1&b=$M2" | pp

  echo; echo "== 12. 既有定版：无法证实的地层按不可判定拒绝，不伪造关系 =="
  RESP=$(curl -s "$BASE/snapshots/$V1/relationship?a=$M1&b=$M3")
  echo "$RESP" | pp
  CODE=$(echo "$RESP" | code_of)
  [ "$CODE" = "SNAPSHOT_MEMBERSHIP_UNDETERMINABLE" ] && \
    echo "OK: 409 SNAPSHOT_MEMBERSHIP_UNDETERMINABLE（$M3 可能是版外新层，也可能是无留痕孤立层，无法判定）"
  RESP=$(curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
    -d "{\"version\": $V1, \"strata\": [\"$M1\", \"$M3\"]}")
  echo "$RESP" | pp
  CODE=$(echo "$RESP" | code_of)
  [ "$CODE" = "SNAPSHOT_MEMBERSHIP_UNDETERMINABLE" ] && echo "OK: 封包同样按不可判定拒绝"

  echo; echo "== 13. 已封成果包保持原样：包 $PKG 内容不变，同集合重复封存仍返回原包 =="
  curl -s "$BASE/packages/$PKG" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['strata'] == sorted(['$M1', '$M2']), d['strata']
print('OK: 已封包内容不变（strata=%s）' % d['strata'])"
  curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
    -d "{\"version\": $V1, \"strata\": [\"$M2\", \"$M1\"]}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['already_exists'] is True and d['id'] == $PKG, d
print('OK: 既有定版下重复封存仍幂等返回原包（id=%s）' % d['id'])"
else
  echo; echo "== 10. （跳过既有定版演练） =="
  echo "    设置 RUN_LEGACY_SCENARIO=1 与 PSQL_CMD 后演示：既有定版（members=NULL）下"
  echo "    可证实成员照常查询、无法证实地层 409 SNAPSHOT_MEMBERSHIP_UNDETERMINABLE、"
  echo "    已封成果包保持原样。自动化回归见 tests/test_snapshot_members.py。"
fi

echo; echo "全部示例执行完毕。"
