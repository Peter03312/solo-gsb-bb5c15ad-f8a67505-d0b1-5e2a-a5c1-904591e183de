"""业务逻辑：批次应用、关系查询、定版快照。"""
from typing import Any, Dict, List, Optional, Set, Tuple

from psycopg2.extras import Json

from .db import db_conn
from .changes import insert_revision_changes, relation_diffs
from .derivation import Assertion, Derivation, RELATION_LABELS
from .errors import ApiError
from .holds import assert_evidence_not_held
from .models import AssertOp, BatchIn, RetractOp


# ------------------------------------------------------------------ 工具
def _current_revision(cur) -> int:
    cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
    return cur.fetchone()[0]


def _load_active(cur) -> List[Assertion]:
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation "
        "FROM assertions WHERE status = 'active' ORDER BY evidence_id"
    )
    return [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]


def _load_strata(cur) -> List[str]:
    cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
    return [r[0] for r in cur.fetchall()]


def _ensure_strata(cur, *stratum_ids: str) -> None:
    for s in stratum_ids:
        cur.execute("SELECT 1 FROM strata WHERE stratum_id = %s", (s,))
        if cur.fetchone() is None:
            raise ApiError(400, "INVALID_STRATUM", f"地层未登记: {s}", {"stratum_id": s})


def _path_details(cur, path: Optional[List[str]]) -> List[Dict[str, Any]]:
    """把证据编号序列展开为可读的逐步推导链。"""
    if not path:
        return []
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation, status "
        "FROM assertions WHERE evidence_id = ANY(%s)",
        (list(path),),
    )
    rows = {r[0]: r for r in cur.fetchall()}
    return [
        {
            "evidence_id": eid,
            "stratum_a": rows[eid][1],
            "stratum_b": rows[eid][2],
            "relation": rows[eid][3],
            "status": rows[eid][4],
        }
        for eid in path
        if eid in rows
    ]


def _relation_payload(relation: str, path: Optional[List[str]], cur) -> Dict[str, Any]:
    return {
        "relation": relation,
        "relation_label": RELATION_LABELS[relation],
        "evidence_path": path,
        "path_detail": _path_details(cur, path),
    }


def _snapshot_path_details(
    frozen_by_id: Dict[str, Dict[str, Any]], path: Optional[List[str]]
) -> List[Dict[str, Any]]:
    """按定版冻结的断言副本把证据编号序列展开为逐步明细。

    明细只取封存时的断言（与专题成果包同一来源 snapshots.assertions），不读在线
    断言表：封存时生效的断言 status 恒为 active，后续正式批次 / 野外包勘误 /
    推演并入撤回证据都不改变历史定版的逐步明细。
    """
    if not path:
        return []
    detail: List[Dict[str, Any]] = []
    for eid in path:
        frozen = frozen_by_id.get(eid)
        if frozen is None:
            # 理论上不会发生：定版结论路径必由其冻结断言构成
            continue
        detail.append(
            {
                "evidence_id": frozen["evidence_id"],
                "stratum_a": frozen["stratum_a"],
                "stratum_b": frozen["stratum_b"],
                "relation": frozen["relation"],
                "status": "active",
            }
        )
    return detail


# ------------------------------------------------------------------ 批次
def apply_batch(batch: BatchIn) -> Dict[str, Any]:
    """应用一批断言变更。

    - 携带修订号与当前不一致 -> STALE_REVISION，整批拒绝；
    - 任一操作非法（无效地层/重复证据编号/撤回不存在）-> 整批拒绝；
    - 对整批应用后的最终关系核验，发现组内早于或早于闭环 -> 整批拒绝并给出冲突证据路径；
    - 全部通过 -> 同一事务内落库并原子递增修订号。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            current = cur.fetchone()[0]
            if batch.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {batch.expected_revision}，当前为 {current}",
                    {"current_revision": current},
                )
            new_revision = current + 1

            # 变化追踪旧侧：本批次应用前的当时生效断言集（后来撤回的证据在旧侧仍生效）
            before_assertions = _load_active(cur)

            # 临时保全拦截：本批撤回目标中任一证据仍被生效保全覆盖则整笔拒绝
            # （在任何写入之前；多份保全覆盖同一证据时须全部解除才可撤回）。
            retract_targets = [
                op.evidence_id for op in batch.operations if isinstance(op, RetractOp)
            ]
            assert_evidence_not_held(
                cur,
                retract_targets,
                code="EVIDENCE_HELD",
                message="批次撤回目标中存在受临时保全覆盖的证据，整批拒绝，原状态不变",
                extra={"expected_revision": current},
            )

            for op in batch.operations:
                if isinstance(op, AssertOp):
                    _ensure_strata(cur, op.stratum_a, op.stratum_b)
                    cur.execute("SELECT 1 FROM assertions WHERE evidence_id = %s", (op.evidence_id,))
                    if cur.fetchone() is not None:
                        raise ApiError(
                            409,
                            "DUPLICATE_EVIDENCE",
                            f"证据编号已存在（含已撤回）: {op.evidence_id}",
                            {"evidence_id": op.evidence_id},
                        )
                    cur.execute(
                        "INSERT INTO assertions (evidence_id, stratum_a, stratum_b, relation, status, added_revision) "
                        "VALUES (%s, %s, %s, %s, 'active', %s)",
                        (op.evidence_id, op.stratum_a, op.stratum_b, op.relation, new_revision),
                    )
                elif isinstance(op, RetractOp):
                    cur.execute(
                        "SELECT status FROM assertions WHERE evidence_id = %s FOR UPDATE",
                        (op.evidence_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise ApiError(
                            409,
                            "ASSERTION_NOT_FOUND",
                            f"撤回失败：证据编号不存在: {op.evidence_id}",
                            {"evidence_id": op.evidence_id},
                        )
                    if row[0] != "active":
                        raise ApiError(
                            409,
                            "ASSERTION_NOT_FOUND",
                            f"撤回失败：证据 {op.evidence_id} 已处于撤回状态",
                            {"evidence_id": op.evidence_id},
                        )
                    cur.execute(
                        "UPDATE assertions SET status = 'retracted', retracted_revision = %s, retracted_at = now() "
                        "WHERE evidence_id = %s",
                        (new_revision, op.evidence_id),
                    )

            # 对整批应用后的最终关系核验
            after_assertions = _load_active(cur)
            derivation = Derivation(after_assertions)
            conflicts = derivation.validate()
            if conflicts:
                raise ApiError(
                    409,
                    "CONFLICT",
                    "批次与现有断言矛盾，整批拒绝，原状态不变",
                    {"conflicts": conflicts},
                )

            # 关系变化追踪：按提交前/后的当时状态逐对比较，与修订号同事务原子落库
            diffs = relation_diffs(_load_strata(cur), before_assertions, after_assertions)

            cur.execute("UPDATE revision_counter SET revision = %s WHERE id = TRUE", (new_revision,))
            cur.execute(
                "INSERT INTO revisions (revision, batch) VALUES (%s, %s)",
                (new_revision, Json(batch.model_dump())),
            )
            insert_revision_changes(cur, new_revision, diffs, "formal_batch")
            return {"revision": new_revision, "applied": len(batch.operations), "relation_changes": len(diffs)}


# ------------------------------------------------------------------ 查询
def query_relationship(a: str, b: str) -> Dict[str, Any]:
    """正式关系查询：修订号、关系结论、最优证据路径与逐步明细取自同一完整视图。

    全程在 REPEATABLE READ READ ONLY 只读快照事务内完成：并发正式批次（含野外整包
    汇入 / 勘误、推演并入）若在各条读取语句之间提交，本查询要么整体取到提交前状态、
    要么整体取到提交后状态，绝不把旧修订号与新结论混在一次响应里，路径中的证据也不
    会在逐步明细中显示为已撤回。不写库、不递增修订号，不改变既有请求与返回字段。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            # 只读快照事务：修订号、生效断言集与逐步明细取自同一事务快照；
            # SET TRANSACTION 仅对当前事务生效，连接归还池后下一事务恢复默认模式。
            cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")
            _ensure_strata(cur, a, b)
            revision = _current_revision(cur)
            derivation = Derivation(_load_active(cur))
            relation, path = derivation.relation(a, b)
            payload = _relation_payload(relation, path, cur)
            payload.update({"stratum_a": a, "stratum_b": b, "revision": revision})
            return payload


# ------------------------------------------------------------------ 定版成员
def _snapshot_membership(cur, version: int) -> Tuple[str, Set[str]]:
    """读取定版的成员清单与清单状态（frozen 完整冻结 / reconstructed 历史重建）。

    定版不存在抛 SNAPSHOT_NOT_FOUND。
    """
    cur.execute(
        "SELECT membership_status, strata FROM snapshots WHERE version = %s",
        (version,),
    )
    row = cur.fetchone()
    if row is None:
        raise ApiError(404, "SNAPSHOT_NOT_FOUND", f"版本不存在: {version}", {"version": version})
    return row[0], set(row[1] or [])


def _assert_snapshot_members(cur, version: int, *stratum_ids: str) -> None:
    """旧版关系查询 / 成果包封存的统一成员门：只接受该版成员，版外明确拒绝。

    与“版内真实未定”严格区分：
    - 地层当前未登记：400 INVALID_STRATUM（与既有错误结构兼容）；
    - frozen 定版（新口径冻结了当时全部已登记地层）：不在清单即定版之后才登记，
      返回 400 STRATUM_OUTSIDE_VERSION，details 注明 registered_after_version；
    - reconstructed 既有定版（无原始清单，只重建出可证实成员）：
      清单内 → 可证实成员，放行（无结论行即版内真实未定）；
      清单外但当前已登记 → 历史记录不足以证实其在定版时点是否已登记，
      返回 409 SNAPSHOT_MEMBERSHIP_UNDECIDABLE，明确说明不可判定，绝不把它
      当成旧版的“未定”成员，也不伪造任何关系。
    """
    status, members = _snapshot_membership(cur, version)
    for s in stratum_ids:
        if s in members:
            continue
        cur.execute("SELECT 1 FROM strata WHERE stratum_id = %s", (s,))
        if cur.fetchone() is None:
            raise ApiError(
                400, "INVALID_STRATUM", f"地层未登记: {s}", {"stratum_id": s, "version": version}
            )
        if status == "frozen":
            raise ApiError(
                400,
                "STRATUM_OUTSIDE_VERSION",
                f"地层 {s} 在定版 {version} 之后才登记，不属于该版成员，不能按旧版查询或封包",
                {
                    "version": version,
                    "stratum_id": s,
                    "membership_status": status,
                    "reason": "registered_after_version",
                },
            )
        raise ApiError(
            409,
            "SNAPSHOT_MEMBERSHIP_UNDECIDABLE",
            (
                f"定版 {version} 为缺少成员清单的既有定版：历史记录只能证实部分成员，"
                f"无法证实地层 {s} 在该定版时点是否已登记，成员归属不可判定；"
                "已证实版内成员的版内关系（含真实未定）照常可查，但不把不可判定地层"
                "当作旧版成员给出结论或封入成果包"
            ),
            {
                "version": version,
                "stratum_id": s,
                "membership_status": status,
                "provable_members": sorted(members),
            },
        )


def backfill_snapshot_membership(cur) -> int:
    """启动时为缺少成员清单的既有定版（迁移后 strata 为 NULL、
    membership_status=reconstructed）按历史记录重建**可证实**成员清单。

    可证实成员只取自该定版自己封存的、永不变更的历史记录（不看当前在线表）：
    snapshots.assertions 冻结断言的全部端点 + snapshot_conclusions 冻结结论行的
    全部端点。这两类记录在 create_snapshot 中只可能引用定版时点已登记地层，因此
    其中出现的地层必为该版成员；定版时点已登记但没有任何断言 / 结论留痕的孤立
    地层无法由历史记录证实，保持清单外——成员门对其返回不可判定而非伪造关系。
    新定版 strata 已完整冻结（membership_status=frozen），本回填不触碰。
    """
    cur.execute("SELECT version, assertions FROM snapshots WHERE strata IS NULL ORDER BY version")
    rows = cur.fetchall()
    backfilled = 0
    for version, frozen_assertions in rows:
        members: Set[str] = set()
        for a in frozen_assertions or []:
            members.add(a["stratum_a"])
            members.add(a["stratum_b"])
        cur.execute(
            "SELECT DISTINCT endpoint FROM snapshot_conclusions "
            "CROSS JOIN LATERAL (VALUES (stratum_a), (stratum_b)) AS t(endpoint) "
            "WHERE version = %s",
            (version,),
        )
        members.update(r[0] for r in cur.fetchall())
        cur.execute(
            "UPDATE snapshots SET strata = %s WHERE version = %s AND strata IS NULL",
            (Json(sorted(members)), version),
        )
        backfilled += 1
    return backfilled


# ------------------------------------------------------------------ 定版
def create_snapshot(label: Optional[str]) -> Dict[str, Any]:
    """定版：把当前修订号下全部可确定的关系结论固化为不可变快照。

    成员时点一致性：定版同时冻结**当时全部已登记地层**清单（snapshots.strata，
    membership_status=frozen）。旧版关系查询与成果包封存只接受该版成员；定版之后
    才登记的地层不属于该版，按版外明确拒绝（STRATUM_OUTSIDE_VERSION），不会被
    误当作旧版的“未定”成员。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            # 锁定修订计数器，保证快照与某一修订严格对应
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            revision = cur.fetchone()[0]
            cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
            strata = [r[0] for r in cur.fetchall()]
            active = _load_active(cur)
            derivation = Derivation(active)

            cur.execute(
                "INSERT INTO snapshots (revision, label, assertions, strata, membership_status) "
                "VALUES (%s, %s, %s, %s, 'frozen') RETURNING version",
                (
                    revision,
                    label,
                    Json(
                        [
                            {"evidence_id": x.evidence_id, "stratum_a": x.a, "stratum_b": x.b, "relation": x.relation}
                            for x in active
                        ]
                    ),
                    Json(strata),
                ),
            )
            version = cur.fetchone()[0]

            determined = 0
            for x in strata:
                for y in strata:
                    if x == y:
                        continue
                    relation, path = derivation.relation(x, y)
                    if relation == "undetermined":
                        continue
                    cur.execute(
                        "INSERT INTO snapshot_conclusions (version, stratum_a, stratum_b, relation, path) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (version, x, y, relation, Json(path)),
                    )
                    determined += 1
            return {
                "version": version,
                "revision": revision,
                "conclusions": determined,
                "members": len(strata),
                "membership_status": "frozen",
            }


def get_snapshot(version: int) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version, revision, label, assertions, strata, membership_status, created_at "
                "FROM snapshots WHERE version = %s",
                (version,),
            )
            row = cur.fetchone()
            if row is None:
                raise ApiError(404, "SNAPSHOT_NOT_FOUND", f"版本不存在: {version}", {"version": version})
            return {
                "version": row[0],
                "revision": row[1],
                "label": row[2],
                "assertions": row[3],
                "strata": list(row[4] or []),
                "membership_status": row[5],
                "created_at": row[6].isoformat(),
            }


def query_snapshot_relationship(version: int, a: str, b: str) -> Dict[str, Any]:
    """查询历史版本结论。快照不可变，后续撤回不影响旧版结论与逐步证据明细。

    成员时点一致性：只接受该定版成员，版外地层明确拒绝，不与版内真实未定混淆。
    - frozen 定版：定版之后才登记的地层 → 400 STRATUM_OUTSIDE_VERSION；
    - reconstructed 既有定版：无法由历史记录证实成员归属的地层
      → 409 SNAPSHOT_MEMBERSHIP_UNDECIDABLE（明确不可判定，不伪造关系）；
    - 当前未登记 → 400 INVALID_STRATUM；同层恒 same_period。
    版内成员但无冻结结论行才是该版的真实“未定”。

    关系结论与最优证据路径取自定版冻结结论表 snapshot_conclusions，逐步明细
    按定版冻结的断言副本（snapshots.assertions，与专题成果包同一来源）逐步展开，
    不读在线断言表：后续正式批次、野外包勘误或推演并入撤回证据后，历史定版的
    结论、路径与每步明细（含 status）保持封存时口径不变；当前正式关系查询
    （GET /relationship）仍显示现时状态。只读，不写断言或修订。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT revision, assertions FROM snapshots WHERE version = %s", (version,))
            row = cur.fetchone()
            if row is None:
                raise ApiError(404, "SNAPSHOT_NOT_FOUND", f"版本不存在: {version}", {"version": version})
            snapshot_revision = row[0]
            frozen_by_id = {x["evidence_id"]: x for x in row[1]}
            # 成员门：版外（或不可判定）地层在此明确拒绝，不会落入下方“无结论行=未定”
            _assert_snapshot_members(cur, version, a, b)

            if a == b:
                relation, path = "same_period", []
            else:
                cur.execute(
                    "SELECT relation, path FROM snapshot_conclusions "
                    "WHERE version = %s AND stratum_a = %s AND stratum_b = %s",
                    (version, a, b),
                )
                row = cur.fetchone()
                if row is None:
                    relation, path = "undetermined", None
                else:
                    relation, path = row[0], list(row[1])

            payload = {
                "relation": relation,
                "relation_label": RELATION_LABELS[relation],
                "evidence_path": path,
                "path_detail": _snapshot_path_details(frozen_by_id, path),
            }
            payload.update(
                {
                    "stratum_a": a,
                    "stratum_b": b,
                    "version": version,
                    "snapshot_revision": snapshot_revision,
                }
            )
            return payload
