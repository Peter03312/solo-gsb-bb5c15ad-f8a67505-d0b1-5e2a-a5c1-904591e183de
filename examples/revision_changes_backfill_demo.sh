#!/usr/bin/env bash
# 考古地层关系核验服务 —— 历史修订启动回填（封口/待补交错）修复核查示例
#
# 背景缺陷：已封口修订与待补修订交错时，旧回填把待补修订与空库比较。
#   修订 1（已封口）：E1 同一期(甲,乙)、E2 早于(乙,丙) => 甲 早于 丙；
#   修订 2（待补）  ：撤回 E2 => 甲 早于 丙 变为未定；
#   修订 3（已封口）：新增 E3 早于(甲,丙) => 关系恢复。
#   旧逻辑给修订 2 记的是“空库 -> 未定”，撤回被漏记；封存在修订 1 的成果包
#   复核时 first_deviation 被错判为修订 3（恢复），实际应是修订 2（撤回）。
#
# 本脚本分两阶段：
#   阶段 A（在线 API，修复前后行为一致）：
#     造出修订 1、定版、封成果包、修订 2（撤回）、修订 3（恢复），记录下
#     修订号、成果包号，全部走公开 HTTP API。
#   阶段 B（模拟“部署前历史修订待回填”，需一个可连库的一次性运维作业）：
#     TRUNCATE 变化台账与封口表（不动 revisions / assertions / 成果包），
#     再按交错封口只封修订 1 与修订 3，留修订 2 待补；重启服务触发启动回填
#     （uvicorn 会在 lifespan 中建表并执行 backfill_revision_changes）。
#
# 阶段 B 需要 psql 与能重启服务的权限，默认跳过；设置 RUN_BACKFILL_SCENARIO=1
# 并提供 PSQL_CMD / RESTART_CMD 后执行。任何时刻都可只跑阶段 A 与复核查询。
#
# 本轮复核操作（第 3 节起）只用只读 API，随时可重复执行：
#   GET  /revision-changes            变化分页（字段/游标约定不变）
#   POST /packages/{id}/reconcile     成果包复核（字段/错误约定不变）
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }
jget() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

# ---------------------------------------------------------------- 阶段 A：在线造数
echo; echo "== 1. 登记地层 RBF-甲 / RBF-乙 / RBF-丙 =="
for s in RBF-甲 RBF-乙 RBF-丙; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"回填核查遗址\"}" >/dev/null || true
done

echo; echo "== 2. 修订 1：E1（甲~乙 同一期）、E2（乙<丙 早于） => 甲 早于 丙，路径 [E1,E2] =="
REV=$(curl -s "$BASE/revision" | jget "['revision']")
if [ "$REV" = "0" ]; then
  curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
    "expected_revision": 0,
    "operations": [
      {"op": "assert", "evidence_id": "RBF-E1", "stratum_a": "RBF-甲", "stratum_b": "RBF-乙", "relation": "same_period"},
      {"op": "assert", "evidence_id": "RBF-E2", "stratum_a": "RBF-乙", "stratum_b": "RBF-丙", "relation": "earlier_than"}
    ]}' | pp

  echo; echo "== 3. 定版 1（修订 1）并封存成果包（甲、丙） =="
  curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' \
    -d '{"label": "回填核查定版"}' >/dev/null
  curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
    -d '{"version": 1, "strata": ["RBF-甲", "RBF-丙"]}' | pp

  echo; echo "== 4. 修订 2：撤回 E2 => 甲 早于 丙 变为未定（新路径 []） =="
  curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
    "expected_revision": 1,
    "operations": [
      {"op": "retract", "evidence_id": "RBF-E2"}
    ]}' | pp

  echo; echo "== 5. 修订 3：新增直达证据 E3（甲<丙）=> 关系恢复为早于，路径 [E3] =="
  curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
    "expected_revision": 2,
    "operations": [
      {"op": "assert", "evidence_id": "RBF-E3", "stratum_a": "RBF-甲", "stratum_b": "RBF-丙", "relation": "earlier_than"}
    ]}' | pp
else
  echo "当前修订号非 0（$REV），假定本示例数据已存在，直接进入复核环节。"
fi

PKG=$(curl -s "$BASE/packages" | python3 -c "
import sys, json
for p in json.load(sys.stdin):
    if p.get('strata') == ['RBF-丙', 'RBF-甲'] and p.get('snapshot_revision') == 1:
        print(p['id']); break
")
CUR=$(curl -s "$BASE/revision" | jget "['revision']")
echo; echo "成果包编号: $PKG；当前修订号: $CUR"

# ------------------------------------------------------------ 本轮复核（只读）
echo; echo "== 6. 复核（1）：变化分页 GET /revision-changes —— 修订 2 的撤回行必须存在 =="
echo "    期望修订 2 有 {甲,丙} 与 {乙,丙} 两行（对按编号固定顺序列，中文序 丙<甲<乙）："
echo "    确定 -> undetermined，new_evidence_path=[]，旧路径分别为 [E1,E2] / [E2]"
python3 - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
JIA, YI, BING = "RBF-甲", "RBF-乙", "RBF-丙"
with urllib.request.urlopen(f"{base}/revision-changes?limit=1000") as resp:
    body = json.load(resp)

def pair_key(c):
    return frozenset((c["stratum_a"], c["stratum_b"]))

rows = [c for c in body["changes"]
        if c["stratum_a"].startswith("RBF-") and c["revision"] in (1, 2, 3)]
for c in rows:
    print(json.dumps({k: c[k] for k in
          ("revision", "stratum_a", "stratum_b", "old_relation", "new_relation",
           "old_evidence_path", "new_evidence_path", "source")}, ensure_ascii=False))
rev2 = {pair_key(c): c for c in rows if c["revision"] == 2}
assert frozenset((JIA, BING)) in rev2, "缺失修订 2 对 {甲,丙} 的撤回记录 —— 回填前态取错"
c = rev2[frozenset((JIA, BING))]
assert (c["old_relation"], c["new_relation"]) in (
    ("earlier_than", "undetermined"), ("later_than", "undetermined")), c
assert c["old_evidence_path"] == ["RBF-E1", "RBF-E2"], c
assert c["new_evidence_path"] == [], c
assert rev2[frozenset((YI, BING))]["old_evidence_path"] == ["RBF-E2"]
print("OK：撤回修订 2 以修订 1 当时生效断言为前态，未被漏记。")
PY

echo; echo "== 7. 复核（2）：POST /packages/{id}/reconcile —— 首次偏离必须是修订 2（撤回） =="
echo "    当前关系虽已恢复（路径更短=仅路径改变），first_deviation.revision 必须为 2。"
RECON=$(curl -s -X POST "$BASE/packages/$PKG/reconcile" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $CUR}")
echo "$RECON" | pp
echo "$RECON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
pair = next(p for p in data['pairs']
            if {p['stratum_a'], p['stratum_b']} == {'RBF-甲', 'RBF-丙'})
fd = pair['first_deviation']
assert fd is not None, '从未偏离记录异常'
assert fd['revision'] == 2, ('首次偏离被错判: ', fd)
assert fd['relation'] == 'undetermined' and fd['evidence_path'] == [], fd
assert fd['previous_relation'] in ('earlier_than', 'later_than'), fd
assert fd['previous_evidence_path'] == ['RBF-E1', 'RBF-E2'], fd
assert fd['source'] == 'formal_batch', fd
print('OK：first_deviation=修订 2（撤回），旧前态/来源正确；当前为', pair['comparison'])
"

echo; echo "== 8. 错误约定保持：过期修订号 409 STALE_REVISION、包不存在 404 PACKAGE_NOT_FOUND =="
curl -s -o /dev/null -w "expected_revision=1 -> HTTP %{http_code}\n" \
  -X POST "$BASE/packages/$PKG/reconcile" -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1}'
curl -s -o /dev/null -w "package 999999      -> HTTP %{http_code}\n" \
  -X POST "$BASE/packages/999999/reconcile" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $CUR}"

# ------------------------------------------------- 阶段 B：交错封口 + 重启回填
if [ "${RUN_BACKFILL_SCENARIO:-0}" = "1" ]; then
  : "${PSQL_CMD:?需设置 PSQL_CMD，例如 PSQL_CMD='docker compose exec -T db psql -U strat -d stratigraphy'}"
  : "${RESTART_CMD:?需设置 RESTART_CMD，例如 RESTART_CMD='docker compose restart app'}"

  echo; echo "== 9. 模拟部署前历史库：清掉变化台账与封口，按交错状态只封修订 1、3 =="
  # shellcheck disable=SC2086
  $PSQL_CMD <<'SQL'
TRUNCATE revision_changes, revision_changes_sealed;
INSERT INTO revision_changes_sealed (revision, source) VALUES (1, 'formal_batch'), (3, 'formal_batch');
-- 修订 2 刻意不封口：服务重启时它是唯一待补修订，前态必须取已封口的修订 1。
SQL

  echo; echo "== 10. 重启服务，触发启动幂等回填 =="
  eval "$RESTART_CMD"
  # 简单等待健康检查通过（启动含建表 + 回填，给足时间）
  healthy=""
  for _ in $(seq 1 60); do
    if curl -sf "$BASE/health" >/dev/null 2>&1; then healthy=1; break; fi
    sleep 1
  done
  [ -n "$healthy" ] || { echo "服务重启后健康检查未通过"; exit 1; }
  echo "服务已就绪（启动回填完成）。"

  echo; echo "== 11. 回填后复核：断言同第 6、7 步（修订 2 撤回行齐、首次偏离=2） =="
  python3 - "$BASE" <<'PY'
import json, sys, urllib.request
base = sys.argv[1]
with urllib.request.urlopen(f"{base}/revision-changes?limit=1000") as resp:
    body = json.load(resp)
rev2 = [c for c in body["changes"]
        if c["revision"] == 2 and c["stratum_a"].startswith("RBF-")]
assert len(rev2) == 2, rev2
for c in rev2:
    assert c["new_relation"] == "undetermined" and c["new_evidence_path"] == [], c
print("OK：重启回填后修订 2 两行撤回记录齐全，未与空库比较。")
PY

  echo; echo "== 12. 幂等：再次重启不重写、不重复（变化行数、封口数保持不变） =="
  eval "$RESTART_CMD"
  # shellcheck disable=SC2086
  $PSQL_CMD -c "SELECT revision, count(*) AS change_rows FROM revision_changes GROUP BY revision ORDER BY revision;
                SELECT count(*) AS sealed_count FROM revision_changes_sealed;"
  echo "-- 期望：只有 revision=2 的 2 行回填变化（封口修订 1、3 不重写），封口共 3 条（1、2、3）。"
else
  echo; echo "== 9. （跳过交错封口/重启回填演练） =="
  echo "    自动化等价验证见：PYTHONPATH=/tmp/pylibs python3 tests/test_revision_changes_backfill.py"
  echo "    该回归用嵌入式 PostgreSQL 覆盖：交错封口撤回、首次偏离复核、零变化封口、"
  echo "    重复启动幂等、回填与在线提交交错及回填持锁期在线提交等待。"
fi

echo; echo "全部复核操作执行完毕。"
