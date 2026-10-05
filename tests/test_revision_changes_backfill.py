"""启动回填前态修复的端到端回归（需真实 PostgreSQL）。

复现缺陷：已封口修订与待补修订交错时，旧回填把每个待补修订与空库（或上一个
待补修订）比较。构造：

- 修订 1（封口）：E1（甲~乙）、E2（乙<丙）=> 甲 早于 丙，路径 [E1, E2]；
- 修订 2（**待补**）：撤回 E2 => 甲 早于 丙 变为未定；
- 修订 3（封口，在线提交）：新增 E3（甲<丙）=> 甲 早于 丙 恢复。

错误行为：修订 2 被拿空库作前态 -> 旧关系记成 undetermined、新关系却也是
undetermined（E2 已撤），撤回被漏记（零行）。连带：

1. /revision-changes 缺失修订 2 的撤回记录；
2. 封存于修订 1 的成果包在修订 4 复核时，首次偏离被错判为修订 3（恢复），
   实际首次偏离应是修订 2（撤回，结论变为未定）。

修复后：待补修订 2 以紧邻上一正式修订（修订 1）的当时生效断言为前态、本修订
为后态，正确记录 earlier_than -> undetermined（新路径 []）。

另覆盖：
- 零变化修订也封口（无变化行但 sealed 有标记），重复启动不重写；
- 回填与在线提交交错（在线修订先封口、回填随后补洞；回填持锁期间在线提交等待），
  最终台账与逐修订在线重放的结果逐条一致（不重写、不漏记、不重复）。

运行：
    PYTHONPATH=/tmp/pylibs python3 tests/test_revision_changes_backfill.py
"""
from __future__ import annotations

import os
import sys
import threading
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pgserver  # type: ignore
    import psycopg2
    import psycopg2.extensions
except ImportError as exc:  # pragma: no cover
    print(f"SKIP: 缺少真实 PostgreSQL 验证依赖（{exc.name}）")
    sys.exit(0)

from psycopg2.extras import Json  # noqa: E402

from app import db  # noqa: E402
from app.changes import backfill_revision_changes  # noqa: E402

PGDATA = "/tmp/pgdata-revision-backfill"

ALL_TABLES = (
    "evidence_hold_items", "evidence_holds",
    "field_package_corrections", "field_packages", "field_short_bindings",
    "thematic_packages", "snapshot_conclusions", "snapshots",
    "revision_changes_sealed", "revision_changes", "revisions",
    "projection_revisions", "projection_retractions", "projection_assertions",
    "projections", "assertions", "strata",
)


# ------------------------------------------------------------------ 环境
def start_server():
    os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-backfill")
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
    os.chmod(os.environ["XDG_RUNTIME_DIR"], 0o700)
    srv = pgserver.get_server(PGDATA)
    uri = srv.get_uri()
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
    os.environ["DATABASE_URL"] = uri.replace(
        "/postgres?", "/stratigraphy?"
    ).replace("postgresql://postgres:@", "postgresql://postgres@")
    return srv


def init_schema_without_backfill():
    """只建表、不回填（模拟回填逻辑部署前的历史库）。"""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(db.SCHEMA)
    finally:
        conn.close()


def init_app_pool():
    db.DATABASE_URL = os.environ["DATABASE_URL"]
    db.init_pool(retries=10, delay=1.0)


# ------------------------------------------------------------------ 底层造数
def add_strata(cur, ids):
    for s in ids:
        cur.execute(
            "INSERT INTO strata (stratum_id, site) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (s, "甲遗址"),
        )


def add_revision(cur, revision, to_add=(), to_retract=(), batch=None):
    """绕过服务层直接造一个正式修订（断言落库 + 审计 + 计数器推进），不写变化台账。

    用于模拟“部署前历史修订”（没有 revision_changes / sealed 记录）。
    to_add: [(evidence_id, a, b, relation), ...]；to_retract: [evidence_id, ...]
    """
    for eid, a, b, rel in to_add:
        cur.execute(
            "INSERT INTO assertions (evidence_id, stratum_a, stratum_b, relation, status, added_revision) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (eid, a, b, rel, revision),
        )
    for eid in to_retract:
        cur.execute(
            "UPDATE assertions SET status='retracted', retracted_revision=%s, retracted_at=now() "
            "WHERE evidence_id=%s",
            (revision, eid),
        )
    cur.execute("UPDATE revision_counter SET revision=%s WHERE id=TRUE", (revision,))
    cur.execute(
        "INSERT INTO revisions (revision, batch) VALUES (%s, %s)",
        (revision, Json(batch or {"operations": []})),
    )


def seal_only(cur, revision, source="formal_batch"):
    """只写封口标记（模拟该修订已由在线提交处理；其变化行另行写入）。"""
    cur.execute(
        "INSERT INTO revision_changes_sealed (revision, source) VALUES (%s, %s)",
        (revision, source),
    )


def fetch_changes(cur):
    cur.execute(
        "SELECT revision, seq, stratum_a, stratum_b, old_relation, new_relation, "
        "old_path, new_path, source FROM revision_changes ORDER BY revision, seq"
    )
    return [
        {
            "revision": r[0], "seq": r[1], "stratum_a": r[2], "stratum_b": r[3],
            "old_relation": r[4], "new_relation": r[5],
            "old_path": list(r[6]), "new_path": list(r[7]), "source": r[8],
        }
        for r in cur.fetchall()
    ]


def fetch_sealed(cur):
    cur.execute("SELECT revision, source FROM revision_changes_sealed ORDER BY revision")
    return cur.fetchall()


def reset_state():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
            cur.execute("UPDATE revision_counter SET revision = 0 WHERE id = TRUE")


# ------------------------------------------------------------------ 场景 1：交错封口下的撤回漏记
def scenario_interleaved_retraction():
    reset_state()
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            add_strata(cur, ("L1", "L2", "L3"))
            # 修订 1（**已封口**，模拟在线提交）：E1 L1~L2、E2 L2<L3
            add_revision(
                cur, 1,
                to_add=[
                    ("E1", "L1", "L2", "same_period"),
                    ("E2", "L2", "L3", "earlier_than"),
                ],
            )
            # 在线口径重放修订 1 的变化行（未定 -> 确定），并封口
            cur.execute(
                "INSERT INTO revision_changes "
                "(revision, seq, stratum_a, stratum_b, old_relation, new_relation, old_path, new_path, source) "
                "VALUES "
                "(1, 1, 'L1', 'L2', 'undetermined', 'same_period', %s, %s, 'formal_batch'), "
                "(1, 2, 'L1', 'L3', 'undetermined', 'earlier_than', %s, %s, 'formal_batch'), "
                "(1, 3, 'L2', 'L3', 'undetermined', 'earlier_than', %s, %s, 'formal_batch')",
                (Json([]), Json(["E1"]),
                 Json([]), Json(["E1", "E2"]),
                 Json([]), Json(["E2"])),
            )
            seal_only(cur, 1)
            # 修订 2（**待补**）：撤回 E2 —— 甲（L1）早于丙（L3）变未定
            add_revision(cur, 2, to_retract=["E2"])
            # 修订 3（在线封口）：新增 E3 L1<L3 —— 同时在线写入其变化行与封口
            add_revision(cur, 3, to_add=[("E3", "L1", "L3", "earlier_than")])
            # 在线口径重放修订 3 的变化（相对修订 2 当时状态）：未定 -> 早于 [E3]
            cur.execute(
                "INSERT INTO revision_changes "
                "(revision, seq, stratum_a, stratum_b, old_relation, new_relation, old_path, new_path, source) "
                "VALUES (3, 1, 'L1', 'L3', 'undetermined', 'earlier_than', %s, %s, 'formal_batch')",
                (Json([]), Json(["E3"])),
            )
            seal_only(cur, 3)

            backfilled = backfill_revision_changes(cur)
    assert backfilled == 1, backfilled  # 只补唯一未封口的修订 2

    with db.db_conn() as conn:
        with conn.cursor() as cur:
            changes = fetch_changes(cur)
            sealed = fetch_sealed(cur)

    assert [r for r in sealed] == [(1, "formal_batch"), (2, "formal_batch"), (3, "formal_batch")], sealed

    by_rev_pair = {(c["revision"], c["stratum_a"], c["stratum_b"]): c for c in changes}

    # 修订 1 的在线行未被回填重写
    r1_l1l3 = by_rev_pair[(1, "L1", "L3")]
    assert (r1_l1l3["old_relation"], r1_l1l3["new_relation"]) == ("undetermined", "earlier_than"), r1_l1l3
    assert r1_l1l3["old_path"] == [] and r1_l1l3["new_path"] == ["E1", "E2"], r1_l1l3

    # 关键断言：修订 2 必须记录撤回（前态=紧邻上一正式修订 1 的当时生效断言），
    # 而不是被空库吞掉。撤回 E2（L2<L3）影响两对：L2-L3 与（经 E1 同期的）L1-L3。
    r2 = {(c["stratum_a"], c["stratum_b"]): c for c in changes if c["revision"] == 2}
    assert set(r2) == {("L1", "L3"), ("L2", "L3")}, r2
    for key in (("L1", "L3"), ("L2", "L3")):
        c2 = r2[key]
        assert (c2["old_relation"], c2["new_relation"]) == ("earlier_than", "undetermined"), c2
        assert c2["new_path"] == [], c2
    assert r2[("L1", "L3")]["old_path"] == ["E1", "E2"], r2[("L1", "L3")]
    assert r2[("L2", "L3")]["old_path"] == ["E2"], r2[("L2", "L3")]

    # 修订 3 的在线行不被回填重写
    c3 = by_rev_pair[(3, "L1", "L3")]
    assert (c3["old_relation"], c3["new_relation"], c3["new_path"]) == (
        "undetermined", "earlier_than", ["E3"]
    ), c3
    print("PASS interleaved 待补修订 2 以已封口修订 1 的当时生效断言为前态，撤回未被漏记；封口行未被重写")


# ------------------------------------------------------------------ 场景 2：首次偏离不再失真（HTTP 复核）
def scenario_first_deviation_via_http():
    reset_state()
    # 用服务层在线登记地层、造出修订 1 并定版、封成果包（修订 1 的变化由在线路径写）
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.models import AssertOp, BatchIn, PackageIn
    from app.service import apply_batch, create_snapshot
    from app.packages import create_package

    with db.db_conn() as conn:
        with conn.cursor() as cur:
            add_strata(cur, ("L1", "L2", "L3"))

    apply_batch(BatchIn(expected_revision=0, operations=[
        AssertOp(op="assert", evidence_id="E1", stratum_a="L1", stratum_b="L2",
                 relation="same_period"),
        AssertOp(op="assert", evidence_id="E2", stratum_a="L2", stratum_b="L3",
                 relation="earlier_than"),
    ]))
    snap = create_snapshot("定版1")
    pkg, _ = create_package(PackageIn(version=snap["version"], strata=["L1", "L3"]))
    package_id = pkg["id"]

    # 之后把修订 1 的变化记录与封口抹掉（模拟它在部署后才由启动回填补），
    # 修订 2（撤回 E2）、修订 3（E3 恢复）同样无台账 —— 全部留给启动回填。
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE revision_changes, revision_changes_sealed")
    # 再造修订 2、3（在线，但先不写台账：直接改断言与审计）
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            add_revision(cur, 2, to_retract=["E2"])
            add_revision(cur, 3, to_add=[("E3", "L1", "L3", "earlier_than")])
            n = backfill_revision_changes(cur)
            assert n == 3, n

    client = TestClient(main_mod.app)
    resp = client.post(f"/packages/{package_id}/reconcile", json={"expected_revision": 3})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    pair = next(p for p in data["pairs"] if p["stratum_a"] == "L1" and p["stratum_b"] == "L3")
    # 当前（修订 3）关系恢复早于，但直达证据 E3 更短 -> 仅路径改变；曾偏离必须保留
    assert pair["comparison"] == "path_only_changed", pair["comparison"]
    fd = pair["first_deviation"]
    assert fd is not None, data
    assert fd["revision"] == 2, fd            # 旧 bug 会错判为 3
    assert fd["source"] == "formal_batch", fd
    assert fd["previous_relation"] == "earlier_than", fd
    assert fd["previous_evidence_path"] == ["E1", "E2"], fd
    assert fd["relation"] == "undetermined", fd
    assert fd["evidence_path"] == [], fd
    print("PASS reconcile 首次偏离=撤回修订 2（不再被错判为恢复修订 3），曾偏离保留")


# ------------------------------------------------------------------ 场景 3：零变化修订也封口、重复启动幂等
def scenario_zero_change_sealed_and_idempotent():
    reset_state()
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            add_strata(cur, ("L1", "L2"))
            # 修订 1：E1 L1<L2
            add_revision(cur, 1, to_add=[("E1", "L1", "L2", "earlier_than")])
            # 修订 2：撤回一条从未生效过的无关证据不可能；改为新增平行断言 E2 L1<L2
            add_revision(cur, 2, to_add=[("E2", "L1", "L2", "earlier_than")])
            # 此时最优路径为 [E1]（字典序更小），修订 2 零变化
            n1 = backfill_revision_changes(cur)
    assert n1 == 2, n1
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            changes = fetch_changes(cur)
            sealed = fetch_sealed(cur)
            # 修订 1 一行；修订 2 零变化（无行）但已封口
            assert [c["revision"] for c in changes] == [1], changes
            assert sealed == [(1, "formal_batch"), (2, "formal_batch")], sealed

            # 重复启动：不再补任何修订、不重写既有行
            n2 = backfill_revision_changes(cur)
    assert n2 == 0, n2
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            assert fetch_changes(cur) == changes
            assert fetch_sealed(cur) == sealed
    print("PASS zero-change 零变化修订封口无变化行；重复启动回填数为 0 且不重写")


# ------------------------------------------------------------------ 场景 4：回填与在线提交交错
def scenario_interleave_with_online_commit():
    """先历史待补修订 1、2；回填前在线提交先落修订 3 并封口，随后回填补齐 1、2。

    最终台账必须与“每个修订都由在线路径连续提交”的口径逐条一致：
    不重写（3 保持在线原文）、不漏记（1、2 补齐）、不重复（封口唯一）。
    """
    reset_state()
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            add_strata(cur, ("L1", "L2", "L3"))
            add_revision(cur, 1, to_add=[
                ("E1", "L1", "L2", "same_period"),
                ("E2", "L2", "L3", "earlier_than"),
            ])
            add_revision(cur, 2, to_retract=["E2"])

    # 在线提交修订 3（服务层完整路径：计数器行锁 + 变化行 + 封口）
    from app.models import AssertOp, BatchIn
    from app.service import apply_batch

    apply_batch(BatchIn(expected_revision=2, operations=[
        AssertOp(op="assert", evidence_id="E3", stratum_a="L1", stratum_b="L3",
                 relation="earlier_than"),
    ]))

    done = threading.Event()

    def run_backfill():
        with db.db_conn() as conn:
            with conn.cursor() as cur:
                backfill_revision_changes(cur)
        done.set()

    t = threading.Thread(target=run_backfill)
    t.start()
    assert done.wait(timeout=15), "回填线程未完成（可能与在线锁互斥死锁）"
    t.join(timeout=5)
    assert not t.is_alive()

    with db.db_conn() as conn:
        with conn.cursor() as cur:
            changes = fetch_changes(cur)
            sealed = fetch_sealed(cur)

    assert sealed == [(1, "formal_batch"), (2, "formal_batch"), (3, "formal_batch")], sealed
    revs = {}
    for c in changes:
        revs.setdefault(c["revision"], []).append(c)
    # 每个 (revision, pair) 唯一，无重复
    keys = [(c["revision"], c["stratum_a"], c["stratum_b"]) for c in changes]
    assert len(keys) == len(set(keys)), keys

    c2 = next(c for c in revs[2] if (c["stratum_a"], c["stratum_b"]) == ("L1", "L3"))
    assert (c2["old_relation"], c2["new_relation"]) == ("earlier_than", "undetermined"), c2
    assert c2["old_path"] == ["E1", "E2"] and c2["new_path"] == [], c2

    c3 = next(c for c in revs[3] if (c["stratum_a"], c["stratum_b"]) == ("L1", "L3"))
    assert (c3["old_relation"], c3["new_relation"]) == ("undetermined", "earlier_than"), c3
    assert c3["new_path"] == ["E3"], c3

    # 连续状态链一致性：修订 2 的新态 == 修订 3 的旧态
    assert c2["new_relation"] == c3["old_relation"] == "undetermined"

    # 回填后再做在线提交（修订 4）仍正常写台账与封口
    apply_batch(BatchIn(expected_revision=3, operations=[
        AssertOp(op="assert", evidence_id="E4", stratum_a="L2", stratum_b="L3",
                 relation="earlier_than"),
    ]))
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            sealed2 = fetch_sealed(cur)
    assert [r for r in sealed2] == [(i, "formal_batch") for i in (1, 2, 3, 4)], sealed2
    print("PASS interleave 在线修订 3 先封口、回填补齐 1/2，不重写/漏记/重复；后续在线修订 4 正常")


# ------------------------------------------------------------------ 场景 5：回填持锁期间在线提交等待
def scenario_backfill_blocks_then_online_commit():
    reset_state()
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            add_strata(cur, ("L1", "L2", "L3"))
            add_revision(cur, 1, to_add=[
                ("E1", "L1", "L2", "same_period"),
                ("E2", "L2", "L3", "earlier_than"),
            ])
            add_revision(cur, 2, to_retract=["E2"])  # 撤回：待补修订

    gate = threading.Event()
    proceed = threading.Event()

    def run_backfill():
        with db.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
                gate.set()
                proceed.wait(timeout=15)
                backfill_revision_changes(cur)  # 内部再次加锁（同事务，可重入）

    t = threading.Thread(target=run_backfill)
    t.start()
    assert gate.wait(timeout=10), "回填未拿到计数器锁"

    from app.models import AssertOp, BatchIn
    from app.service import apply_batch

    box = {}

    def online_commit():
        try:
            # 修订 3：新增 E3（L1<L3），不与现存断言冲突
            box["resp"] = apply_batch(BatchIn(expected_revision=2, operations=[
                AssertOp(op="assert", evidence_id="E3", stratum_a="L1", stratum_b="L3",
                         relation="earlier_than"),
            ]))
        except Exception as exc:  # noqa: BLE001
            box["exc"] = exc

    wt = threading.Thread(target=online_commit)
    wt.start()
    # 在线提交应被行锁阻塞（给它一点时间证明它没先完成）
    import time as _time
    _time.sleep(1.0)
    assert "resp" not in box and "exc" not in box, box
    proceed.set()
    t.join(timeout=15)
    wt.join(timeout=15)
    assert not t.is_alive() and not wt.is_alive()
    assert "exc" not in box, box.get("exc")
    assert box["resp"]["revision"] == 3, box

    with db.db_conn() as conn:
        with conn.cursor() as cur:
            sealed = fetch_sealed(cur)
            changes = fetch_changes(cur)
    assert [r for r in sealed] == [(1, "formal_batch"), (2, "formal_batch"), (3, "formal_batch")], sealed
    # 修订 3 的在线行存在且仅有一份
    r3 = [c for c in changes if c["revision"] == 3]
    assert r3 and all(c["source"] == "formal_batch" for c in r3), r3
    print("PASS locking 回填持计数器锁期间在线提交等待，锁释放后串行成功，台账不重/不漏")


def main():
    srv = start_server()
    try:
        init_schema_without_backfill()
        init_app_pool()
        scenario_interleaved_retraction()
        scenario_first_deviation_via_http()
        scenario_zero_change_sealed_and_idempotent()
        scenario_interleave_with_online_commit()
        scenario_backfill_blocks_then_online_commit()
    finally:
        try:
            db._pool.closeall()
        except Exception:  # noqa: BLE001
            pass
        srv.cleanup()
    print("ALL REVISION-CHANGES BACKFILL SCENARIOS PASSED")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
