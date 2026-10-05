"""两类关系查询“同一次响应同一完整视图”的端到端并发验证（需真实 PostgreSQL）。

与 tests/test_relationship_view_consistency.py（内存假连接）互补：本脚本启动嵌入式
PostgreSQL（pgserver，pip 包，非 requirements 依赖；环境缺失则跳过），通过给连接池
注入“语句门闩”游标，在真实服务代码的指定读取语句处把查询线程挂起，由另一线程提交
并发批次，确定性地复现“读取之间提交”的交错：

A. 修订号先读、生效断言后读 —— 旧修订号配新结论；
B. 路径已算出、逐步明细后读 —— 路径中的证据在明细中显示为已撤回。

修复后（查询在 REPEATABLE READ READ ONLY 快照事务内完成）：
- 查询事务快照在第一条 SELECT 固定，门闩期间提交的批次对其不可见，
  响应整体是提交前完整视图（旧修订号 + 旧结论 + 旧明细）；
- 门闩释放后的全新查询整体看到提交后的新视图。

另含多线程压力：单写线程制造修订间确定性的关系翻转，多个读线程持续查询，
逐条核对“响应内修订号对应的关系结论”与“逐步明细全部 active”不变量。

运行：
    PYTHONPATH=/tmp/pylibs python3 tests/test_relationship_view_consistency_e2e.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pgserver  # type: ignore
    import psycopg2
    import psycopg2.extensions
    import psycopg2.pool
except ImportError as exc:  # pragma: no cover
    print(f"SKIP: 缺少真实 PostgreSQL 验证依赖（{exc.name}）；内存版验证见 "
          "tests/test_relationship_view_consistency.py")
    sys.exit(0)

from app import db, projections as projections_mod, service  # noqa: E402
from app.models import AssertOp, BatchIn, ProjectionBatchIn, RetractOp  # noqa: E402

PGDATA = "/tmp/pgdata-relation-view-e2e"


# ------------------------------------------------------------------ 门闩游标
class Gate:
    def __init__(self):
        self.predicate = lambda sql: False
        self.parked = threading.Event()
        self.release = threading.Event()
        self.armed = False

    def configure(self, predicate):
        self.predicate = predicate
        self.parked.clear()
        self.release.clear()
        self.armed = True

    def disarm(self):
        self.armed = False
        self.release.set()


GATE = Gate()


class GatedCursor(psycopg2.extensions.cursor):
    def execute(self, query, vars=None):  # noqa: A002 - psycopg2 API
        if GATE.armed and GATE.predicate(query):
            GATE.armed = False  # 每次门闩只拦一条语句
            GATE.parked.set()
            GATE.release.wait(timeout=30)
        return super().execute(query, vars)


# ------------------------------------------------------------------ 环境准备
def start_server():
    os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-relation-view")
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
    os.chmod(os.environ["XDG_RUNTIME_DIR"], 0o700)
    srv = pgserver.get_server(PGDATA)
    uri = srv.get_uri()
    # 建应用角色与库（幂等）
    conn = psycopg2.connect(uri)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname='strat'")
        if cur.fetchone() is None:
            cur.execute("CREATE ROLE strat LOGIN PASSWORD 'strat'")
        cur.execute("SELECT 1 FROM pg_database WHERE datname='stratigraphy'")
        if cur.fetchone() is None:
            cur.execute("CREATE DATABASE stratigraphy OWNER strat")
    conn.close()
    return srv, uri


def init_app_pool():
    """初始化应用连接池（建表 + 回填），随后换成带门闩游标的连接池。"""
    db.DATABASE_URL = os.environ["DATABASE_URL"]
    db.init_pool(retries=10, delay=1.0)
    db._pool.closeall()
    db._pool = psycopg2.pool.ThreadedConnectionPool(
        2, 20, db.DATABASE_URL, cursor_factory=GatedCursor
    )


ALL_TABLES = (
    "evidence_hold_items", "evidence_holds",
    "field_package_corrections", "field_packages", "field_short_bindings",
    "thematic_packages", "snapshot_conclusions", "snapshots",
    "revision_changes_sealed", "revision_changes", "revisions",
    "projection_revisions", "projection_retractions", "projection_assertions",
    "projections", "assertions", "strata",
)


def reset_state():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
            cur.execute("UPDATE revision_counter SET revision = 0 WHERE id = TRUE")


def register_strata():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            for s in ("L1", "L2", "L3", "L4"):
                cur.execute(
                    "INSERT INTO strata (stratum_id, site) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (s, "甲遗址"),
                )


def seed_revision_one():
    """修订 1：E1(L1~L2)、E2(L2<L3)，L1 早于 L3，最优路径 [E1, E2]。"""
    register_strata()
    service.apply_batch(BatchIn(expected_revision=0, operations=[
        AssertOp(op="assert", evidence_id="E1", stratum_a="L1", stratum_b="L2",
                 relation="same_period"),
        AssertOp(op="assert", evidence_id="E2", stratum_a="L2", stratum_b="L3",
                 relation="earlier_than"),
    ]))


def seed_revision_one_same_only():
    """修订 1：仅 E1(L1~L2)；L3/L4 已登记但孤立。推演场景用：基线不含 L2<L3，
    推演内新增的 PE1 是唯一的 L2 早于 L3 证据，撤回后关系确定翻转为未定。"""
    register_strata()
    service.apply_batch(BatchIn(expected_revision=0, operations=[
        AssertOp(op="assert", evidence_id="E1", stratum_a="L1", stratum_b="L2",
                 relation="same_period"),
    ]))


def assert_op(eid, a, b, relation):
    return AssertOp(op="assert", evidence_id=eid, stratum_a=a, stratum_b=b, relation=relation)


def retract_op(eid):
    return RetractOp(op="retract", evidence_id=eid)


def check_formal_old_view(resp, label):
    assert resp["revision"] == 1, (label, resp["revision"])
    assert resp["relation"] == "earlier_than", (label, resp["relation"])
    assert resp["evidence_path"] == ["E1", "E2"], (label, resp["evidence_path"])
    statuses = {s["evidence_id"]: s["status"] for s in resp["path_detail"]}
    assert statuses == {"E1": "active", "E2": "active"}, (label, statuses)


def gated_reader(gate_predicate, reader_fn):
    """把门闩挂在 gate_predicate 上执行 reader_fn；返回 (响应, 写线程结果, 写线程异常)。"""
    writer_box = {}

    def reader():
        try:
            writer_box["reader"] = reader_fn()
        except Exception as exc:  # noqa: BLE001
            writer_box["reader_exc"] = exc

    rt = threading.Thread(target=reader)
    rt.start()
    assert GATE.parked.wait(timeout=15), "读线程未在预期语句处挂起"
    return rt, writer_box


# ------------------------------------------------------------------ 正式查询：确定性交错
def scenario_formal_old_revision_with_new_conclusion():
    """门闩挂在生效断言读取前：写线程提交修订 2（撤回 E2）后放行。

    期望：读线程整体停留在提交前视图（修订 1、早于、E2 active）。
    """
    GATE.configure(lambda sql: "FROM assertions WHERE status = 'active'" in sql)
    rt, box = gated_reader(None, lambda: service.query_relationship("L1", "L3"))

    # 读线程已在修订号（修订 1）与 strata 校验之后、生效断言读取之前挂起
    service.apply_batch(BatchIn(expected_revision=1, operations=[retract_op("E2")]))
    GATE.release.set()
    rt.join(timeout=15)
    assert not rt.is_alive(), "读线程未结束"
    assert "reader_exc" not in box, box.get("reader_exc")
    check_formal_old_view(box["reader"], "formal-A")

    fresh = service.query_relationship("L1", "L3")
    assert fresh["revision"] == 2 and fresh["relation"] == "undetermined", fresh
    print("PASS formal-A 提交插在修订号与生效断言读取之间：响应整体为提交前视图")


def scenario_formal_path_detail_retracted():
    """门闩挂在逐步明细（ANY）读取前：写线程撤回 E2 后放行。

    期望：结论/路径仍是提交前的早于 [E1,E2]，逐步明细同样来自同一快照（E2 active），
    不出现“路径引用 E2 但明细 status=retracted”的混版。
    """
    GATE.configure(lambda sql: "FROM assertions WHERE evidence_id = ANY(%s)" in sql)
    rt, box = gated_reader(None, lambda: service.query_relationship("L1", "L3"))
    service.apply_batch(BatchIn(expected_revision=1, operations=[retract_op("E2")]))
    GATE.release.set()
    rt.join(timeout=15)
    assert not rt.is_alive()
    assert "reader_exc" not in box, box.get("reader_exc")
    check_formal_old_view(box["reader"], "formal-B")
    print("PASS formal-B 提交插在结论与逐步明细之间：路径证据不显示为已撤回")


# ------------------------------------------------------------------ 推演查询：确定性交错
def make_projection_with_one_added_assertion():
    """创建推演（基线只有 E1），推演修订 1 新增 PE1（L2<L3），返回推演 id。"""
    created = projections_mod.create_projection(None)
    pid = created["id"]
    projections_mod.apply_projection_batch(
        pid,
        ProjectionBatchIn(expected_revision=0, operations=[assert_op("PE1", "L2", "L3", "earlier_than")]),
    )
    return pid


def check_projection_old_view(resp, label):
    assert resp["projection_revision"] == 1, (label, resp["projection_revision"])
    assert resp["relation"] == "earlier_than", (label, resp["relation"])
    assert resp["evidence_path"] == ["E1", "PE1"], (label, resp["evidence_path"])
    statuses = {s["evidence_id"]: (s["scope"], s["status"]) for s in resp["path_detail"]}
    assert statuses == {"E1": ("base", "active"), "PE1": ("projection", "active")}, (label, statuses)


def scenario_projection_old_revision_with_new_conclusion():
    """门闩挂在推演增撤记录读取前：写线程提交推演修订 2（撤回 PE1）后放行。"""
    pid = make_projection_with_one_added_assertion()
    GATE.configure(
        lambda sql: "FROM projection_assertions WHERE projection_id = %s ORDER BY evidence_id" in sql
    )
    rt, box = gated_reader(
        None, lambda: projections_mod.query_projection_relationship(pid, "L1", "L3")
    )
    projections_mod.apply_projection_batch(
        pid, ProjectionBatchIn(expected_revision=1, operations=[retract_op("PE1")])
    )
    GATE.release.set()
    rt.join(timeout=15)
    assert not rt.is_alive()
    assert "reader_exc" not in box, box.get("reader_exc")
    check_projection_old_view(box["reader"], "projection-A")

    fresh = projections_mod.query_projection_relationship(pid, "L1", "L3")
    assert fresh["projection_revision"] == 2 and fresh["relation"] == "undetermined", fresh
    print("PASS projection-A 推演批次插在推演修订号与增撤记录之间：响应整体为提交前视图")


def scenario_projection_path_detail_retracted():
    """门闩挂在推演逐步明细读取前：写线程撤回 PE1 后放行，明细仍按旧视图 active。"""
    pid = make_projection_with_one_added_assertion()
    GATE.configure(
        lambda sql: "FROM projection_assertions WHERE projection_id = %s AND evidence_id = ANY(%s)" in sql
    )
    rt, box = gated_reader(
        None, lambda: projections_mod.query_projection_relationship(pid, "L1", "L3")
    )
    projections_mod.apply_projection_batch(
        pid, ProjectionBatchIn(expected_revision=1, operations=[retract_op("PE1")])
    )
    GATE.release.set()
    rt.join(timeout=15)
    assert not rt.is_alive()
    assert "reader_exc" not in box, box.get("reader_exc")
    check_projection_old_view(box["reader"], "projection-B")
    print("PASS projection-B 推演批次插在结论与逐步明细之间：路径证据不显示为已撤回")


def scenario_closed_projection_still_reads_frozen():
    """已并入 / 已放弃推演仍可查询冻结视图。"""
    # merged
    pid_m = make_projection_with_one_added_assertion()
    projections_mod.merge_projection(pid_m)
    resp = projections_mod.query_projection_relationship(pid_m, "L1", "L3")
    assert resp["status"] == "merged" and resp["relation"] == "earlier_than", resp

    # discarded（另一个净变更为空放弃的推演）
    created = projections_mod.create_projection(None)
    pid_d = created["id"]
    projections_mod.discard_projection(pid_d)
    resp = projections_mod.query_projection_relationship(pid_d, "L1", "L2")
    assert resp["status"] == "discarded" and resp["relation"] == "same_period", resp
    print("PASS closed 已关闭推演（merged/discarded）冻结视图仍可查")


# ------------------------------------------------------------------ 多线程压力
def stress_formal():
    reset_state()
    seed_revision_one()
    # 期望：修订 1 早于；偶数修订撤回当前 L2<L3 证据（未定）；奇数修订重新断言（早于）
    expected_relation = {1: "earlier_than"}
    for rev in range(2, 42):
        expected_relation[rev] = "undetermined" if rev % 2 == 0 else "earlier_than"
    current = 1
    current_eid = "E2"
    seq = 0
    stop = threading.Event()
    errors = []

    def writer():
        nonlocal current, current_eid, seq
        time.sleep(0.05)
        for _ in range(40):
            if current % 2 == 1:  # 早于 -> 撤回 -> 未定
                service.apply_batch(
                    BatchIn(expected_revision=current, operations=[retract_op(current_eid)])
                )
                current += 1
            else:  # 未定 -> 新增证据 -> 早于
                seq += 1
                new_eid = f"ES-{seq:03d}"
                service.apply_batch(
                    BatchIn(expected_revision=current,
                            operations=[assert_op(new_eid, "L2", "L3", "earlier_than")])
                )
                current_eid = new_eid
                current += 1
        stop.set()

    def reader():
        while not stop.is_set():
            try:
                resp = service.query_relationship("L1", "L3")
            except Exception as exc:  # noqa: BLE001
                errors.append(("reader-exc", repr(exc)))
                return
            rev = resp["revision"]
            if rev not in expected_relation:
                errors.append(("unknown-revision", rev))
                continue
            if resp["relation"] != expected_relation[rev]:
                errors.append(("mixed-version", rev, resp["relation"], expected_relation[rev]))
            bad = [s for s in resp["path_detail"] if s["status"] != "active"]
            if bad:
                errors.append(("retracted-evidence-on-path", rev, bad))
            if resp["relation"] == "undetermined" and resp["evidence_path"]:
                errors.append(("undetermined-with-path", rev, resp["evidence_path"]))

    wt = threading.Thread(target=writer)
    wt.start()
    readers = [threading.Thread(target=reader) for _ in range(6)]
    for t in readers:
        t.start()
    wt.join(timeout=60)
    for t in readers:
        t.join(timeout=10)
    assert not wt.is_alive(), "写线程超时"
    assert not any(t.is_alive() for t in readers), "读线程超时"
    assert not errors, errors[:5]
    assert current == 41, current
    print(f"PASS formal-stress 正式查询 {current - 1} 次翻转期间多读者并发，零混版响应")


def stress_projection():
    reset_state()
    seed_revision_one_same_only()
    created = projections_mod.create_projection(None)
    pid = created["id"]
    # 推演修订 0：L1 与 L3 未定；奇数修订新增 L2<L3（早于）；其后偶数修订撤回（未定）
    expected = {0: "undetermined"}
    for rev in range(1, 41):
        expected[rev] = "earlier_than" if rev % 2 == 1 else "undetermined"
    current = 0
    current_eid = None
    seq = 0
    stop = threading.Event()
    errors = []

    def writer():
        nonlocal current, current_eid, seq
        time.sleep(0.05)
        for _ in range(40):
            if current % 2 == 0:
                seq += 1
                new_eid = f"PES-{seq:03d}"
                projections_mod.apply_projection_batch(
                    pid,
                    ProjectionBatchIn(expected_revision=current,
                                      operations=[assert_op(new_eid, "L2", "L3", "earlier_than")]),
                )
                current_eid = new_eid
                current += 1
            else:
                projections_mod.apply_projection_batch(
                    pid,
                    ProjectionBatchIn(expected_revision=current, operations=[retract_op(current_eid)]),
                )
                current += 1
        stop.set()

    def reader():
        while not stop.is_set():
            try:
                resp = projections_mod.query_projection_relationship(pid, "L1", "L3")
            except Exception as exc:  # noqa: BLE001
                errors.append(("reader-exc", repr(exc)))
                return
            rev = resp["projection_revision"]
            if rev not in expected:
                errors.append(("unknown-revision", rev))
                continue
            if resp["relation"] != expected[rev]:
                errors.append(("mixed-version", rev, resp["relation"], expected[rev]))
            bad = [s for s in resp["path_detail"] if s["status"] != "active"]
            if bad:
                errors.append(("retracted-evidence-on-path", rev, bad))

    wt = threading.Thread(target=writer)
    wt.start()
    readers = [threading.Thread(target=reader) for _ in range(6)]
    for t in readers:
        t.start()
    wt.join(timeout=60)
    for t in readers:
        t.join(timeout=10)
    assert not wt.is_alive(), "写线程超时"
    assert not any(t.is_alive() for t in readers), "读线程超时"
    assert not errors, errors[:5]
    assert current == 40, current
    print(f"PASS projection-stress 推演查询 {current} 次推演修订翻转期间多读者并发，零混版响应")


# ------------------------------------------------------------------ 错误约定
def error_conventions():
    from app.errors import ApiError

    try:
        service.query_relationship("L1", "NOPE")
    except ApiError as exc:
        assert exc.status_code == 400 and exc.code == "INVALID_STRATUM", exc.code
    else:
        raise AssertionError("正式查询未知地层未拒绝")

    try:
        projections_mod.query_projection_relationship(99999, "L1", "L3")
    except ApiError as exc:
        assert exc.status_code == 404 and exc.code == "PROJECTION_NOT_FOUND", exc.code
    else:
        raise AssertionError("未知推演未拒绝")

    pid = make_projection_with_one_added_assertion()
    try:
        projections_mod.query_projection_relationship(pid, "L1", "NOPE")
    except ApiError as exc:
        assert exc.status_code == 400 and exc.code == "INVALID_STRATUM", exc.code
    else:
        raise AssertionError("推演查询未知地层未拒绝")
    print("PASS errors 未知地层 / 未知推演的错误约定保持不变")


SCENARIOS = [
    ("formal-A", lambda: (reset_state(), seed_revision_one(),
                          scenario_formal_old_revision_with_new_conclusion())),
    ("formal-B", lambda: (reset_state(), seed_revision_one(),
                          scenario_formal_path_detail_retracted())),
    ("projection-A", lambda: (reset_state(), seed_revision_one_same_only(),
                              scenario_projection_old_revision_with_new_conclusion())),
    ("projection-B", lambda: (reset_state(), seed_revision_one_same_only(),
                              scenario_projection_path_detail_retracted())),
    ("closed", lambda: (reset_state(), seed_revision_one_same_only(),
                        scenario_closed_projection_still_reads_frozen())),
    ("formal-stress", stress_formal),
    ("projection-stress", stress_projection),
    ("errors", lambda: (reset_state(), seed_revision_one(), error_conventions())),
]


def main():
    srv, admin_uri = start_server()
    # 应用走 TCP 本地回环；pgserver 默认 unix socket，直接复用 socket DSN 即可
    os.environ["DATABASE_URL"] = admin_uri.replace(
        "/postgres?", "/stratigraphy?"
    ).replace("postgresql://postgres:@", "postgresql://postgres@")
    try:
        init_app_pool()
        for name, fn in SCENARIOS:
            GATE.disarm()
            fn()
    finally:
        GATE.disarm()
        try:
            db._pool.closeall()
        except Exception:  # noqa: BLE001
            pass
        srv.cleanup()
    print("ALL E2E RELATIONSHIP-VIEW SCENARIOS PASSED")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
