"""两类关系查询“同一次响应同一完整视图”的回归测试（无需真实数据库）。

修复口径（正式 GET /relationship 与推演 GET /projections/{id}/relationship）：
- 一次响应中的修订号（推演另含推演状态 / 推演修订号）、关系结论、最优证据路径与
  逐步证据明细必须取自**同一 REPEATABLE READ READ ONLY 只读快照视图**；
- 并发正式批次 / 推演批次在查询的多条读取语句之间提交时，响应只可能整体是提交前
  或整体是提交后的结果，不得出现：
    1) 旧修订号配新结论（修订号先读、结论后算）；
    2) 结论仍引用某证据路径，但逐步明细把路径中的证据显示为已撤回。

用带“快照语义”的内存假连接驱动 service.query_relationship /
projections.query_projection_relationship：
- 执行 `SET TRANSACTION ... REPEATABLE READ` 后深拷贝状态（模拟事务快照），之后的
  全部 SELECT 读拷贝；
- 不设置快照（honor_snapshot=False）时模拟修复前的 READ COMMITTED 行为：每条
  SELECT 读最新已提交状态，用于证明本测试对混版有辨别力（修复前代码在该模式下
  必然产出混版响应）；
- 可在指定 SQL 语句执行前注入“并发批次提交”钩子。

运行：python3 tests/test_relationship_view_consistency.py
"""
from __future__ import annotations

import copy
import os
import sys
import types
from contextlib import contextmanager

# app.db 顶部 import psycopg2 / psycopg2.pool；本测试不需要真实驱动
_psycopg2 = types.ModuleType("psycopg2")
_psycopg2_pool = types.ModuleType("psycopg2.pool")
_psycopg2_extras = types.ModuleType("psycopg2.extras")
_psycopg2_extras.Json = lambda obj: obj
_psycopg2.pool = _psycopg2_pool
_psycopg2.extras = _psycopg2_extras
sys.modules.setdefault("psycopg2", _psycopg2)
sys.modules.setdefault("psycopg2.pool", _psycopg2_pool)
sys.modules.setdefault("psycopg2.extras", _psycopg2_extras)

# 环境内无 pydantic 时用最小桩替代（本测试不实例化请求模型，只需 app 模块可导入）
try:  # pragma: no cover - 有真实 pydantic 的环境直接使用
    import pydantic  # noqa: F401
except ImportError:
    _pydantic = types.ModuleType("pydantic")

    class BaseModel:  # noqa: D401
        pass

    def Field(default=None, **kwargs):  # noqa: D401
        return default

    _pydantic.BaseModel = BaseModel
    _pydantic.Field = Field
    sys.modules.setdefault("pydantic", _pydantic)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import projections as projections_mod  # noqa: E402
from app import service as service_mod  # noqa: E402
from app.errors import ApiError  # noqa: E402


# ------------------------------------------------------------------ 内存假连接
class FakeCursor:
    """按 SQL 文本把 SELECT 分发到内存状态；支持事务快照与语句前提交钩子。"""

    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    @property
    def state(self):
        return self.conn.snapshot if self.conn.snapshot is not None else self.conn.live

    def execute(self, sql, params=None):
        norm = " ".join(sql.split()).upper()

        if norm.startswith("SET TRANSACTION"):
            assert "REPEATABLE READ" in norm, f"关系查询须使用 REPEATABLE READ: {sql}"
            self.conn.set_transaction_seen.append(sql)
            if self.conn.honor_snapshot:
                self.conn.snapshot = copy.deepcopy(self.conn.live)
            self._rows = []
            return

        for predicate, hook in self.conn.before_hooks:
            if predicate(norm) and not getattr(hook, "_fired", False):
                hook._fired = True
                hook(self.conn.live)

        # ---- 正式关系查询路径 ----
        if norm == "SELECT 1 FROM STRATA WHERE STRATUM_ID = %S":
            self._rows = [(1,)] if params[0] in self.state["strata"] else []
        elif norm == "SELECT REVISION FROM REVISION_COUNTER WHERE ID = TRUE":
            self._rows = [(self.state["revision"],)]
        elif "FROM ASSERTIONS WHERE STATUS = 'ACTIVE'" in norm:
            self._rows = [
                (eid, a, b, rel)
                for eid, (a, b, rel, status) in self.state["assertions"].items()
                if status == "active"
            ]
            self._rows.sort(key=lambda r: r[0])
        elif "FROM ASSERTIONS WHERE EVIDENCE_ID = ANY(%S)" in norm:
            self._rows = [
                (eid, *self.state["assertions"][eid])
                for eid in params[0]
                if eid in self.state["assertions"]
            ]

        # ---- 推演关系查询路径 ----
        elif norm.startswith("SELECT ID, LABEL, BASE_REVISION, BASE_STRATA, BASE_ASSERTIONS,"):
            p = self.state["projections"].get(params[0])
            if p is None:
                self._rows = []
            else:
                self._rows = [
                    (
                        p["id"],
                        p["label"],
                        p["base_revision"],
                        list(p["base_strata"]),
                        list(p["base_assertions"]),
                        p["projection_revision"],
                        p["status"],
                        p["merged_into_revision"],
                    )
                ]
        elif "FROM PROJECTION_ASSERTIONS WHERE PROJECTION_ID = %S" in norm and "ANY(%S)" in norm:
            pa = self.state["projection_assertions"].get(params[0], {})
            self._rows = [
                (eid, *pa[eid]) for eid in params[1] if eid in pa
            ]
        elif "FROM PROJECTION_ASSERTIONS WHERE PROJECTION_ID = %S" in norm:
            pa = self.state["projection_assertions"].get(params[0], {})
            self._rows = [(eid, *row) for eid, row in sorted(pa.items())]
        elif "FROM PROJECTION_RETRACTIONS WHERE PROJECTION_ID = %S" in norm:
            pr = self.state["projection_retractions"].get(params[0], {})
            self._rows = sorted((eid, rev) for eid, rev in pr.items())
        else:
            raise AssertionError(f"unexpected SQL in read-only relationship query: {sql}")

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
    """一次查询用一个连接；honor_snapshot=False 模拟修复前 READ COMMITTED。"""

    def __init__(self, live, honor_snapshot=True, before_hooks=()):
        self.live = live
        self.honor_snapshot = honor_snapshot
        self.before_hooks = list(before_hooks)
        self.snapshot = None
        self.set_transaction_seen = []

    def cursor(self):
        return _CursorCM(FakeCursor(self))


@contextmanager
def _fake_conn(live, honor_snapshot=True, before_hooks=()):
    conn = FakeConn(live, honor_snapshot=honor_snapshot, before_hooks=before_hooks)
    yield conn


def run_formal(live, a, b, honor_snapshot=True, hooks=()):
    original = service_mod.db_conn
    service_mod.db_conn = lambda: _fake_conn(live, honor_snapshot, hooks)
    try:
        return service_mod.query_relationship(a, b)
    finally:
        service_mod.db_conn = original


def run_projection(live, pid, a, b, honor_snapshot=True, hooks=()):
    original = projections_mod.db_conn
    projections_mod.db_conn = lambda: _fake_conn(live, honor_snapshot, hooks)
    try:
        return projections_mod.query_projection_relationship(pid, a, b)
    finally:
        projections_mod.db_conn = original


def once(predicate, fn):
    """只触发一次的语句前钩子。"""

    def hook(state):
        fn(state)

    hook._fired = False

    def wrapped_predicate(norm):
        return not hook._fired and predicate(norm)

    return wrapped_predicate, hook


def nth(base_predicate, occurrence):
    """在第 occurrence 次匹配 base_predicate 的语句前返回 True（用于区分同形语句）。"""
    count = {"n": 0}

    def predicate(norm):
        if base_predicate(norm):
            count["n"] += 1
            return count["n"] == occurrence
        return False

    return predicate


def _is_proj_assertions_full_load(norm):
    return "FROM PROJECTION_ASSERTIONS WHERE PROJECTION_ID = %S" in norm and "ANY(%S)" not in norm


def _is_proj_assertions_detail_load(norm):
    return "FROM PROJECTION_ASSERTIONS WHERE PROJECTION_ID = %S" in norm and "ANY(%S)" in norm


def _is_proj_retractions(norm):
    return "FROM PROJECTION_RETRACTIONS WHERE PROJECTION_ID = %S" in norm


# ------------------------------------------------------------------ 场景构造
def formal_state():
    """修订 1：E1(L1~L2) + E2(L2<L3)，故 L1 早于 L3，最优路径 [E1, E2]。"""
    return {
        "revision": 1,
        "strata": {"L1", "L2", "L3", "L4"},
        "assertions": {
            "E1": ("L1", "L2", "same_period", "active"),
            "E2": ("L2", "L3", "earlier_than", "active"),
        },
        "projections": {},
        "projection_assertions": {},
        "projection_retractions": {},
    }


def commit_revision_2_retract_e2(state):
    """并发正式批次提交修订 2：撤回 E2（L2 早于 L3），L1 与 L3 变为未定。"""
    assert state["assertions"]["E2"][3] == "active"
    a, b, rel, _ = state["assertions"]["E2"]
    state["assertions"]["E2"] = (a, b, rel, "retracted")
    state["revision"] = 2


EXPECTED_FORMAL_R1 = {
    "relation": "earlier_than",
    "evidence_path": ["E1", "E2"],
    "revision": 1,
}
EXPECTED_FORMAL_R2 = {
    "relation": "undetermined",
    "evidence_path": None,
    "revision": 2,
}


def projection_state():
    """推演 1：基线修订 5，基线断言 E1(L1~L2)；推演修订 1 新增 E2(L2<L3)。"""
    state = formal_state()
    state["revision"] = 5
    state["projections"] = {
        1: {
            "id": 1,
            "label": "p1",
            "base_revision": 5,
            "base_strata": ["L1", "L2", "L3", "L4"],
            "base_assertions": [
                {"evidence_id": "E1", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"}
            ],
            "projection_revision": 1,
            "status": "open",
            "merged_into_revision": None,
        },
        2: {
            "id": 2,
            "label": "p2-closed",
            "base_revision": 5,
            "base_strata": ["L1", "L2", "L3", "L4"],
            "base_assertions": [
                {"evidence_id": "E1", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"}
            ],
            "projection_revision": 1,
            "status": "merged",
            "merged_into_revision": 6,
        },
        3: {
            "id": 3,
            "label": "p3-discarded",
            "base_revision": 5,
            "base_strata": ["L1", "L2", "L3", "L4"],
            "base_assertions": [
                {"evidence_id": "E1", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"}
            ],
            "projection_revision": 0,
            "status": "discarded",
            "merged_into_revision": None,
        },
    }
    for pid in (1, 2, 3):
        state["projection_assertions"][pid] = {}
        state["projection_retractions"][pid] = {}
    state["projection_assertions"][1]["E2"] = (
        "L2", "L3", "earlier_than", "active", 1, None
    )
    state["projection_assertions"][2]["E2"] = (
        "L2", "L3", "earlier_than", "active", 1, None
    )
    return state


def commit_proj_revision_2_retract_e2(state):
    """并发推演批次：撤回推演新增的 E2，推演修订 1 -> 2，L1 与 L3 在推演内变未定。"""
    row = state["projection_assertions"][1]["E2"]
    assert row[3] == "active"
    # row: (a, b, relation, status, added_revision, retracted_revision)
    state["projection_assertions"][1]["E2"] = (row[0], row[1], row[2], "retracted", row[4], 2)
    state["projection_retractions"][1]["E2"] = 2
    state["projections"][1]["projection_revision"] = 2


def path_detail_by_id(resp):
    return {step["evidence_id"]: step for step in resp["path_detail"]}


# ------------------------------------------------------------------ 正式查询
def test_formal_uses_repeatable_read_snapshot_transaction():
    """正式关系查询以 REPEATABLE READ 只读快照事务开场。"""
    state = formal_state()
    resp = _capture_formal(state, "L1", "L3")
    assert resp["revision"] == 1
    assert any("REPEATABLE READ" in s for s in _last_conn.set_transaction_seen)
    assert any("READ ONLY" in s for s in _last_conn.set_transaction_seen)


_last_conn = None


def _capture_formal(live, a, b, honor_snapshot=True, hooks=()):
    global _last_conn
    original = service_mod.db_conn

    @contextmanager
    def factory():
        global _last_conn
        conn = FakeConn(live, honor_snapshot=honor_snapshot, before_hooks=hooks)
        _last_conn = conn
        yield conn

    service_mod.db_conn = factory
    try:
        return service_mod.query_relationship(a, b)
    finally:
        service_mod.db_conn = original


def test_formal_concurrent_commit_before_active_load_returns_coherent_old_view():
    """旧修订号 + 新结论混版：提交发生在修订号读取后、生效断言读取前。

    快照下整体返回提交前（修订 1、早于）；修复前 READ COMMITTED 会把修订 1
    与提交后的“未定”结论混在一次响应里。
    """
    pred, hook = once(
        lambda n: "FROM ASSERTIONS WHERE STATUS = 'ACTIVE'" in n,
        commit_revision_2_retract_e2,
    )
    state = formal_state()
    resp = _capture_formal(state, "L1", "L3", hooks=[(pred, hook)])
    assert hook._fired, "并发提交钩子应已触发"
    assert resp["revision"] == EXPECTED_FORMAL_R1["revision"]
    assert resp["relation"] == EXPECTED_FORMAL_R1["relation"]
    assert resp["evidence_path"] == ["E1", "E2"]
    assert all(step["status"] == "active" for step in resp["path_detail"])
    # 在线状态确实已前进到修订 2
    assert state["revision"] == 2


def test_formal_concurrent_commit_between_derivation_and_path_detail():
    """路径证据显示为已撤回的混版：提交发生在推导之后、逐步明细读取之前。

    快照下逐步明细仍按同一视图把 E2 显示为 active；修复前 READ COMMITTED 下
    结论仍是早于、路径仍含 E2，但明细把 E2 显示为 retracted。
    """
    pred, hook = once(
        lambda n: "FROM ASSERTIONS WHERE EVIDENCE_ID = ANY(%S)" in n,
        commit_revision_2_retract_e2,
    )
    state = formal_state()
    resp = _capture_formal(state, "L1", "L3", hooks=[(pred, hook)])
    assert hook._fired
    assert resp["revision"] == 1
    assert resp["relation"] == "earlier_than"
    assert resp["evidence_path"] == ["E1", "E2"]
    detail = path_detail_by_id(resp)
    assert detail["E1"]["status"] == "active"
    assert detail["E2"]["status"] == "active", "快照视图内路径证据不得显示为已撤回"


def test_formal_read_committed_control_demonstrates_mix_is_observable_without_fix():
    """对照：模拟修复前（无快照）。同一注入时序必须能观察到混版，证明测试有辨别力。"""
    # 场景 1：旧修订号 + 新结论
    pred, hook = once(
        lambda n: "FROM ASSERTIONS WHERE STATUS = 'ACTIVE'" in n,
        commit_revision_2_retract_e2,
    )
    state = formal_state()
    resp = run_formal(state, "L1", "L3", honor_snapshot=False, hooks=[(pred, hook)])
    assert hook._fired
    assert resp["revision"] == 1 and resp["relation"] == "undetermined", resp

    # 场景 2：结论 / 路径为旧视图，逐步明细把路径证据显示为已撤回
    pred, hook = once(
        lambda n: "FROM ASSERTIONS WHERE EVIDENCE_ID = ANY(%S)" in n,
        commit_revision_2_retract_e2,
    )
    state = formal_state()
    resp = run_formal(state, "L1", "L3", honor_snapshot=False, hooks=[(pred, hook)])
    assert hook._fired
    assert resp["revision"] == 1
    assert resp["relation"] == "earlier_than"
    assert resp["evidence_path"] == ["E1", "E2"]
    assert path_detail_by_id(resp)["E2"]["status"] == "retracted", resp


def test_formal_unknown_stratum_error_convention_unchanged():
    """未知地层仍 400 INVALID_STRATUM（错误约定不变）。"""
    state = formal_state()
    try:
        run_formal(state, "L1", "LX")
    except ApiError as exc:
        assert exc.status_code == 400
        assert exc.code == "INVALID_STRATUM"
        assert exc.details == {"stratum_id": "LX"}
    else:
        raise AssertionError("expected INVALID_STRATUM")


# ------------------------------------------------------------------ 推演查询
def test_projection_baseline_response_is_coherent():
    state = projection_state()
    resp = run_projection(state, 1, "L1", "L3")
    assert resp["status"] == "open"
    assert resp["base_revision"] == 5
    assert resp["projection_revision"] == 1
    assert resp["relation"] == "earlier_than"
    assert resp["evidence_path"] == ["E1", "E2"]
    detail = path_detail_by_id(resp)
    assert detail["E1"]["scope"] == "base" and detail["E1"]["status"] == "active"
    assert detail["E2"]["scope"] == "projection" and detail["E2"]["status"] == "active"


def test_projection_commit_before_view_load_returns_coherent_old_view():
    """旧推演修订号 + 新结论混版：推演批次在推演行（修订号/状态）读取后提交。

    注入点为推演行之后、推演增撤记录之前。快照下整体为提交前视图（修订 1、早于）；
    修复前 READ COMMITTED 会用修订 1 搭配提交后推导的“未定”结论。
    """
    pred, hook = once(
        nth(_is_proj_assertions_full_load, 1),
        commit_proj_revision_2_retract_e2,
    )
    state = projection_state()
    resp = run_projection(state, 1, "L1", "L3", hooks=[(pred, hook)])
    assert hook._fired
    assert resp["projection_revision"] == 1
    assert resp["status"] == "open"
    assert resp["relation"] == "earlier_than"
    assert resp["evidence_path"] == ["E1", "E2"]
    detail = path_detail_by_id(resp)
    assert detail["E2"]["status"] == "active"
    assert state["projections"][1]["projection_revision"] == 2


def test_projection_commit_between_view_load_and_path_detail():
    """推演路径证据显示为已撤回的混版：提交发生在视图推导后、逐步明细读取前。"""
    pred, hook = once(
        nth(_is_proj_assertions_detail_load, 1),
        commit_proj_revision_2_retract_e2,
    )
    state = projection_state()
    resp = run_projection(state, 1, "L1", "L3", hooks=[(pred, hook)])
    assert hook._fired
    assert resp["projection_revision"] == 1
    assert resp["relation"] == "earlier_than"
    assert resp["evidence_path"] == ["E1", "E2"]
    detail = path_detail_by_id(resp)
    assert detail["E2"]["status"] == "active", "快照视图内路径证据不得显示为已撤回"
    assert detail["E2"]["scope"] == "projection"


def test_projection_read_committed_control_demonstrates_mix_without_fix():
    """对照：无快照（修复前）下同一注入时序必然混版。"""
    pred, hook = once(
        nth(_is_proj_assertions_full_load, 1),
        commit_proj_revision_2_retract_e2,
    )
    state = projection_state()
    resp = run_projection(state, 1, "L1", "L3", honor_snapshot=False, hooks=[(pred, hook)])
    assert hook._fired
    assert resp["projection_revision"] == 1 and resp["relation"] == "undetermined", resp

    pred, hook = once(
        nth(_is_proj_assertions_detail_load, 1),
        commit_proj_revision_2_retract_e2,
    )
    state = projection_state()
    resp = run_projection(state, 1, "L1", "L3", honor_snapshot=False, hooks=[(pred, hook)])
    assert hook._fired
    assert resp["projection_revision"] == 1
    assert resp["relation"] == "earlier_than"
    assert path_detail_by_id(resp)["E2"]["status"] == "retracted"


def test_projection_base_evidence_detail_consistent_under_formal_commit():
    """路径只含基线证据时，正式侧并发提交也不得让明细读取混入新版本。

    构造：推演修订 0（无推演内增撤），L1 与 L2 仅由基线 E1 连为同一期；
    并发正式批次（修订 5 -> 6，仅在正式 assertions 表撤回 E2——E2 不在路径上，
    这里直接验证正式 assertions 的逐步明细读取走同一快照：正式表状态变化不影响
    基线冻结视图，E1 在推演视角恒为 active/scope=base）。
    """
    state = projection_state()

    def formal_commit_retract_e2(s):
        a, b, rel, _ = s["assertions"]["E2"]
        s["assertions"]["E2"] = (a, b, rel, "retracted")
        s["revision"] = 6

    pred, hook = once(
        lambda n: "FROM ASSERTIONS WHERE EVIDENCE_ID = ANY(%S)" in n,
        formal_commit_retract_e2,
    )
    resp = run_projection(state, 1, "L1", "L2", hooks=[(pred, hook)])
    assert hook._fired
    assert resp["relation"] == "same_period"
    assert resp["evidence_path"] == ["E1"]
    detail = path_detail_by_id(resp)
    assert detail["E1"]["status"] == "active"
    assert detail["E1"]["scope"] == "base"


def test_closed_projections_still_query_frozen_view():
    """已并入 / 已放弃推演仍可查询冻结视图（状态字段如实回带）。"""
    state = projection_state()
    merged = run_projection(state, 2, "L1", "L3")
    assert merged["status"] == "merged"
    assert merged["projection_revision"] == 1
    assert merged["relation"] == "earlier_than"
    assert merged["evidence_path"] == ["E1", "E2"]

    discarded = run_projection(state, 3, "L1", "L2")
    assert discarded["status"] == "discarded"
    assert discarded["relation"] == "same_period"
    assert discarded["evidence_path"] == ["E1"]


def test_projection_error_conventions_unchanged():
    """推演不存在 404 PROJECTION_NOT_FOUND；地层不在基线内 400 INVALID_STRATUM。"""
    state = projection_state()
    try:
        run_projection(state, 999, "L1", "L3")
    except ApiError as exc:
        assert exc.status_code == 404
        assert exc.code == "PROJECTION_NOT_FOUND"
        assert exc.details == {"projection_id": 999}
    else:
        raise AssertionError("expected PROJECTION_NOT_FOUND")

    try:
        run_projection(state, 1, "L1", "LZ")
    except ApiError as exc:
        assert exc.status_code == 400
        assert exc.code == "INVALID_STRATUM"
        assert exc.details == {"stratum_id": "LZ"}
    else:
        raise AssertionError("expected INVALID_STRATUM")


ALL_TESTS = [
    test_formal_uses_repeatable_read_snapshot_transaction,
    test_formal_concurrent_commit_before_active_load_returns_coherent_old_view,
    test_formal_concurrent_commit_between_derivation_and_path_detail,
    test_formal_read_committed_control_demonstrates_mix_is_observable_without_fix,
    test_formal_unknown_stratum_error_convention_unchanged,
    test_projection_baseline_response_is_coherent,
    test_projection_commit_before_view_load_returns_coherent_old_view,
    test_projection_commit_between_view_load_and_path_detail,
    test_projection_read_committed_control_demonstrates_mix_without_fix,
    test_projection_base_evidence_detail_consistent_under_formal_commit,
    test_closed_projections_still_query_frozen_view,
    test_projection_error_conventions_unchanged,
]


def main():
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
    print(f"ALL {len(ALL_TESTS)} RELATIONSHIP-VIEW TESTS PASSED")


if __name__ == "__main__":
    main()
