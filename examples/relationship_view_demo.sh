#!/usr/bin/env bash
# 考古地层关系核验服务 —— 两类关系查询“同一完整视图”行为示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
# 说明：本脚本自洽运行（自行登记地层并提交正式批次），不依赖 demo.sh 的数据。
#
# 覆盖：
#   1. 正式关系查询 GET /relationship：一次响应中的正式修订号、关系结论、
#      最优证据路径 evidence_path 与逐步明细 path_detail（含每步 status）同一视图；
#   2. 并发正式批次在查询的读取语句之间提交时，响应只可能整体为提交前或提交后的
#      完整视图（修订号奇偶 ↔ 关系结论严格对应；路径证据不会在明细中显示为已撤回）；
#   3. 推演关系查询 GET /projections/{id}/relationship：推演修订号 / 状态、结论、
#      路径与逐步明细（scope=base/projection）同一冻结视图；已关闭推演仍可查询；
#   4. 错误约定：未知地层 400 INVALID_STRATUM、未知推演 404 PROJECTION_NOT_FOUND。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }
req() { curl -s -o /tmp/resp.json -w '%{http_code}' "$@"; }
show() { echo "   [HTTP $(req "$@")]"; pp < /tmp/resp.json; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 =="
for s in RV1 RV2 RV3 RV4; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"甲遗址\"}" >/dev/null
done

echo; echo "== 2. 正式批次（修订 0 -> 1）：RV1~RV2 同一期，RV2 早于 RV3 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "R-E1", "stratum_a": "RV1", "stratum_b": "RV2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "R-E2", "stratum_a": "RV2", "stratum_b": "RV3", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 3. 正式关系查询 RV1 与 RV3 =="
echo "   修订号=1、结论=早于、最优路径 [R-E1,R-E2]、逐步明细全部 active 来自同一视图："
curl -s "$BASE/relationship?a=RV1&b=RV3" | pp

echo; echo "== 4. 并发批次与并发查询：20 次“撤回↔重新断言”翻转期间多读者持续查询 =="
echo "   写线程严格按 奇数修订=早于 / 偶数修订=未定 推进；"
echo "   读线程逐条校验：响应修订号奇偶与结论严格对应、路径逐步明细全部 active、"
echo "   未定不附路径——任意一次混版（旧修订号配新结论 / 路径证据显示已撤回）都会报错。"
python3 - "$BASE" <<'PY'
import json
import sys
import threading
import time
import urllib.request
import urllib.error

BASE = sys.argv[1]


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def op_assert(eid, a, b, rel):
    return {"op": "assert", "evidence_id": eid, "stratum_a": a, "stratum_b": b, "relation": rel}


def op_retract(eid):
    return {"op": "retract", "evidence_id": eid}


errors, checked, observed = [], 0, set()
stop = threading.Event()


def writer():
    time.sleep(0.05)
    revision, current_eid, seq = 1, "R-E2", 0
    for _ in range(20):
        if revision % 2 == 1:  # 早于 -> 撤回 -> 未定（偶数修订）
            code, resp = call("POST", "/batches", {
                "expected_revision": revision, "operations": [op_retract(current_eid)]})
            assert code == 200, (code, resp)
            revision = resp["revision"]
        else:                  # 未定 -> 新证据 -> 早于（奇数修订）
            seq += 1
            new_eid = f"R-EX{seq:03d}"
            code, resp = call("POST", "/batches", {
                "expected_revision": revision,
                "operations": [op_assert(new_eid, "RV2", "RV3", "earlier_than")]})
            assert code == 200, (code, resp)
            current_eid, revision = new_eid, resp["revision"]
    stop.set()


def reader():
    global checked
    while not stop.is_set():
        _, resp = call("GET", "/relationship?a=RV1&b=RV3")
        rev, rel = resp["revision"], resp["relation"]
        observed.add((rev, rel))
        expected = "earlier_than" if rev % 2 == 1 else "undetermined"
        if rel != expected:
            errors.append(f"混版：revision={rev} 但 relation={rel}（应为 {expected}）")
        path = resp.get("evidence_path")
        detail = resp.get("path_detail") or []
        bad = [s["evidence_id"] for s in detail if s["status"] != "active"]
        if bad:
            errors.append(f"混版：revision={rev} 路径 {path} 中证据在明细显示已撤回: {bad}")
        if rel == "undetermined" and path:
            errors.append(f"未定关系附带路径：revision={rev} path={path}")
        if rel == "earlier_than" and not path:
            errors.append(f"早于关系缺少路径：revision={rev}")
        checked += 1


wt = threading.Thread(target=writer)
wt.start()
readers = [threading.Thread(target=reader) for _ in range(4)]
for t in readers:
    t.start()
wt.join(timeout=60)
for t in readers:
    t.join(timeout=10)
assert not wt.is_alive() and not any(t.is_alive() for t in readers), "线程超时"
if errors:
    print("发现混版响应：")
    for e in errors[:10]:
        print(" -", e)
    sys.exit(1)
print(f"   OK：写线程推进 20 个修订，4 个读线程共校验 {checked} 次响应，零混版。")
print("   实际观察到的 (revision, relation) 组合：")
for rev, rel in sorted(observed):
    print(f"     revision={rev:2d} -> {rel}")
assert all((rel == "earlier_than") == (rev % 2 == 1) for rev, rel in observed)
PY

echo; echo "== 5. 推演关系查询：创建推演（固定当前修订 21），推演内撤回当前 L2<L3 证据 =="
PID=$(curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "关系查询同视图验证推演"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "   projection id = $PID"

# 自动找出当前生效的 RV2 早于 RV3 证据编号（写线程最后一次重新断言引入），
# 不依赖编号的具体填充格式。
ACTIVE_EVIDENCE=$(curl -s "$BASE/assertions?status=active" | python3 -c '
import sys, json
rows = json.load(sys.stdin)
ids = [r["evidence_id"] for r in rows
       if r["stratum_a"] == "RV2" and r["stratum_b"] == "RV3"
       and r["relation"] == "earlier_than"]
assert len(ids) == 1, ids
print(ids[0])')
echo "   当前基线中 RV2 早于 RV3 的生效证据：$ACTIVE_EVIDENCE"
curl -s -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": 0,
       \"operations\": [{\"op\": \"retract\", \"evidence_id\": \"$ACTIVE_EVIDENCE\"}]}" | pp

echo; echo "== 6. 推演视图查询 RV1 与 RV3（推演修订 1：未定）；推演状态/修订号/结论同一冻结视图 =="
curl -s "$BASE/projections/$PID/relationship?a=RV1&b=RV3" | pp

echo; echo "== 7. 推演内重新断言 RV2 早于 RV3（推演修订 1 -> 2），再查 =="
curl -s -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [
    {"op": "assert", "evidence_id": "R-PX1", "stratum_a": "RV2", "stratum_b": "RV3", "relation": "earlier_than"}
  ]}' >/dev/null
echo "   路径首步来自基线 scope=base，尾步为推演新增 scope=projection，两步 status 均为 active："
curl -s "$BASE/projections/$PID/relationship?a=RV1&b=RV3" | pp

echo; echo "== 8. 放弃推演后冻结视图仍可查询（status=discarded） =="
curl -s -X DELETE "$BASE/projections/$PID" | pp
curl -s "$BASE/projections/$PID/relationship?a=RV1&b=RV3" | pp

echo; echo "== 9. 错误约定保持：正式查询未知地层（400 INVALID_STRATUM） =="
show "$BASE/relationship?a=RV1&b=NOPE"

echo; echo "== 10. 错误约定保持：推演查询未知地层 / 未知推演（400 / 404） =="
show "$BASE/projections/$PID/relationship?a=RV1&b=NOPE"
show "$BASE/projections/999999/relationship?a=RV1&b=RV3"

echo; echo "全部示例执行完毕：两类关系查询在并发提交下均未出现混版响应。"
