#!/usr/bin/env bash
# 考古地层关系核验服务 —— 历史定版关系查询“逐步明细按封存时断言返回”端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 场景：定版之后，路径上的支撑证据被三类来源陆续撤回——
#   1) 正式批次撤回 EV-002；
#   2) 野外整包勘误撤回原包证据（F:...:P-01:...）；
#   3) 推演并入撤回 EV-001。
# 每撤一次都复查历史定版查询 GET /snapshots/{version}/relationship：
# 结论、最优证据路径与逐步明细 path_detail（含每步 status）保持封存时口径，
# 并与同一定版封存的成果包证据明细逐步一致；当前正式关系查询
# GET /relationship 继续显示现时状态（撤回后变未定）。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }
# 从 stdin 的 JSON 中提取字段（用法：... | pick '["path_detail"]'）
pick() { python3 -c "import json,sys; d=json.load(sys.stdin); print(json.dumps(d$1, ensure_ascii=False))"; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 L1/L2/L3/L4 =="
for s in L1 L2 L3 L4; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"甲遗址\"}" >/dev/null || true
done
echo "已登记（若此前已存在则返回 STRATUM_EXISTS，不影响后续演示）"

echo; echo "== 2. 批次 1（修订 0 -> 1）：EV-001 L1~L2 同一期，EV-002 L2<L3 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 3. 野外整包汇入（修订 1 -> 2）：终端 T-07 包 P-01，L3<L4 =="
RESP=$(curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d '{
  "site": "甲遗址",
  "terminal_id": "T-07",
  "package_no": "P-01",
  "expected_revision": 1,
  "mappings": [
    {"short_code": "s3", "stratum_id": "L3"},
    {"short_code": "s4", "stratum_id": "L4"}
  ],
  "assertions": [
    {"seq": 1, "stratum_a": "s3", "stratum_b": "s4", "relation": "earlier_than"}
  ]}')
echo "$RESP" | pp
FIELD_EVIDENCE=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['evidence'][0]['evidence_id'])")
echo "原包证据编号: $FIELD_EVIDENCE"

echo; echo "== 4. 定版（修订 2）：版本 1 =="
curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' \
  -d '{"label": "二期定版"}' | pp

echo; echo "== 5. 同一定版封存成果包（L1/L4），供后续比对证据明细 =="
curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d '{"version": 1, "strata": ["L1", "L4"]}' | pp

echo; echo "== 6. 定版查询（撤回前基线）：L1 早于 L4，逐步明细全部 active =="
curl -s "$BASE/snapshots/1/relationship?a=L1&b=L4" | pp

echo; echo "== 7. 来源一：正式批次撤回 EV-002（修订 2 -> 3） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "operations": [{"op": "retract", "evidence_id": "EV-002"}]}' | pp
echo "-- 定版 1 的 L1/L4：结论 / 路径 / 逐步明细保持封存口径（EV-002 仍按封存时 active）--"
curl -s "$BASE/snapshots/1/relationship?a=L1&b=L4" | pp

echo; echo "== 8. 来源二：野外整包勘误撤回原包证据（修订 3 -> 4，P-01 -> P-01F1） =="
curl -s -X POST "$BASE/field-packages/corrections" -H 'Content-Type: application/json' -d '{
  "terminal_id": "T-07",
  "original_package_no": "P-01",
  "new_package_no": "P-01F1",
  "expected_revision": 3,
  "site": "甲遗址",
  "mappings": [
    {"short_code": "s3", "stratum_id": "L3"},
    {"short_code": "s4", "stratum_id": "L4"}
  ],
  "assertions": [
    {"seq": 1, "stratum_a": "s3", "stratum_b": "s4", "relation": "earlier_than"}
  ]}' | pp
echo "-- 定版 1 的 L1/L4：原包证据 $FIELD_EVIDENCE 已被勘误撤回，历史明细仍不变 --"
curl -s "$BASE/snapshots/1/relationship?a=L1&b=L4" | pp

echo; echo "== 9. 来源三：推演并入撤回 EV-001（修订 4 -> 5） =="
RESP=$(curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "撤回 EV-001 推演"}')
echo "$RESP" | pp
PID=$(echo "$RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
curl -s -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [{"op": "retract", "evidence_id": "EV-001"}]}' | pp
curl -s -X POST "$BASE/projections/$PID/merge" | pp
echo "-- 定版 1 的 L1/L4：三类来源撤回后，结论 / 路径 / 逐步明细仍全部保持封存口径 --"
curl -s "$BASE/snapshots/1/relationship?a=L1&b=L4" | pp

echo; echo "== 10. 对照：当前正式关系查询显示现时状态（L1 与 L4 已未定） =="
curl -s "$BASE/relationship?a=L1&b=L4" | pp

echo; echo "== 11. 与同一定版封存的成果包证据明细逐步比对（应完全一致） =="
SNAP_DETAIL=$(curl -s "$BASE/snapshots/1/relationship?a=L1&b=L4" | pick '["path_detail"]')
PKG_DETAIL=$(curl -s "$BASE/packages/1" | python3 -c "
import json, sys
pkg = json.load(sys.stdin)
pair = [p for p in pkg['pairs'] if p['stratum_a'] == 'L1' and p['stratum_b'] == 'L4'][0]
# 包内每步多一个 in_selection 标记；剔除后应与定版查询逐步明细完全一致
print(json.dumps([{k: v for k, v in s.items() if k != 'in_selection'} for s in pair['path_detail']], ensure_ascii=False))")
SNAP_DETAIL="$SNAP_DETAIL" PKG_DETAIL="$PKG_DETAIL" python3 - <<'EOF'
import json, os
snap = json.loads(os.environ["SNAP_DETAIL"])
pkg = json.loads(os.environ["PKG_DETAIL"])
assert snap == pkg, f"定版查询明细与成果包明细不一致:\n{snap}\n{pkg}"
assert all(s["status"] == "active" for s in snap), "历史定版明细须保持封存时 active"
print("一致：定版查询 path_detail == 成果包 pairs[0].path_detail（剔除 in_selection），全部 active")
print(json.dumps(snap, ensure_ascii=False, indent=2))
EOF

echo; echo "== 12. 错误约定不变：未知定版 404 / 未知地层 400 =="
curl -s "$BASE/snapshots/99/relationship?a=L1&b=L4" | pp
curl -s "$BASE/snapshots/1/relationship?a=L1&b=LX" | pp

echo; echo "全部示例执行完毕。"
