"""争议证据临时保全：交接复核期间冻结撤回入口。

- 建立（`create_hold`）：携带当前正式修订号、一组不重复且**当前均生效**的证据编号
  与事由；成功返回保全记录编号与**仅在创建时显示一次**的解除凭据（明文凭据不落库，
  库内只存 sha256 哈希，任何查询都不回传凭据）。
- 查询：可列保全、看单份保全状态与每条证据当前的在保全来源（含其他保全覆盖），
  也可按证据编号反查生效保全；查询不泄露解除凭据。
- 解除（`release_hold`）：凭对应凭据解除指定保全；错误凭据、保全不存在 / 已解除、
  修订号过期均明确拒绝。同一证据被多份保全覆盖时，须全部解除后才可撤回。
- 撤回拦截（`assert_evidence_not_held`）：普通正式批次、野外包勘误与推演并入中
  凡将受保全覆盖的生效证据撤回到正式库的操作整笔拒绝，返回命中的证据与保全记录；
  正式修订、台账与推演状态均不变。推演内试验撤回不经正式库，不受限制。
  提交前预检（只读）沿用同一拦截：直接撤回受保全证据按正式批次的
  409 EVIDENCE_HELD 拒绝，并以 `active_held_evidence_ids` 把受保全证据排除在
  额外撤回建议之外。

并发裁决：保全建立 / 解除与正式写入一样，事务内先锁修订计数器行
（`revision_counter ... FOR UPDATE`），全部操作按取得该锁的同一全序裁决；
保全操作本身只锁计数器、不更新其值——不递增正式修订，也不改变既有断言与快照。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .db import db_conn
from .errors import ApiError
from .models import HoldCreateIn, HoldReleaseIn

# ------------------------------------------------------------------ 凭据
def _hash_credential(credential: str) -> str:
    """解除凭据只以 sha256 哈希形式落库；明文仅在创建响应中出现一次。"""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def _new_credential() -> str:
    """256 位 URL 安全随机凭据（只此一次随创建响应返回）。"""
    return secrets.token_urlsafe(32)


# ------------------------------------------------------------------ 工具
def _current_revision(cur) -> int:
    cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
    return cur.fetchone()[0]


def _hold_summary(row) -> Dict[str, Any]:
    """生效保全的对外留痕（不含凭据）。"""
    return {
        "id": row[0],
        "revision": row[1],
        "reason": row[2],
        "created_at": row[3].isoformat(),
    }


def _active_holds_map(cur, evidence_ids: Sequence[str]) -> Dict[str, List[Dict[str, Any]]]:
    """一批证据当前的生效保全来源：evidence_id -> 保全摘要列表（按保全编号排序）。"""
    ids = sorted(set(evidence_ids))
    if not ids:
        return {}
    cur.execute(
        "SELECT i.evidence_id, h.id, h.revision, h.reason, h.created_at "
        "FROM evidence_hold_items i "
        "JOIN evidence_holds h ON h.id = i.hold_id "
        "WHERE i.evidence_id = ANY(%s) AND h.status = 'active' "
        "ORDER BY i.evidence_id, h.id",
        (ids,),
    )
    result: Dict[str, List[Dict[str, Any]]] = {}
    for evidence_id, *hold_row in cur.fetchall():
        result.setdefault(evidence_id, []).append(_hold_summary(hold_row))
    return result


def active_held_evidence_ids(cur, evidence_ids: Iterable[str]) -> set:
    """给定证据编号中当前仍被生效保全覆盖的子集（在调用方事务视图内读取）。

    只读路径（如提交前预检）用它把受保全证据排除在“建议撤回”候选之外；
    与 _active_holds_map 同一口径，只取编号、不展开保全明细。
    """
    ids = sorted(set(e for e in evidence_ids if e))
    if not ids:
        return set()
    cur.execute(
        "SELECT DISTINCT i.evidence_id "
        "FROM evidence_hold_items i "
        "JOIN evidence_holds h ON h.id = i.hold_id "
        "WHERE i.evidence_id = ANY(%s) AND h.status = 'active'",
        (ids,),
    )
    return {r[0] for r in cur.fetchall()}


def assert_evidence_not_held(
    cur,
    evidence_ids: Iterable[str],
    *,
    code: str,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """正式写入路径的撤回拦截：任一目标证据仍被生效保全覆盖即整笔拒绝。

    调用方须已在本事务内持有修订计数器行锁（与保全建立 / 解除同一裁决顺序）。
    拒绝详情 `blocked_evidence` 列出每条命中证据及其全部生效保全记录。
    """
    ids = sorted(set(e for e in evidence_ids if e))
    if not ids:
        return
    blocked_map = _active_holds_map(cur, ids)
    if not blocked_map:
        return
    blocked = [
        {"evidence_id": eid, "holds": blocked_map[eid]}
        for eid in sorted(blocked_map)
    ]
    raise ApiError(
        409,
        code,
        message,
        {
            **(extra or {}),
            "current_revision": _current_revision(cur),
            "blocked_evidence_ids": [item["evidence_id"] for item in blocked],
            "blocked_evidence": blocked,
        },
    )


# ------------------------------------------------------------------ 建立
def create_hold(body: HoldCreateIn) -> Dict[str, Any]:
    """建立临时保全（语义见模块文档）。成功返回保全编号与一次性解除凭据。"""
    # 结构层先于事务：证据编号不得重复
    evidence_ids = list(body.evidence_ids)
    seen: set = set()
    duplicates = sorted({eid for eid in evidence_ids if eid in seen or seen.add(eid)})
    if duplicates:
        raise ApiError(
            400,
            "HOLD_DUPLICATE_EVIDENCE",
            f"保全请求中的证据编号重复: {', '.join(duplicates)}",
            {"duplicate_evidence_ids": duplicates},
        )
    evidence_ids = sorted(evidence_ids)

    credential = _new_credential()
    credential_hash = _hash_credential(credential)

    with db_conn() as conn:
        with conn.cursor() as cur:
            # 与正式写入一致的裁决顺序：先锁修订计数器（只读其值，不递增）
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            current = cur.fetchone()[0]
            if body.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前为 {current}",
                    {"current_revision": current, "expected_revision": body.expected_revision},
                )

            # 每条证据须存在且当前生效
            cur.execute(
                "SELECT evidence_id, status FROM assertions WHERE evidence_id = ANY(%s)",
                (evidence_ids,),
            )
            status_map = {r[0]: r[1] for r in cur.fetchall()}
            invalid = [
                {"evidence_id": eid, "status": status_map.get(eid)}
                for eid in evidence_ids
                if status_map.get(eid) != "active"
            ]
            if invalid:
                raise ApiError(
                    400,
                    "INVALID_HOLD_EVIDENCE",
                    "保全拒绝：证据编号须全部存在且当前生效",
                    {"invalid_evidence": invalid, "current_revision": current},
                )

            cur.execute(
                "INSERT INTO evidence_holds (revision, reason, status, credential_hash) "
                "VALUES (%s, %s, 'active', %s) RETURNING id, created_at",
                (current, body.reason, credential_hash),
            )
            hold_id, created_at = cur.fetchone()
            cur.executemany(
                "INSERT INTO evidence_hold_items (hold_id, evidence_id) VALUES (%s, %s)",
                [(hold_id, eid) for eid in evidence_ids],
            )

            # 每条证据当前的在保全来源（含本次新建；同证据可被多份保全覆盖）
            holds_map = _active_holds_map(cur, evidence_ids)
            evidences = [
                {
                    "evidence_id": eid,
                    "status": "active",
                    "holds": holds_map.get(eid, []),
                }
                for eid in evidence_ids
            ]
            return {
                "id": hold_id,
                "revision": current,
                "current_revision": current,
                "reason": body.reason,
                "status": "active",
                "evidence_ids": evidence_ids,
                "evidences": evidences,
                "credential": credential,
                "created_at": created_at.isoformat(),
            }


# ------------------------------------------------------------------ 解除
def release_hold(hold_id: int, body: HoldReleaseIn) -> Dict[str, Any]:
    """凭解除凭据解除指定保全（语义见模块文档）。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            # 与正式写入一致的裁决顺序：先锁修订计数器
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            current = cur.fetchone()[0]
            if body.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前为 {current}",
                    {"current_revision": current, "expected_revision": body.expected_revision, "hold_id": hold_id},
                )

            cur.execute(
                "SELECT status, credential_hash, revision, reason, created_at, released_at "
                "FROM evidence_holds WHERE id = %s",
                (hold_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise ApiError(
                    404,
                    "HOLD_NOT_FOUND",
                    f"保全记录不存在: {hold_id}",
                    {"hold_id": hold_id, "current_revision": current},
                )
            status, stored_hash, revision, reason, created_at, released_at = row
            if status != "active":
                raise ApiError(
                    409,
                    "HOLD_ALREADY_RELEASED",
                    f"保全 {hold_id} 已解除，不可重复解除",
                    {"hold_id": hold_id, "released_at": released_at.isoformat() if released_at else None},
                )
            # 凭据定长比较；凭据错误明确拒绝，不改变任何状态
            if not hmac.compare_digest(stored_hash, _hash_credential(body.credential)):
                raise ApiError(
                    403,
                    "INVALID_HOLD_CREDENTIAL",
                    f"解除凭据与保全 {hold_id} 不匹配",
                    {"hold_id": hold_id},
                )

            cur.execute(
                "UPDATE evidence_holds SET status = 'released', released_at = now() "
                "WHERE id = %s RETURNING released_at",
                (hold_id,),
            )
            released_at = cur.fetchone()[0]
            cur.execute(
                "SELECT evidence_id FROM evidence_hold_items WHERE hold_id = %s ORDER BY evidence_id",
                (hold_id,),
            )
            evidence_ids = [r[0] for r in cur.fetchall()]
            return {
                "id": hold_id,
                "status": "released",
                "revision": revision,
                "current_revision": current,
                "reason": reason,
                "evidence_ids": evidence_ids,
                "created_at": created_at.isoformat(),
                "released_at": released_at.isoformat(),
            }


# ------------------------------------------------------------------ 查询
def _hold_row(cur, hold_id: int):
    cur.execute(
        "SELECT id, revision, reason, status, created_at, released_at "
        "FROM evidence_holds WHERE id = %s",
        (hold_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ApiError(404, "HOLD_NOT_FOUND", f"保全记录不存在: {hold_id}", {"hold_id": hold_id})
    return row


def _hold_payload(cur, row) -> Dict[str, Any]:
    hold_id, revision, reason, status, created_at, released_at = row
    cur.execute(
        "SELECT evidence_id FROM evidence_hold_items WHERE hold_id = %s ORDER BY evidence_id",
        (hold_id,),
    )
    evidence_ids = [r[0] for r in cur.fetchall()]

    cur.execute(
        "SELECT evidence_id, status FROM assertions WHERE evidence_id = ANY(%s)",
        (evidence_ids,),
    )
    assertion_status = {r[0]: r[1] for r in cur.fetchall()}
    # 每条证据当前的在保全来源（生效中；可能来自覆盖同一证据的其他保全）
    holds_map = _active_holds_map(cur, evidence_ids)
    evidences = [
        {
            "evidence_id": eid,
            "status": assertion_status.get(eid),
            "holds": holds_map.get(eid, []),
        }
        for eid in evidence_ids
    ]
    return {
        "id": hold_id,
        "revision": revision,
        "current_revision": _current_revision(cur),
        "reason": reason,
        "status": status,
        "evidence_ids": evidence_ids,
        "evidences": evidences,
        "created_at": created_at.isoformat(),
        "released_at": released_at.isoformat() if released_at else None,
    }


def list_holds(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """保全台账摘要（可按状态过滤）；不含解除凭据。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            sql = (
                "SELECT h.id, h.revision, h.reason, h.status, h.created_at, h.released_at, "
                "array_agg(i.evidence_id ORDER BY i.evidence_id) "
                "FROM evidence_holds h JOIN evidence_hold_items i ON i.hold_id = h.id"
            )
            params: tuple = ()
            if status is not None:
                sql += " WHERE h.status = %s"
                params = (status,)
            sql += " GROUP BY h.id ORDER BY h.id"
            cur.execute(sql, params)
            return [
                {
                    "id": r[0],
                    "revision": r[1],
                    "reason": r[2],
                    "status": r[3],
                    "evidence_ids": list(r[6]),
                    "created_at": r[4].isoformat(),
                    "released_at": r[5].isoformat() if r[5] else None,
                }
                for r in cur.fetchall()
            ]


def get_hold(hold_id: int) -> Dict[str, Any]:
    """单份保全详情：状态及每条证据当前的在保全来源（不泄露解除凭据）。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            return _hold_payload(cur, _hold_row(cur, hold_id))


def get_holds_for_evidence(evidence_id: str) -> Dict[str, Any]:
    """按证据编号反查：当前证据状态、是否在保全中及全部生效保全来源。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM assertions WHERE evidence_id = %s", (evidence_id,))
            row = cur.fetchone()
            evidence_status = row[0] if row is not None else None
            holds = _active_holds_map(cur, [evidence_id]).get(evidence_id, [])
            return {
                "evidence_id": evidence_id,
                "evidence_status": evidence_status,
                "held": bool(holds),
                "active_hold_count": len(holds),
                "active_holds": holds,
                "current_revision": _current_revision(cur),
            }
