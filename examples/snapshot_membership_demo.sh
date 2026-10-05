#!/usr/bin/env bash
# 考古地层关系核验服务 —— 定版成员时点一致性端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 修复的问题：定版只封存断言与已确定关系，旧版查询 / 成果包封存却按当前
# 地层表校验成员。“先定版、后登记新地层”后，用旧版查询或封包时，新地层会
# 被误当作旧版的“未定”成员，与版内真实未定混淆。
#
# 本示例演示新口径：
#   1) 定版冻结当时全部已登记地层（响应 / 详情带 members、membership_status）；
#   2) 旧版关系查询只接受该版成员：定版后新登记地层明确 400
#      STRATUM_OUTSIDE_VERSION，版内成员无结论行才是该版真实“未定”；
#   3) 旧版成果包封存同样拒绝版外成员；已封成果包内容保持原样、重复幂等；
#   4) 缺少成员清单的既有定版（membership_status=reconstructed）只对历史记录
#      可证实成员给出结论，不可证实时 409 SNAPSHOT_MEMBERSHIP_UNDECIDABLE
#      （details 附 provable_members），不伪造关系。
#
# 第 4 类“既有定版”需数据库已有升级前旧定版；本脚本用唯一前缀数据演示
# frozen 新定版口径，reconstructed 口径见脚本末尾的说明与 e2e 测试
# tests/test_snapshot_membership_point_in_time.py。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
TS="$(date +%s)"
A="SM${TS}-A"   # 定版前已登记
B="SM${TS}-B"   # 定版前已登记
C="SM${TS}-C"   # 定版前已登记（与 A、B 无断言：版内真实未定）
D="SM${TS}-D"   # 定版后才登记：版外
pp() { local d; d="$(cat)"; printf '%s' "$d" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$d"; }
show() { echo "HTTP $1"; }
REV() { curl -s "$BASE/revision" | python3 -c "import json,sys; print(json.load(sys.stdin)['revision'])"; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记三个定版前地层 A/B/C =="
for s in "$A" "$B" "$C"; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"时点示例\"}" >/dev/null
done
echo "已登记 $A / $B / $C"

echo; echo "== 2. 断言 A 早于 B（修订 $(REV) -> +1）；C 无任何断言 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $(REV),
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"EV-${TS}-1\", \"stratum_a\": \"$A\",
     \"stratum_b\": \"$B\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 3. 定版：冻结当时全部已登记地层（members=3, frozen）=="
SNAP=$(curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' \
  -d '{"label": "时点一致性示例定版"}')
echo "$SNAP" | pp
VER=$(echo "$SNAP" | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])")

echo; echo "== 4. 定版详情可见冻结成员清单 strata 与 membership_status =="
curl -s "$BASE/snapshots/$VER" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('version=%s membership_status=%s strata=%s' % (d['version'], d['membership_status'], d['strata']))"

echo; echo "== 5. 定版后登记新地层 D =="
curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
  -d "{\"stratum_id\": \"$D\", \"site\": \"时点示例\"}" | pp

echo; echo "== 6. 旧版查询版内确定关系 A vs B（结论 / 路径 / 逐步明细封存口径）=="
curl -s "$BASE/snapshots/$VER/relationship?a=$A&b=$B" | pp

echo; echo "== 7. 旧版查询版内真实未定 A vs C（无断言，但二者都是版内成员）=="
curl -s "$BASE/snapshots/$VER/relationship?a=$A&b=$C" | pp

echo; echo "== 8. 旧版查询版外 A vs D -> 400 STRATUM_OUTSIDE_VERSION（不是 undetermined）=="
R=$(curl -s -w '\n[HTTP %{http_code}]' "$BASE/snapshots/$VER/relationship?a=$A&b=$D" || true)
echo "$R" | pp || echo "$R"

echo; echo "== 9. 旧版封存成果包含 D -> 同样 400 STRATUM_OUTSIDE_VERSION =="
R=$(curl -s -w '\n[HTTP %{http_code}]' -X POST "$BASE/packages" \
  -H 'Content-Type: application/json' \
  -d "{\"version\": $VER, \"strata\": [\"$A\", \"$D\"]}" || true)
echo "$R" | pp || echo "$R"

echo; echo "== 10. 版内成员封包（含真实未定对 A/C，未定不附路径）-> 201 =="
PKG=$(curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d "{\"version\": $VER, \"strata\": [\"$C\", \"$A\", \"$B\"]}")
echo "$PKG" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('package id=%s version=%s strata=%s' % (d['id'], d['version'], d['strata']))
for p in d['pairs']:
    print(' pair:', p['stratum_a'], p['relation'], p['stratum_b'])
print('content_hash:', d['content_hash'])"

echo; echo "== 11. 同集合（换顺序）重复封存 -> 200 幂等，content_hash 不变 =="
curl -s -w "[HTTP %{http_code}]\n" -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d "{\"version\": $VER, \"strata\": [\"$A\", \"$B\", \"$C\"]}" \
  -o /tmp/sm_pkg2.json
python3 - <<EOF
import json
first = json.loads('''$PKG''')
second = json.load(open('/tmp/sm_pkg2.json'))
assert first['content_hash'] == second['content_hash']
print("幂等返回同一份包，content_hash =", second['content_hash'])
EOF

echo; echo "== 12. 其他错误结构不变 =="
echo "-- 当前未登记地层（ZZ）仍 400 INVALID_STRATUM --"
R=$(curl -s -w ' [HTTP %{http_code}]' "$BASE/snapshots/$VER/relationship?a=$A&b=SM${TS}-ZZ" || true)
echo "$R" | pp || echo "$R"
echo "-- 未知定版仍 404 SNAPSHOT_NOT_FOUND --"
R=$(curl -s -w ' [HTTP %{http_code}]' "$BASE/snapshots/999999/relationship?a=$A&b=$B" || true)
echo "$R" | pp || echo "$R"
echo "-- 当前正式关系查询不受定版影响（D 若与现时断言有关仍按现时返回） --"
curl -s "$BASE/relationship?a=$A&b=$B" | pp

echo; echo "全部 frozen 新定版示例执行完毕。"
cat <<EOF

提示（缺少成员清单的既有定版 reconstructed）：
  GET /snapshots/{version} 可见 "membership_status": "reconstructed" 与重建出的
  "strata"（只含能由该版冻结断言端点与冻结结论行证实的成员）。
  - 查询 / 封包涉及清单内成员：按版内结论返回（无结论行即真实未定）；
  - 涉及清单外、当前已登记的地层：409 SNAPSHOT_MEMBERSHIP_UNDECIDABLE，
    details.provable_members 列出可证实成员——明确不可判定，不伪造关系；
  - 已封存成果包内容、版内结论与证据路径保持原样。
  完整复现见：python3 tests/test_snapshot_membership_point_in_time.py
EOF
