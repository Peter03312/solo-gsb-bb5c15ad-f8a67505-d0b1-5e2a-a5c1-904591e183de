"""定版成员时点一致性回归测试（内存假连接，无需真实数据库）。

修复的缺陷：定版只封存断言与已确定关系，历史关系查询与成果包封存却按**当前**
地层表校验——先定版、再登记新地层后，用旧版查询或封包时，新地层会被误当作
旧版的“未定”成员（伪造出并未封存过的未定结论）。

修复口径：
- 新定版冻结当时全部已登记地层为成员清单（snapshots.members）；
- 历史定版关系查询 / 成果包封存只接受该版成员：版外地层 400
  STRATUM_NOT_IN_VERSION 明确拒绝，不与版内真实未定混淆；
- 缺少成员清单的既有定版（members=NULL）只在其冻结断言副本 / 冻结结论足以
  证实时认定成员，无法证实时 409 SNAPSHOT_MEMBERSHIP_UNDETERMINABLE 明确
  拒绝，不伪造关系；
- 已封成果包内容保持原样：同定版 + 同集合的重复封存仍幂等返回原包；
- 未登记地层仍 400 INVALID_STRATUM、未知定版仍 404 SNAPSHOT_NOT_FOUND。

运行：python3 tests/test_snapshot_members.py
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
_psycopg2_extras = types.ModuleType("psycopg2.extras")
_psycopg2_extras.Json = lambda obj: obj
_psycopg2.pool = _psycopg2_pool
_psycopg2.extras = _psycopg2_extras
sys.modules.setdefault("psycopg2", _psycopg2)
sys.modules.setdefault("psycopg2.pool", _psycopg2_pool)
sys.modules.setdefault("psycopg2.extras", _psycopg2_extras)

# 环境内无 pydantic 时用最小桩替代（本测试用 SimpleNamespace 代替请求模型）
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

NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)


# ------------------------------------------------------------------ 内存假连接
class FakeState:
    """按 SQL 文本分发的内存状态；写语句落库并记录，供断言。"""

    def __init__(self):
        self.revision = 0
        self.strata = []          # 已登记地层（注册顺序）
        self.assertions = {}      # evidence_id -> (a, b, relation, status)
        self.snapshots = {}       # version -> dict(revision,label,assertions,members)
        self.conclusions = {}     # (version,a,b) -> (relation,path)
        self.packages = {}        # id -> dict(...)
        self.writes = []          # (表, 参数) 写记录


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    # ---- 工具 ----
    @property
    def state(self) -> FakeState:
        return self.conn.state

    def _snapshot_row(self, version):
        return self.state.snapshots.get(version)

    # ---- 分发 ----
    def execute(self, sql, params=None):
        norm = " ".join(sql.split()).upper()
        st = self.state

        if norm.startswith("SET TRANSACTION"):
            self._rows = []
            return

        # ---- 通用 ----
        if norm == "SELECT 1 FROM STRATA WHERE STRATUM_ID = %S":
            self._rows = [(1,)] if params[0] in st.strata else []
        elif norm == "SELECT STRATUM_ID FROM STRATA WHERE STRATUM_ID = ANY(%S)":
            self._rows = [(s,) for s in params[0] if s in st.strata]
        elif norm == "SELECT STRATUM_ID FROM STRATA ORDER BY STRATUM_ID":
            self._rows = [(s,) for s in sorted(st.strata)]
        elif norm == "SELECT REVISION FROM REVISION_COUNTER WHERE ID = TRUE FOR UPDATE":
            self._rows = [(st.revision,)]

        # ---- 定版 ----
        elif norm == "SELECT REVISION, ASSERTIONS, MEMBERS FROM SNAPSHOTS WHERE VERSION = %S":
            snap = self._snapshot_row(params[0])
            self._rows = (
                [] if snap is None else [(snap["revision"], snap["assertions"], snap["members"])]
            )
        elif norm == "SELECT REVISION, LABEL, ASSERTIONS, MEMBERS FROM SNAPSHOTS WHERE VERSION = %S":
            snap = self._snapshot_row(params[0])
            self._rows = (
                []
                if snap is None
                else [(snap["revision"], snap["label"], snap["assertions"], snap["members"])]
            )
        elif norm == "SELECT VERSION, REVISION, LABEL, ASSERTIONS, MEMBERS, CREATED_AT FROM SNAPSHOTS WHERE VERSION = %S":
            snap = self._snapshot_row(params[0])
            self._rows = (
                []
                if snap is None
                else [(
                    params[0], snap["revision"], snap["label"],
                    snap["assertions"], snap["members"], NOW,
                )]
            )
        elif norm.startswith("INSERT INTO SNAPSHOTS"):
            _, label, assertions, members = params
            version = max(st.snapshots, default=0) + 1
            st.snapshots[version] = {
                "revision": params[0], "label": label,
                "assertions": assertions, "members": members,
            }
            st.writes.append(("snapshots", params))
            self._rows = [(version,)]
        elif norm.startswith("INSERT INTO SNAPSHOT_CONCLUSIONS"):
            version, a, b, relation, path = params
            st.conclusions[(version, a, b)] = (relation, path)
            st.writes.append(("snapshot_conclusions", params))
            self._rows = []
        elif norm.startswith("SELECT 1 FROM SNAPSHOT_CONCLUSIONS"):
            version, stratum_id = params[0], params[1]
            hit = any(
                v == version and stratum_id in (a, b)
                for (v, a, b) in st.conclusions
            )
            self._rows = [(1,)] if hit else []
        elif norm.startswith("SELECT RELATION, PATH FROM SNAPSHOT_CONCLUSIONS"):
            self._rows = (
                []
                if (params[0], params[1], params[2]) not in st.conclusions
                else [st.conclusions[(params[0], params[1], params[2])]]
            )

        # ---- 断言（create_snapshot 读取生效断言） ----
        elif "FROM ASSERTIONS WHERE STATUS = 'ACTIVE'" in norm:
            self._rows = sorted(
                (eid, a, b, rel)
                for eid, (a, b, rel, status) in st.assertions.items()
                if status == "active"
            )

        # ---- 成果包 ----
        elif norm.startswith("SELECT ID, VERSION, REVISION, LABEL, STRATA, PAIRS, EVIDENCE, TRANSIT_STRATA, CONTENT_HASH, CREATED_AT FROM THEMATIC_PACKAGES"):
            version, strata_key = params
            row = next(
                (p for p in st.packages.values()
                 if p["version"] == version and p["strata_key"] == strata_key),
                None,
            )
            self._rows = [] if row is None else [self._package_tuple(row)]
        elif norm.startswith("INSERT INTO THEMATIC_PACKAGES"):
            (version, revision, label, strata, strata_key,
             pairs, evidence, transit, content_hash) = params
            st.writes.append(("thematic_packages", params))
            dup = any(
                p["version"] == version and p["strata_key"] == strata_key
                for p in st.packages.values()
            )
            if dup:
                self._rows = []  # ON CONFLICT DO NOTHING：无 RETURNING 行
            else:
                package_id = max(st.packages, default=0) + 1
                st.packages[package_id] = {
                    "id": package_id, "version": version, "revision": revision,
                    "label": label, "strata": list(strata), "strata_key": strata_key,
                    "pairs": pairs, "evidence": evidence, "transit_strata": list(transit),
                    "content_hash": content_hash, "created_at": NOW,
                }
                self._rows = [(package_id, NOW)]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    @staticmethod
    def _package_tuple(p):
        return (
            p["id"], p["version"], p["revision"], p["label"], p["strata"],
            p["pairs"], p["evidence"], p["transit_strata"], p["content_hash"],
            p["created_at"],
        )

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
        self.state = state

    def cursor(self):
        return _CursorCM(FakeCursor(self))


@contextmanager
def _fake_conn(state):
    yield FakeConn(state)


def _patch(module, state):
    original = module.db_conn
    module.db_conn = lambda: _fake_conn(state)
    return original


def _restore(module, original):
    module.db_conn = original


def run_snapshot_query(state, version, a, b):
    original = _patch(service_mod, state)
    try:
        return service_mod.query_snapshot_relationship(version, a, b)
    finally:
        _restore(service_mod, original)


def run_create_snapshot(state, label=None):
    original = _patch(service_mod, state)
    try:
        return service_mod.create_snapshot(label)
    finally:
        _restore(service_mod, original)


def run_get_snapshot(state, version):
    original = _patch(service_mod, state)
    try:
        return service_mod.get_snapshot(version)
    finally:
        _restore(service_mod, original)


def run_create_package(state, version, strata):
    original = _patch(packages_mod, state)
    body = types.SimpleNamespace(version=version, strata=strata)
    try:
        return packages_mod.create_package(body)
    finally:
        _restore(packages_mod, original)


def expect_error(status_code, code, fn, *args):
    try:
        fn(*args)
    except ApiError as exc:
        assert exc.status_code == status_code, (exc.status_code, exc.code, exc.message)
        assert exc.code == code, (exc.code, exc.message)
        return exc
    raise AssertionError(f"expected {code}")


# ------------------------------------------------------------------ 场景构造
FROZEN = [
    {"evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
]


def new_snapshot_state():
    """定版 1（新口径，members=["L1","L2","L3"]）；L4 在定版后才登记。"""
    st = FakeState()
    st.revision = 3
    st.strata = ["L1", "L2", "L3", "L4"]
    st.assertions = {
        "EV-001": ("L1", "L2", "same_period", "active"),
        "EV-002": ("L2", "L3", "earlier_than", "active"),
    }
    st.snapshots[1] = {
        "revision": 2, "label": "一期定版",
        "assertions": FROZEN, "members": ["L1", "L2", "L3"],
    }
    st.conclusions[(1, "L1", "L2")] = ("same_period", ["EV-001"])
    st.conclusions[(1, "L2", "L3")] = ("earlier_than", ["EV-002"])
    st.conclusions[(1, "L1", "L3")] = ("earlier_than", ["EV-001", "EV-002"])
    # (1, "L3", "L1") 等无结论行 -> 版内真实未定
    return st


def legacy_snapshot_state():
    """定版 1（既有定版，members=None）。L1/L2/L3 可由冻结断言证实，
    L9 已登记但无任何封存记录（不可判定），LX 未登记。"""
    st = new_snapshot_state()
    st.strata = ["L1", "L2", "L3", "L4", "L9"]
    st.snapshots[1]["members"] = None
    return st


# ------------------------------------------------------------------ A. 新定版（带成员清单）历史查询
def test_new_snapshot_member_queries_unchanged():
    """版内成员：确定结论、版内真实未定、同地层自反均保持原口径。"""
    st = new_snapshot_state()
    resp = run_snapshot_query(st, 1, "L1", "L3")
    assert resp["relation"] == "earlier_than"
    assert resp["evidence_path"] == ["EV-001", "EV-002"]
    assert [s["status"] for s in resp["path_detail"]] == ["active", "active"]
    assert resp["version"] == 1 and resp["snapshot_revision"] == 2

    und = run_snapshot_query(st, 1, "L3", "L1")  # 版内真实未定（无结论行）
    assert und["relation"] == "undetermined"
    assert und["evidence_path"] is None and und["path_detail"] == []

    same = run_snapshot_query(st, 1, "L2", "L2")
    assert same["relation"] == "same_period" and same["evidence_path"] == []


def test_new_snapshot_rejects_out_of_version_stratum():
    """定版后新登记的地层：明确拒绝，不得误当版内未定。"""
    st = new_snapshot_state()
    exc = expect_error(400, "STRATUM_NOT_IN_VERSION", run_snapshot_query, st, 1, "L1", "L4")
    assert exc.details == {"version": 1, "stratum_id": "L4"}
    # 反向同样拒绝；同地层自反也不例外
    expect_error(400, "STRATUM_NOT_IN_VERSION", run_snapshot_query, st, 1, "L4", "L1")
    expect_error(400, "STRATUM_NOT_IN_VERSION", run_snapshot_query, st, 1, "L4", "L4")


def test_new_snapshot_unregistered_and_unknown_version_unchanged():
    """未登记地层仍 400 INVALID_STRATUM；未知定版仍 404 SNAPSHOT_NOT_FOUND。"""
    st = new_snapshot_state()
    exc = expect_error(400, "INVALID_STRATUM", run_snapshot_query, st, 1, "L1", "LX")
    assert exc.details == {"stratum_id": "LX"}
    expect_error(404, "SNAPSHOT_NOT_FOUND", run_snapshot_query, st, 99, "L1", "L2")


# ------------------------------------------------------------------ B. 既有定版（members=NULL）
def test_legacy_provable_member_by_frozen_assertions():
    """既有定版：地层出现在冻结断言副本中即证实为成员，查询正常。"""
    st = legacy_snapshot_state()
    resp = run_snapshot_query(st, 1, "L1", "L3")
    assert resp["relation"] == "earlier_than"
    assert resp["evidence_path"] == ["EV-001", "EV-002"]


def test_legacy_provable_member_by_conclusions_only():
    """既有定版：仅出现在冻结结论（不在冻结断言）中也可证实为成员。"""
    st = legacy_snapshot_state()
    # L4 不在冻结断言里，但出现在冻结结论中（合成场景，验证结论也可作证）
    st.conclusions[(1, "L4", "L1")] = ("later_than", ["EV-001"])
    resp = run_snapshot_query(st, 1, "L4", "L1")
    assert resp["relation"] == "later_than"


def test_legacy_unprovable_membership_undeterminable():
    """既有定版：封存记录无法证实的地层按不可判定拒绝，不伪造关系。"""
    st = legacy_snapshot_state()
    exc = expect_error(
        409, "SNAPSHOT_MEMBERSHIP_UNDETERMINABLE", run_snapshot_query, st, 1, "L1", "L9"
    )
    assert exc.details == {"version": 1, "stratum_id": "L9"}
    expect_error(409, "SNAPSHOT_MEMBERSHIP_UNDETERMINABLE", run_snapshot_query, st, 1, "L9", "L9")


def test_legacy_unregistered_still_invalid_stratum():
    """既有定版：当前未登记的地层仍 400 INVALID_STRATUM（先于成员判定）。"""
    st = legacy_snapshot_state()
    expect_error(400, "INVALID_STRATUM", run_snapshot_query, st, 1, "L1", "LX")


# ------------------------------------------------------------------ C. 定版封存冻结成员清单
def test_create_snapshot_freezes_members():
    """新定版把当时全部已登记地层（按编号排序）冻结为成员清单。"""
    st = FakeState()
    st.revision = 1
    st.strata = ["L2", "L1", "L3"]  # 注册顺序乱序；成员清单须按编号排序
    st.assertions = {
        "EV-001": ("L1", "L2", "same_period", "active"),
    }
    result = run_create_snapshot(st, "一期定版")
    assert result["version"] == 1
    assert result["revision"] == 1
    assert result["members"] == 3
    assert result["conclusions"] == 2  # L1~L2 与 L2~L1 两向同一期

    snap_writes = [p for (table, p) in st.writes if table == "snapshots"]
    assert len(snap_writes) == 1
    _, label, assertions, members = snap_writes[0]
    assert label == "一期定版"
    assert members == ["L1", "L2", "L3"], members  # 冻结成员=当时全部已登记地层
    assert [a["evidence_id"] for a in assertions] == ["EV-001"]

    # 结论行只覆盖版内成员（L1/L2 双向），不涉及版外地层
    assert st.conclusions[(1, "L1", "L2")][0] == "same_period"
    assert st.conclusions[(1, "L2", "L1")][0] == "same_period"
    assert all(v == 1 and a in members and b in members for (v, a, b) in st.conclusions)


def test_get_snapshot_exposes_members():
    """定版详情返回成员清单；既有定版（members=NULL）返回 null。"""
    st = new_snapshot_state()
    detail = run_get_snapshot(st, 1)
    assert detail["members"] == ["L1", "L2", "L3"]
    assert detail["revision"] == 2

    st.snapshots[1]["members"] = None
    assert run_get_snapshot(st, 1)["members"] is None


# ------------------------------------------------------------------ D. 成果包封存
def test_package_seal_with_members_ok():
    """版内成员封存：201 首次创建，pairs / evidence 口径不变。"""
    st = new_snapshot_state()
    payload, created = run_create_package(st, 1, ["L1", "L3"])
    assert created is True
    assert payload["version"] == 1 and payload["snapshot_revision"] == 2
    assert payload["strata"] == ["L1", "L3"]
    pair = payload["pairs"][0]
    assert pair["relation"] == "earlier_than"
    assert pair["evidence_path"] == ["EV-001", "EV-002"]
    assert [s["evidence_id"] for s in payload["evidence"]] == ["EV-001", "EV-002"]
    # 重复封存同集合：幂等返回同一份
    again, created2 = run_create_package(st, 1, ["L3", "L1"])
    assert created2 is False
    assert again["id"] == payload["id"] and again["content_hash"] == payload["content_hash"]


def test_package_member_genuine_undetermined_pair():
    """版内成员间的真实未定对照常封存（无路径），不与版外拒绝混淆。"""
    st = new_snapshot_state()
    payload, created = run_create_package(st, 1, ["L1", "L2", "L3"])
    assert created is True
    und = [p for p in payload["pairs"] if p["relation"] == "undetermined"]
    assert und == [] or all("evidence_path" not in p for p in und)


def test_package_rejects_out_of_version_stratum():
    """含版外地层的封存明确拒绝，不写任何包。"""
    st = new_snapshot_state()
    exc = expect_error(400, "STRATUM_NOT_IN_VERSION", run_create_package, st, 1, ["L1", "L4"])
    assert exc.details == {"version": 1, "stratum_ids": ["L4"]}
    assert st.packages == {}, "拒绝后不得留下半成品包"
    assert not any(t == "thematic_packages" for (t, _) in st.writes)


def test_package_replay_existing_returned_despite_membership():
    """已封成果包保持原样：同定版 + 同集合重复封存仍幂等返回原包。

    模拟修复前已封存的包（既有定版 + 现不可证实的地层 L9）：重复封存不再
    重新判定成员，而是返回已封存的那一份。
    """
    st = legacy_snapshot_state()
    strata = sorted(["L1", "L9"])
    key = packages_mod._strata_key(1, strata)
    st.packages[1] = {
        "id": 1, "version": 1, "revision": 2, "label": "一期定版",
        "strata": strata, "strata_key": key,
        "pairs": [{"stratum_a": "L1", "stratum_b": "L9",
                   "relation": "undetermined", "relation_label": "未定"}],
        "evidence": [], "transit_strata": [],
        "content_hash": "sealed-hash", "created_at": NOW,
    }
    payload, created = run_create_package(st, 1, ["L9", "L1"])  # 乱序同集合
    assert created is False
    assert payload["id"] == 1 and payload["content_hash"] == "sealed-hash"
    assert payload["pairs"][0]["relation"] == "undetermined"  # 已封内容原样
    # 但同集合若尚未封存，则按不可判定拒绝（不新造）
    st.packages.clear()
    expect_error(409, "SNAPSHOT_MEMBERSHIP_UNDETERMINABLE", run_create_package, st, 1, ["L1", "L9"])
    assert st.packages == {}


def test_package_legacy_provable_members_ok():
    """既有定版：全部入选地层均可由封存记录证实时，封存正常。"""
    st = legacy_snapshot_state()
    payload, created = run_create_package(st, 1, ["L1", "L2", "L3"])
    assert created is True
    assert len(payload["pairs"]) == 3


def test_package_unregistered_still_invalid_stratum():
    """未登记地层仍 400 INVALID_STRATUM（先于成员判定）。"""
    st = new_snapshot_state()
    exc = expect_error(400, "INVALID_STRATUM", run_create_package, st, 1, ["L1", "LX"])
    assert exc.details["stratum_id"] == "LX"
    expect_error(404, "SNAPSHOT_NOT_FOUND", run_create_package, st, 99, ["L1", "L2"])


ALL_TESTS = [
    test_new_snapshot_member_queries_unchanged,
    test_new_snapshot_rejects_out_of_version_stratum,
    test_new_snapshot_unregistered_and_unknown_version_unchanged,
    test_legacy_provable_member_by_frozen_assertions,
    test_legacy_provable_member_by_conclusions_only,
    test_legacy_unprovable_membership_undeterminable,
    test_legacy_unregistered_still_invalid_stratum,
    test_create_snapshot_freezes_members,
    test_get_snapshot_exposes_members,
    test_package_seal_with_members_ok,
    test_package_member_genuine_undetermined_pair,
    test_package_rejects_out_of_version_stratum,
    test_package_replay_existing_returned_despite_membership,
    test_package_legacy_provable_members_ok,
    test_package_unregistered_still_invalid_stratum,
]


def main():
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
    print(f"ALL {len(ALL_TESTS)} SNAPSHOT-MEMBERS TESTS PASSED")


if __name__ == "__main__":
    main()
