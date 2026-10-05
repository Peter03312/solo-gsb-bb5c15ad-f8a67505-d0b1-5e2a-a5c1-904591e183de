"""历史定版关系查询“逐步明细按封存时断言返回”的回归测试（无需真实数据库）。

修复的混时态缺陷：定版（snapshot）之后，某条支撑证据被撤回（正式批次、野外包
勘误或推演并入都会把正式 assertions 置为 retracted），此时
GET /snapshots/{version}/relationship 的关系结论与 evidence_path 仍是封存值，
但 path_detail 取自在线断言表，逐步 status 变成 retracted——与同一定版封存的
成果包证据明细（取定版冻结断言副本 snapshots.assertions，恒为封存时的
active）相互矛盾。

修复口径：定版查询的逐步明细只按定版冻结的断言副本逐步展开，不读在线断言表：
- 后续正式批次 / 野外包勘误 / 推演并入撤回证据后，历史定版的结论、最优路径与
  逐步明细（含每步 status）保持封存时口径；
- 与同一定版封存的成果包（packages 模块同一冻结来源）逐步一致；
- 当前正式关系查询 GET /relationship 继续显示现时状态（在线断言表）；
- 输入、响应字段、路径优选与未知定版（404 SNAPSHOT_NOT_FOUND）/ 未知地层
  （400 INVALID_STRATUM）错误约定不变；查询只读，不写断言或修订。

用内存假连接驱动 service.query_snapshot_relationship / service.query_relationship：
- 假游标按 SQL 文本分发到内存状态；定版查询若触碰在线 assertions 表或任何写
  语句即失败（证明明细来源是冻结副本，后续撤回不可能影响历史明细）；
- 成果包一致性用 packages._load_snapshot（成果包封存的真实冻结来源加载器）
  构造期望明细逐步比对。

运行：python3 tests/test_snapshot_relationship_frozen_detail.py
"""
from __future__ import annotations

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

from app import packages as packages_mod  # noqa: E402
from app import service as service_mod  # noqa: E402
from app.errors import ApiError  # noqa: E402


# ------------------------------------------------------------------ 内存假连接
class FakeCursor:
    """按 SQL 文本把查询分发到内存状态。

    strict_snapshot=True 时模拟定版查询的纪律要求：不允许触碰在线 assertions
    表（明细必须来自定版冻结副本），也不允许任何写语句。
    """

    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        norm = " ".join(sql.split()).upper()

        for forbidden in ("INSERT ", "UPDATE ", "DELETE "):
            assert not norm.startswith(forbidden), f"关系查询不得写入: {sql}"

        if norm.startswith("SET TRANSACTION"):
            assert "REPEATABLE READ" in norm and "READ ONLY" in norm, sql
            self._rows = []
            return

        if self.conn.strict_snapshot and "FROM ASSERTIONS" in norm:
            raise AssertionError(f"定版查询不得读取在线断言表: {sql}")

        # ---- 定版关系查询路径 ----
        if norm == "SELECT REVISION, ASSERTIONS FROM SNAPSHOTS WHERE VERSION = %S":
            snap = self.conn.live["snapshots"].get(params[0])
            self._rows = [] if snap is None else [(snap["revision"], snap["assertions"])]
        elif norm == "SELECT REVISION, LABEL, ASSERTIONS FROM SNAPSHOTS WHERE VERSION = %S":
            snap = self.conn.live["snapshots"].get(params[0])
            self._rows = (
                [] if snap is None else [(snap["revision"], snap["label"], snap["assertions"])]
            )
        elif norm == "SELECT 1 FROM STRATA WHERE STRATUM_ID = %S":
            self._rows = [(1,)] if params[0] in self.conn.live["strata"] else []
        elif norm.startswith("SELECT RELATION, PATH FROM SNAPSHOT_CONCLUSIONS"):
            key = (params[0], params[1], params[2])
            conclusion = self.conn.live["snapshot_conclusions"].get(key)
            self._rows = [] if conclusion is None else [conclusion]

        # ---- 正式关系查询路径（现时状态对照） ----
        elif norm == "SELECT REVISION FROM REVISION_COUNTER WHERE ID = TRUE":
            self._rows = [(self.conn.live["revision"],)]
        elif "FROM ASSERTIONS WHERE STATUS = 'ACTIVE'" in norm:
            self._rows = sorted(
                (eid, a, b, rel)
                for eid, (a, b, rel, status) in self.conn.live["assertions"].items()
                if status == "active"
            )
        elif "FROM ASSERTIONS WHERE EVIDENCE_ID = ANY(%S)" in norm:
            self._rows = [
                (eid, *self.conn.live["assertions"][eid])
                for eid in params[0]
                if eid in self.conn.live["assertions"]
            ]
        else:
            raise AssertionError(f"unexpected SQL in relationship query: {sql}")

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
    def __init__(self, live, strict_snapshot=False):
        self.live = live
        self.strict_snapshot = strict_snapshot

    def cursor(self):
        return _CursorCM(FakeCursor(self))


@contextmanager
def _fake_conn(live, strict_snapshot=False):
    yield FakeConn(live, strict_snapshot=strict_snapshot)


def run_snapshot(live, version, a, b, strict_snapshot=True):
    original = service_mod.db_conn
    service_mod.db_conn = lambda: _fake_conn(live, strict_snapshot=strict_snapshot)
    try:
        return service_mod.query_snapshot_relationship(version, a, b)
    finally:
        service_mod.db_conn = original


def run_formal(live, a, b):
    original = service_mod.db_conn
    service_mod.db_conn = lambda: _fake_conn(live)
    try:
        return service_mod.query_relationship(a, b)
    finally:
        service_mod.db_conn = original


def load_package_frozen(live, version):
    """用成果包封存的真实冻结来源加载器读取定版冻结断言（同一来源口径）。"""
    original = packages_mod.db_conn
    packages_mod.db_conn = lambda: _fake_conn(live)
    try:
        with packages_mod.db_conn() as conn:
            with conn.cursor() as cur:
                return packages_mod._load_snapshot(cur, version)
    finally:
        packages_mod.db_conn = original


# ------------------------------------------------------------------ 场景构造
FROZEN_ASSERTIONS = [
    {"evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"},
]


def sealed_state():
    """定版 1 封存于修订 2（EV-001/EV-002/EV-003 全部生效）。

    之后正式状态前进到修订 4：EV-002 被正式批次撤回（修订 3），EV-003 被野外包
    勘误 / 推演并入撤回（修订 4）——撤回来源只改变在线 assertions 表，定版查询
    的冻结明细不得随之变化。
    """
    return {
        "revision": 4,
        "strata": {"L1", "L2", "L3", "L4"},
        "assertions": {
            "EV-001": ("L1", "L2", "same_period", "active"),
            "EV-002": ("L2", "L3", "earlier_than", "retracted"),
            "EV-003": ("L3", "L4", "earlier_than", "retracted"),
        },
        "snapshots": {
            1: {"revision": 2, "label": "二期定版", "assertions": FROZEN_ASSERTIONS},
        },
        "snapshot_conclusions": {
            (1, "L1", "L2"): ("same_period", ["EV-001"]),
            (1, "L1", "L3"): ("earlier_than", ["EV-001", "EV-002"]),
            (1, "L1", "L4"): ("earlier_than", ["EV-001", "EV-002", "EV-003"]),
            (1, "L2", "L3"): ("earlier_than", ["EV-002"]),
            (1, "L2", "L4"): ("earlier_than", ["EV-002", "EV-003"]),
            (1, "L3", "L4"): ("earlier_than", ["EV-003"]),
            (1, "L4", "L1"): ("later_than", ["EV-001", "EV-002", "EV-003"]),
            # (1, "L3", "L1") 等反向对同理；未定对（无结论行）由查询路径覆盖
        },
    }


# ------------------------------------------------------------------ 定版查询：冻结明细
def test_snapshot_detail_frozen_after_retractions():
    """定版后撤回路径证据（正式批次 / 勘误 / 并入）：结论、路径与逐步明细保持封存口径。"""
    state = sealed_state()
    #  Sanity：在线断言表中 EV-002 / EV-003 确已撤回
    assert state["assertions"]["EV-002"][3] == "retracted"
    assert state["assertions"]["EV-003"][3] == "retracted"

    resp = run_snapshot(state, 1, "L1", "L4")
    assert resp["relation"] == "earlier_than", resp
    assert resp["relation_label"] == "早于"
    assert resp["evidence_path"] == ["EV-001", "EV-002", "EV-003"], resp
    assert [s["evidence_id"] for s in resp["path_detail"]] == ["EV-001", "EV-002", "EV-003"]
    for step in resp["path_detail"]:
        assert step["status"] == "active", f"历史定版明细不得显示撤回后状态: {step}"
    by_id = {s["evidence_id"]: s for s in resp["path_detail"]}
    assert by_id["EV-002"]["stratum_a"] == "L2" and by_id["EV-002"]["stratum_b"] == "L3"
    assert by_id["EV-002"]["relation"] == "earlier_than"
    assert resp["version"] == 1 and resp["snapshot_revision"] == 2
    assert resp["stratum_a"] == "L1" and resp["stratum_b"] == "L4"


def test_snapshot_detail_matches_package_frozen_source():
    """定版查询明细与成果包证据明细同一冻结来源：逐步一致。"""
    state = sealed_state()
    _, _, frozen_by_id = load_package_frozen(state, 1)  # 成果包封存的真实加载器

    resp = run_snapshot(state, 1, "L1", "L4")
    expected = [
        {
            "evidence_id": eid,
            "stratum_a": frozen_by_id[eid]["stratum_a"],
            "stratum_b": frozen_by_id[eid]["stratum_b"],
            "relation": frozen_by_id[eid]["relation"],
            "status": frozen_by_id[eid]["status"],  # 成果包口径：封存时 active
        }
        for eid in resp["evidence_path"]
    ]
    assert resp["path_detail"] == expected, (resp["path_detail"], expected)


def test_snapshot_query_never_reads_live_assertions():
    """严格模式：定版查询触碰在线断言表即失败——撤回不可能影响历史明细。"""
    state = sealed_state()
    # strict_snapshot=True（run_snapshot 默认）：任何 FROM assertions 都抛错
    resp = run_snapshot(state, 1, "L2", "L4")
    assert resp["relation"] == "earlier_than"
    assert [s["status"] for s in resp["path_detail"]] == ["active", "active"]


def test_snapshot_response_fields_unchanged():
    """响应字段集合与取值口径不变（含反向 later_than 与未定）。"""
    state = sealed_state()
    resp = run_snapshot(state, 1, "L4", "L1")
    assert set(resp) == {
        "stratum_a", "stratum_b", "relation", "relation_label",
        "evidence_path", "path_detail", "version", "snapshot_revision",
    }
    assert resp["relation"] == "later_than" and resp["relation_label"] == "晚于"
    assert resp["evidence_path"] == ["EV-001", "EV-002", "EV-003"]
    assert all(s["status"] == "active" for s in resp["path_detail"])

    undetermined = run_snapshot(state, 1, "L3", "L1")  # 无结论行 -> 未定
    assert undetermined["relation"] == "undetermined"
    assert undetermined["relation_label"] == "未定"
    assert undetermined["evidence_path"] is None
    assert undetermined["path_detail"] == []

    same = run_snapshot(state, 1, "L2", "L2")  # 同一地层 -> 同一期，空路径
    assert same["relation"] == "same_period"
    assert same["evidence_path"] == []
    assert same["path_detail"] == []


def test_snapshot_error_conventions_unchanged():
    """未知定版 404 SNAPSHOT_NOT_FOUND；未知地层 400 INVALID_STRATUM。"""
    state = sealed_state()
    try:
        run_snapshot(state, 99, "L1", "L2")
    except ApiError as exc:
        assert exc.status_code == 404 and exc.code == "SNAPSHOT_NOT_FOUND", exc.code
        assert exc.details == {"version": 99}
    else:
        raise AssertionError("expected SNAPSHOT_NOT_FOUND")

    try:
        run_snapshot(state, 1, "L1", "LX")
    except ApiError as exc:
        assert exc.status_code == 400 and exc.code == "INVALID_STRATUM", exc.code
        assert exc.details == {"stratum_id": "LX"}
    else:
        raise AssertionError("expected INVALID_STRATUM")


# ------------------------------------------------------------------ 正式查询：现时状态不变
def test_formal_query_still_shows_current_status():
    """当前正式关系继续显示现时状态：撤回后结论变未定，明细取自在线断言表。"""
    state = sealed_state()
    resp = run_formal(state, "L1", "L4")
    assert resp["revision"] == 4
    assert resp["relation"] == "undetermined", resp
    assert resp["evidence_path"] is None
    assert resp["path_detail"] == []

    # 仍生效的 EV-001：正式查询按在线状态显示 active
    resp2 = run_formal(state, "L1", "L2")
    assert resp2["relation"] == "same_period"
    assert resp2["evidence_path"] == ["EV-001"]
    assert resp2["path_detail"][0]["status"] == "active"

    # 在线再撤回 EV-001：正式查询随即反映，定版查询仍冻结
    state["assertions"]["EV-001"] = ("L1", "L2", "same_period", "retracted")
    state["revision"] = 5
    assert run_formal(state, "L1", "L2")["relation"] == "undetermined"
    frozen = run_snapshot(state, 1, "L1", "L2")
    assert frozen["relation"] == "same_period"
    assert frozen["path_detail"][0]["status"] == "active"


def test_control_old_detail_source_would_show_retracted():
    """对照：修复前的明细来源（在线断言表）在同一状态下必然混时态。

    证明本测试对缺陷有辨别力：_path_details（旧来源）会把已撤回的 EV-002
    显示为 retracted，而定版查询的新来源保持封存时的 active。
    """
    state = sealed_state()
    original = service_mod.db_conn
    service_mod.db_conn = lambda: _fake_conn(state)
    try:
        with service_mod.db_conn() as conn:
            with conn.cursor() as cur:
                old_style = service_mod._path_details(cur, ["EV-001", "EV-002"])
    finally:
        service_mod.db_conn = original
    assert {s["evidence_id"]: s["status"] for s in old_style} == {
        "EV-001": "active",
        "EV-002": "retracted",  # 旧来源在定版查询中造成混时态
    }

    resp = run_snapshot(state, 1, "L1", "L3")
    assert resp["evidence_path"] == ["EV-001", "EV-002"]
    assert {s["evidence_id"]: s["status"] for s in resp["path_detail"]} == {
        "EV-001": "active",
        "EV-002": "active",
    }


ALL_TESTS = [
    test_snapshot_detail_frozen_after_retractions,
    test_snapshot_detail_matches_package_frozen_source,
    test_snapshot_query_never_reads_live_assertions,
    test_snapshot_response_fields_unchanged,
    test_snapshot_error_conventions_unchanged,
    test_formal_query_still_shows_current_status,
    test_control_old_detail_source_would_show_retracted,
]


def main():
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
    print(f"ALL {len(ALL_TESTS)} SNAPSHOT-FROZEN-DETAIL TESTS PASSED")


if __name__ == "__main__":
    main()
