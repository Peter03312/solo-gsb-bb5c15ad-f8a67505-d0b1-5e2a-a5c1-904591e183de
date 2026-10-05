"""定版成员时点一致性端到端回归（真实 PostgreSQL，经 pgserver 启动本地实例）。

修复的缺陷：定版（snapshot）只封存断言与已确定关系，不含成员清单；旧版关系
查询与成果包封存却按**当前地层表**校验成员。于是“先定版、后登记新地层”后，
用旧版查询或封包时，新地层被误当作旧版的“未定”成员——与版内真实未定混淆。

修复口径（详见 app/service.py 成员门）：
- 新定版冻结当时全部已登记地层（membership_status=frozen）；
- 旧版关系查询 GET /snapshots/{v}/relationship 与成果包封存 POST /packages
  只接受该版成员：定版后才登记的地层 400 STRATUM_OUTSIDE_VERSION，
  不落入“无冻结结论行=未定”；版内无结论行才是该版真实未定；
- 对缺少成员清单的既有定版，启动时按该版冻结断言端点 + 冻结结论行重建
  **可证实**成员（membership_status=reconstructed）；无法证实归属的地层
  返回 409 SNAPSHOT_MEMBERSHIP_UNDECIDABLE（附 provable_members），不伪造关系；
- 已封存成果包内容原样保留、可原样取回（含修复前封入的版外“未定”对），
  同集合重复封存仍幂等返回 already_exists=true；版内结论与证据路径不变。

运行：python3 tests/test_snapshot_membership_point_in_time.py
依赖：pgserver（pip 包，非 requirements 依赖；缺失则跳过）。
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import time

try:
    import pgserver  # type: ignore
except ImportError:
    print("SKIP: 缺少真实 PostgreSQL 验证依赖（pgserver）")
    sys.exit(0)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from app import db  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(base: str) -> None:
    for _ in range(60):
        try:
            if httpx.get(f"{base}/health", timeout=1.0).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.5)
    raise RuntimeError("服务未就绪")


def _err(resp: httpx.Response):
    return resp.status_code, resp.json()["error"]["code"], resp.json()["error"]["details"]


def main() -> None:
    tmp = tempfile.mkdtemp(prefix="pg-snap-members-")
    srv = pgserver.get_server(tmp)
    dsn = srv.get_uri()  # unix socket DSN；应用通过 TCP 本地回环连接
    port = _free_port()
    db.DATABASE_URL = dsn
    db._pool = None
    db.init_pool()

    import uvicorn
    from app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    import threading

    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    _wait_ready(base)

    try:
        c = httpx.Client(base_url=base, timeout=10)

        def register(sid: str) -> None:
            r = c.post("/strata", json={"stratum_id": sid, "site": "甲遗址"})
            assert r.status_code in (201, 409), r.text

        def batch(rev: int, *ops) -> None:
            r = c.post("/batches", json={"expected_revision": rev, "operations": list(ops)})
            assert r.status_code == 200, r.text

        # ---------------------------------------------------------- 场景搭建
        register("L1")
        register("L2")
        register("L3")
        batch(
            0,
            {"op": "assert", "evidence_id": "EV-1", "stratum_a": "L1",
             "stratum_b": "L2", "relation": "earlier_than"},
            {"op": "assert", "evidence_id": "EV-2", "stratum_a": "L2",
             "stratum_b": "L3", "relation": "same_period"},
        )

        # 既有定版（模拟升级前缺成员清单）：直接按旧表结构写入，再起服务重建。
        # 这里先正常定版得到 v1，再用底层连接剥除成员清单模拟“既有定版”。
        r = c.post("/snapshots", json={"label": "旧定版"})
        assert r.status_code == 201 and r.json()["membership_status"] == "frozen"
        legacy_version = r.json()["version"]

        # ---- 新定版冻结成员时点 ----
        r = c.post("/snapshots", json={"label": "新定版"})
        assert r.status_code == 201
        frozen_version = r.json()["version"]
        assert r.json()["members"] == 3 and r.json()["membership_status"] == "frozen"
        detail = c.get(f"/snapshots/{frozen_version}").json()
        assert detail["strata"] == ["L1", "L2", "L3"] and detail["membership_status"] == "frozen"

        # ---- 定版后登记新地层 L4、L5（L4 与版内无关；L5 随后与版内产生现时关系）
        register("L4")
        batch(
            1,
            {"op": "assert", "evidence_id": "EV-3", "stratum_a": "L1",
             "stratum_b": "L4", "relation": "earlier_than"},
        )
        # 现时关系确实存在：对照 GET /relationship 与旧版查询必须不同口径
        now_resp = c.get("/relationship", params={"a": "L1", "b": "L4"}).json()
        assert now_resp["relation"] == "earlier_than" and now_resp["revision"] == 2

        # ---- frozen 旧版查询：版内真实未定 vs 版外拒绝 ----
        inside = c.get(f"/snapshots/{frozen_version}/relationship",
                       params={"a": "L1", "b": "L3"}).json()
        assert inside["relation"] == "earlier_than", inside  # EV-1 + EV-2 同期传递
        und = c.get(f"/snapshots/{frozen_version}/relationship",
                    params={"a": "L3", "b": "L1"}).json()
        assert und["relation"] == "later_than"

        status, code, details = _err(
            c.get(f"/snapshots/{frozen_version}/relationship",
                  params={"a": "L1", "b": "L4"})
        )
        assert (status, code) == (400, "STRATUM_OUTSIDE_VERSION"), (status, code)
        assert details["reason"] == "registered_after_version" and details["stratum_id"] == "L4"

        # 当前未登记 / 未知定版错误结构不变
        status, code, _ = _err(c.get(f"/snapshots/{frozen_version}/relationship",
                                     params={"a": "L1", "b": "ZZ"}))
        assert (status, code) == (400, "INVALID_STRATUM")
        status, code, _ = _err(c.get("/snapshots/9999/relationship",
                                     params={"a": "L1", "b": "L2"}))
        assert (status, code) == (404, "SNAPSHOT_NOT_FOUND")

        # 同层版外也不自证为成员
        status, code, _ = _err(c.get(f"/snapshots/{frozen_version}/relationship",
                                     params={"a": "L4", "b": "L4"}))
        assert (status, code) == (400, "STRATUM_OUTSIDE_VERSION")

        # ---- frozen 旧版封包：含版外拒绝；版内真实未定可封 ----
        status, code, details = _err(
            c.post("/packages", json={"version": frozen_version,
                                      "strata": ["L1", "L4"]})
        )
        assert (status, code) == (400, "STRATUM_OUTSIDE_VERSION"), (status, code)

        r = c.post("/packages", json={"version": frozen_version,
                                      "strata": ["L2", "L1"]})
        assert r.status_code == 201 and r.json()["version"] == frozen_version
        assert [(p["stratum_a"], p["stratum_b"], p["relation"]) for p in r.json()["pairs"]] == [
            ("L1", "L2", "earlier_than")
        ]
        # 幂等：顺序无关重复封存 -> 200 already_exists，内容指纹不变
        r2 = c.post("/packages", json={"version": frozen_version,
                                       "strata": ["L1", "L2"]})
        assert r2.status_code == 200 and r2.json()["content_hash"] == r.json()["content_hash"]

        # 旧版（升级前口径）封存一个成果包：L1<L3 经 EV-1 与 EV-2 同期传递成立。
        # 该包在成员门升级前已封存，重建后必须原样可取、内容指纹不变。
        r = c.post("/packages", json={"version": legacy_version, "strata": ["L3", "L1"]})
        assert r.status_code == 201, r.text
        legacy_pkg = r.json()
        assert [(p["stratum_a"], p["stratum_b"], p["relation"]) for p in legacy_pkg["pairs"]] == [
            ("L1", "L3", "earlier_than")
        ]

        # ---- 既有定版：剥除成员清单，模拟“升级前定版”，重启触发重建 ----
        server.should_exit = True
        t.join(timeout=5)
        with db.db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE snapshots SET strata = NULL, membership_status = 'reconstructed' "
                "WHERE version = %s",
                (legacy_version,),
            )

        db._pool = None

        # 重新起服务：启动回填把 legacy 版成员重建为可证实集合 {L1,L2,L3}
        db.init_pool()
        config2 = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config2)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        _wait_ready(base)

        # 重建后：可证实成员（断言端点 L1/L2/L3）正常查询；后登记的 L4 不可判定
        legacy_detail = c.get(f"/snapshots/{legacy_version}").json()
        assert legacy_detail["membership_status"] == "reconstructed"
        assert set(legacy_detail["strata"]) == {"L1", "L2", "L3"}, legacy_detail["strata"]

        ok = c.get(f"/snapshots/{legacy_version}/relationship",
                   params={"a": "L1", "b": "L2"}).json()
        assert ok["relation"] == "earlier_than"
        status, code, details = _err(
            c.get(f"/snapshots/{legacy_version}/relationship",
                  params={"a": "L1", "b": "L4"})
        )
        assert (status, code) == (409, "SNAPSHOT_MEMBERSHIP_UNDECIDABLE"), (status, code)
        assert details["provable_members"] == ["L1", "L2", "L3"]

        # reconstructed 版新封包：可证实成员可封；含不可判定成员拒绝
        r = c.post("/packages", json={"version": legacy_version,
                                      "strata": ["L3", "L2"]})
        assert r.status_code == 201, r.text
        status, code, _ = _err(
            c.post("/packages", json={"version": legacy_version,
                                      "strata": ["L1", "L4"]})
        )
        assert (status, code) == (409, "SNAPSHOT_MEMBERSHIP_UNDECIDABLE")

        # 升级前已封存的成果包原样可取（pairs/指纹不变），重复请求仍幂等返回它
        kept = c.get(f"/packages/{legacy_pkg['id']}").json()
        assert kept["content_hash"] == legacy_pkg["content_hash"]
        assert kept["pairs"] == legacy_pkg["pairs"]
        again_same = c.post("/packages", json={"version": legacy_version,
                                               "strata": ["L1", "L3"]})
        assert again_same.status_code == 200
        assert again_same.json()["content_hash"] == legacy_pkg["content_hash"]

        # ---- 回填幂等：再次重启不重复处理、不改变清单与既有内容 ----
        server.should_exit = True
        t.join(timeout=5)
        db._pool = None
        db.init_pool()
        config3 = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        server = uvicorn.Server(config3)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        _wait_ready(base)
        again = c.get(f"/snapshots/{legacy_version}").json()
        assert again["strata"] == ["L1", "L2", "L3"] and again["membership_status"] == "reconstructed"
        # 新定版始终是 frozen，重建过程不触碰
        assert c.get(f"/snapshots/{frozen_version}").json()["membership_status"] == "frozen"

        # 列表接口回带成员数与清单状态
        listed = {s["version"]: s for s in c.get("/snapshots").json()}
        assert listed[legacy_version]["membership_status"] == "reconstructed"
        assert listed[legacy_version]["members"] == 3
        assert listed[frozen_version]["membership_status"] == "frozen"

        c.close()
        print("PASS frozen 旧版：版内关系不变，版外 STRATUM_OUTSIDE_VERSION，真实未定仍为未定")
        print("PASS frozen 旧版封包：版外拒绝、版内可封、重复幂等、内容指纹不变")
        print("PASS reconstructed 既有定版：断言端点重建可证实成员，不可判定显式 409")
        print("PASS 回填幂等、frozen 版不被触碰、列表成员数/状态正确")
    finally:
        server.should_exit = True
        srv.cleanup()


if __name__ == "__main__":
    main()
