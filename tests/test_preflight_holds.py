"""提交前预检与临时保全联动的回归测试（无需真实数据库）。

修复口径：预检在同一快照视图内读取生效保全——
- 直接撤回受保全证据：按正式批次的 409 EVIDENCE_HELD 拒绝（附 blocked_evidence）；
- 搜索额外撤回时排除受保全证据，其余生效旧证据仍按数量最少、编号序列字典序最小选解；
- 预算内无解仍返回 budget_insufficient 与冲突路径；解除保全后恢复原有建议。

用内存假连接替代 psycopg2 连接（按 SQL 文本分发到内存状态），
从而在无 PostgreSQL 的环境下直接驱动 app.preflight.preflight_batch。

运行：python3 tests/test_preflight_holds.py（或 pytest 收集 test_* 函数）
"""
from __future__ import annotations

import os
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timezone

# app.db 顶部 import psycopg2 / psycopg2.pool；本测试不需要真实驱动
_psycopg2 = types.ModuleType("psycopg2")
_psycopg2_pool = types.ModuleType("psycopg2.pool")
_psycopg2.pool = _psycopg2_pool
sys.modules.setdefault("psycopg2", _psycopg2)
sys.modules.setdefault("psycopg2.pool", _psycopg2_pool)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import preflight as preflight_mod  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.models import PreflightIn  # noqa: E402


# ------------------------------------------------------------------ 内存假连接
class FakeCursor:
    """按 SQL 文本把查询分发到内存状态（只覆盖预检路径用到的语句）。"""

    def __init__(self, state):
        self.state = state
        self._rows = []

    # -- 分发
    def execute(self, sql, params=None):
        norm = " ".join(sql.split()).upper()
        if norm.startswith("SET TRANSACTION"):
            self._rows = []
        elif "FROM REVISION_COUNTER" in norm:
            self._rows = [(self.state["revision"],)]
        elif "FROM STRATA" in norm:
            self._rows = [(s,) for s in sorted(self.state["strata"])]
        elif "FROM ASSERTIONS" in norm:
            self._rows = [
                (eid, a, b, relation, status, added_rev)
                for eid, (a, b, relation, status, added_rev) in self.state["assertions"].items()
            ]
        elif "FROM EVIDENCE_HOLD_ITEMS" in norm and "DISTINCT" in norm:
            wanted = set(params[0])
            self._rows = [(eid,) for eid in sorted(wanted) if self._active_holds_of(eid)]
        elif "FROM EVIDENCE_HOLD_ITEMS" in norm:
            wanted = set(params[0])
            rows = []
            for hold_id in sorted(self.state["holds"]):
                hold = self.state["holds"][hold_id]
                if hold["status"] != "active":
                    continue
                for eid in sorted(wanted & set(hold["evidence_ids"])):
                    rows.append((eid, hold_id, hold["revision"], hold["reason"], hold["created_at"]))
            rows.sort(key=lambda r: (r[0], r[1]))
            self._rows = rows
        else:  # 预检只读路径不应出现其他语句（尤其不允许任何写语句）
            raise AssertionError(f"unexpected SQL in read-only preflight: {sql}")

    def _active_holds_of(self, evidence_id):
        return [
            h for h in self.state["holds"].values()
            if h["status"] == "active" and evidence_id in h["evidence_ids"]
        ]

    # -- 结果
    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _CursorCM:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self._cursor

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, state):
        self._state = state

    def cursor(self):
        return _CursorCM(FakeCursor(self._state))


@contextmanager
def _fake_db_conn(state):
    yield FakeConn(state)


# ------------------------------------------------------------------ 测试工具
def make_state(assertions, holds=None, revision=1):
    """assertions: {eid: (a, b, relation, status, added_revision)}；holds: {id: [eid...]}。"""
    strata = set()
    for a, b, _rel, _status, _rev in assertions.values():
        strata.add(a)
        strata.add(b)
    hold_map = {}
    for hold_id, evidence_ids in (holds or {}).items():
        hold_map[hold_id] = {
            "status": "active",
            "revision": revision,
            "reason": f"交接复核保全 {hold_id}",
            "created_at": datetime(2026, 10, 4, tzinfo=timezone.utc),
            "evidence_ids": list(evidence_ids),
        }
    return {
        "revision": revision,
        "strata": strata,
        "assertions": dict(assertions),
        "holds": hold_map,
    }


def run_preflight(state, operations, budget=1, expected_revision=1):
    """在假连接上执行一次预检（每次调用替换 app.preflight.db_conn）。"""
    original = preflight_mod.db_conn
    preflight_mod.db_conn = lambda: _fake_db_conn(state)
    try:
        body = PreflightIn(
            expected_revision=expected_revision,
            extra_retraction_budget=budget,
            operations=operations,
        )
        return preflight_mod.preflight_batch(body)
    finally:
        preflight_mod.db_conn = original


def op_assert(eid, a, b, relation="earlier_than"):
    return {"op": "assert", "evidence_id": eid, "stratum_a": a, "stratum_b": b, "relation": relation}


def op_retract(eid):
    return {"op": "retract", "evidence_id": eid}


def expect_api_error(state, operations, budget=1, expected_revision=1):
    try:
        run_preflight(state, operations, budget, expected_revision)
    except ApiError as exc:
        return exc
    raise AssertionError("expected ApiError, preflight returned normally")


# ------------------------------------------------------------------ 用例
def test_direct_retract_of_held_evidence_raises_evidence_held():
    """直接撤回受保全证据：409 EVIDENCE_HELD，details 与正式批次同构。"""
    state = make_state(
        {"E1": ("A", "B", "earlier_than", "active", 1)},
        holds={7: ["E1"]},
    )
    exc = expect_api_error(state, [op_retract("E1")])
    assert exc.status_code == 409, exc.status_code
    assert exc.code == "EVIDENCE_HELD", exc.code
    details = exc.details
    assert details["blocked_evidence_ids"] == ["E1"], details
    blocked = details["blocked_evidence"]
    assert [b["evidence_id"] for b in blocked] == ["E1"]
    assert [h["id"] for h in blocked[0]["holds"]] == [7]
    assert details["current_revision"] == 1
    assert details["expected_revision"] == 1


def test_direct_retract_of_held_evidence_lists_all_active_holds():
    """多份保全覆盖同一证据时，blocked_evidence 列出全部生效保全。"""
    state = make_state(
        {"E1": ("A", "B", "earlier_than", "active", 1)},
        holds={3: ["E1"], 5: ["E1"]},
    )
    exc = expect_api_error(state, [op_retract("E1")])
    assert exc.code == "EVIDENCE_HELD"
    holds = exc.details["blocked_evidence"][0]["holds"]
    assert [h["id"] for h in holds] == [3, 5], holds


def test_held_evidence_excluded_from_extra_retractions():
    """任务场景：E1（A 早于 B）被保全，新增 E2（B 早于 A）——预检不得建议撤回 E1。"""
    state = make_state(
        {"E1": ("A", "B", "earlier_than", "active", 1)},
        holds={1: ["E1"]},
    )
    resp = run_preflight(state, [op_assert("E2", "B", "A")], budget=1)
    # 修复前：extra_retractions_found + ["E1"]；修复后：唯一可解证据被保全 -> 预算内无解
    assert resp["resolution"] == "budget_insufficient", resp
    assert resp["passed"] is False
    assert resp["extra_retractions"] == []
    assert resp["extra_retraction_count"] is None
    paths = [c["evidence_path"] for c in resp["conflicts"]]
    assert any(set(p) == {"E1", "E2"} for p in paths), paths


def test_same_scenario_without_hold_still_suggests_minimal():
    """对照组：无保全时同一批次仍建议撤回字典序最小的 E1（既有行为不变）。"""
    state = make_state({"E1": ("A", "B", "earlier_than", "active", 1)})
    resp = run_preflight(state, [op_assert("E2", "B", "A")], budget=1)
    assert resp["resolution"] == "extra_retractions_found", resp
    assert resp["extra_retractions"] == ["E1"]
    assert resp["extra_retraction_count"] == 1
    assert resp["passed"] is True


def test_next_best_solution_among_remaining_evidence():
    """环上最小编号旧证据被保全：改选其余生效旧证据中字典序最小者。"""
    state = make_state(
        {
            "EV1": ("S1", "S2", "same_period", "active", 1),
            "EV2": ("S2", "S3", "earlier_than", "active", 1),
            "EV3": ("S3", "S4", "earlier_than", "active", 1),
        },
        holds={2: ["EV1"]},
    )
    resp = run_preflight(state, [op_assert("EV9", "S4", "S1")], budget=1)
    assert resp["resolution"] == "extra_retractions_found", resp
    assert resp["extra_retractions"] == ["EV2"], resp  # 不是受保全的 EV1
    assert resp["passed"] is True


def test_all_cycle_evidence_held_gives_budget_insufficient():
    """环上全部旧证据都被保全：预算内无解，附当前冲突路径。"""
    state = make_state(
        {
            "EV1": ("S1", "S2", "same_period", "active", 1),
            "EV2": ("S2", "S3", "earlier_than", "active", 1),
            "EV3": ("S3", "S4", "earlier_than", "active", 1),
        },
        holds={1: ["EV1", "EV2"], 2: ["EV3"]},
    )
    resp = run_preflight(state, [op_assert("EV9", "S4", "S1")], budget=3)
    assert resp["resolution"] == "budget_insufficient", resp
    assert resp["extra_retraction_count"] is None
    assert resp["conflicts"], resp


def test_released_hold_restores_original_suggestion():
    """保全解除后（视图中无生效保全），同一预检恢复建议最小编号旧证据。"""
    state = make_state(
        {
            "EV1": ("S1", "S2", "same_period", "active", 1),
            "EV2": ("S2", "S3", "earlier_than", "active", 1),
            "EV3": ("S3", "S4", "earlier_than", "active", 1),
        },
        holds={2: ["EV1"]},
    )
    state["holds"][2]["status"] = "released"  # 解除后不参与拦截与排除
    resp = run_preflight(state, [op_assert("EV9", "S4", "S1")], budget=1)
    assert resp["resolution"] == "extra_retractions_found", resp
    assert resp["extra_retractions"] == ["EV1"]


def test_hold_on_unrelated_evidence_does_not_change_suggestion():
    """保全与冲突无关的证据不影响既有选取（仍取字典序最小）。"""
    state = make_state(
        {
            "EV1": ("S1", "S2", "same_period", "active", 1),
            "EV2": ("S2", "S3", "earlier_than", "active", 1),
            "EV3": ("S3", "S4", "earlier_than", "active", 1),
            "EV7": ("X1", "X2", "earlier_than", "active", 1),
        },
        holds={4: ["EV7"]},
    )
    resp = run_preflight(state, [op_assert("EV9", "S4", "S1")], budget=1)
    assert resp["resolution"] == "extra_retractions_found", resp
    assert resp["extra_retractions"] == ["EV1"]


def test_already_consistent_unaffected_by_holds():
    """原批次本可通过时仍 already_consistent（保全只影响撤回路径）。"""
    state = make_state(
        {"E1": ("A", "B", "earlier_than", "active", 1)},
        holds={1: ["E1"]},
    )
    state["strata"].add("C")
    resp = run_preflight(state, [op_assert("E2", "B", "C", "earlier_than")], budget=1)
    assert resp["resolution"] == "already_consistent", resp
    assert resp["passed"] is True
    assert resp["extra_retractions"] == []


def test_retract_of_unheld_evidence_still_works():
    """撤回未受保全的旧证据：不触发 EVIDENCE_HELD，按原批次语义处理。"""
    state = make_state(
        {
            "E1": ("A", "B", "earlier_than", "active", 1),
            "E2": ("B", "C", "earlier_than", "active", 1),
        },
        holds={1: ["E1"]},
    )
    resp = run_preflight(state, [op_retract("E2")], budget=0)
    assert resp["resolution"] == "already_consistent", resp
    assert resp["batch_retracted_evidence"] == ["E2"]


def test_structural_errors_still_raised_before_search():
    """既有结构拒绝语义不变：撤回不存在证据仍 ASSERTION_NOT_FOUND。"""
    state = make_state(
        {"E1": ("A", "B", "earlier_than", "active", 1)},
        holds={1: ["E1"]},
    )
    exc = expect_api_error(state, [op_retract("E404")])
    assert exc.status_code == 409
    assert exc.code == "ASSERTION_NOT_FOUND", exc.code


def test_held_check_precedes_structural_checks_like_formal_batch():
    """与正式批次同一顺序：撤回目标同时受保全且另有结构错误时，先报 EVIDENCE_HELD。"""
    state = make_state(
        {"E1": ("A", "B", "earlier_than", "active", 1)},
        holds={1: ["E1"]},
    )
    exc = expect_api_error(state, [op_retract("E1"), op_retract("E404")])
    assert exc.code == "EVIDENCE_HELD", exc.code


def test_stale_revision_still_first():
    """修订号过期仍是第一个拒绝（STALE_REVISION 优先于 EVIDENCE_HELD）。"""
    state = make_state(
        {"E1": ("A", "B", "earlier_than", "active", 1)},
        holds={1: ["E1"]},
        revision=5,
    )
    exc = expect_api_error(state, [op_retract("E1")], expected_revision=1)
    assert exc.code == "STALE_REVISION", exc.code
    assert exc.details["current_revision"] == 5


ALL_TESTS = [
    test_direct_retract_of_held_evidence_raises_evidence_held,
    test_direct_retract_of_held_evidence_lists_all_active_holds,
    test_held_evidence_excluded_from_extra_retractions,
    test_same_scenario_without_hold_still_suggests_minimal,
    test_next_best_solution_among_remaining_evidence,
    test_all_cycle_evidence_held_gives_budget_insufficient,
    test_released_hold_restores_original_suggestion,
    test_hold_on_unrelated_evidence_does_not_change_suggestion,
    test_already_consistent_unaffected_by_holds,
    test_retract_of_unheld_evidence_still_works,
    test_structural_errors_still_raised_before_search,
    test_held_check_precedes_structural_checks_like_formal_batch,
    test_stale_revision_still_first,
]


def main():
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
    print(f"ALL {len(ALL_TESTS)} PREFLIGHT-HOLD TESTS PASSED")


if __name__ == "__main__":
    main()
