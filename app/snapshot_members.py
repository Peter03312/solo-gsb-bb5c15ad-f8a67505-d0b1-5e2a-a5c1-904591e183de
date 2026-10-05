"""定版成员（snapshot membership）的时点一致性判定。

定版除冻结断言与确定关系结论外，还冻结**封存时刻全部已登记地层**（成员清单
`snapshots.members`）：历史定版关系查询与成果包封存只接受该版成员；版外地层
明确拒绝，不与版内真实“未定”混淆。

既有定版（`members` 为 NULL，本列引入前封存）缺少成员清单，只用定版自身封存的
历史记录证实成员身份：

- 冻结断言副本 `snapshots.assertions` 中出现的地层——断言在封存时刻生效，
  其端点地层必然当时已登记；
- 冻结结论 `snapshot_conclusions` 中出现的地层——结论只对当时已登记地层计算。

两处都不出现的地层**无法证实**（可能是当时已登记但无断言留痕的孤立地层，也可能
是定版后才登记的新地层），按“不可判定”明确拒绝，不伪造关系；已封成果包内容、
版内结论与证据路径保持原样。
"""
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .errors import ApiError

# 成员判定结果
MEMBER = "member"                  # 该版成员（清单在内，或既有定版已被封存记录证实）
NOT_MEMBER = "not_member"          # 有成员清单且不在其中：版外地层（定版后才登记）
UNDETERMINABLE = "undeterminable"  # 既有定版无成员清单，封存记录不足以证实


def frozen_assertion_strata(frozen_assertions: Sequence[Dict[str, Any]]) -> Set[str]:
    """定版冻结断言副本中出现的全部地层编号。"""
    strata: Set[str] = set()
    for a in frozen_assertions:
        strata.add(a["stratum_a"])
        strata.add(a["stratum_b"])
    return strata


def classify(
    cur,
    version: int,
    members: Optional[Sequence[str]],
    assertion_strata: Set[str],
    stratum_id: str,
) -> str:
    """判定地层在该定版中的成员身份。

    - 定版带成员清单（新定版）：清单内即成员，清单外即版外地层；
    - 既有定版（members 为 NULL）：冻结断言副本或冻结结论中出现即证实为成员，
      否则不可判定（不读在线地层表的时间状态，不做无依据推断）。
    """
    if members is not None:
        return MEMBER if stratum_id in members else NOT_MEMBER
    if stratum_id in assertion_strata:
        return MEMBER
    cur.execute(
        "SELECT 1 FROM snapshot_conclusions "
        "WHERE version = %s AND (stratum_a = %s OR stratum_b = %s) LIMIT 1",
        (version, stratum_id, stratum_id),
    )
    return MEMBER if cur.fetchone() is not None else UNDETERMINABLE


def ensure_member(
    cur,
    version: int,
    members: Optional[Sequence[str]],
    assertion_strata: Set[str],
    stratum_id: str,
) -> None:
    """单个地层的成员校验（历史定版关系查询）：版外 / 不可判定明确拒绝。"""
    status = classify(cur, version, members, assertion_strata, stratum_id)
    if status == NOT_MEMBER:
        raise ApiError(
            400,
            "STRATUM_NOT_IN_VERSION",
            f"地层不在定版 {version} 的成员清单内（定版封存后才登记）: {stratum_id}",
            {"version": version, "stratum_id": stratum_id},
        )
    if status == UNDETERMINABLE:
        raise ApiError(
            409,
            "SNAPSHOT_MEMBERSHIP_UNDETERMINABLE",
            f"定版 {version} 为缺少成员清单的既有定版，其封存记录不足以证实地层 "
            f"{stratum_id} 在定版时刻已登记，成员身份不可判定",
            {"version": version, "stratum_id": stratum_id},
        )


def collect_non_members(
    cur,
    version: int,
    members: Optional[Sequence[str]],
    assertion_strata: Set[str],
    strata: Sequence[str],
) -> Tuple[List[str], List[str]]:
    """批量判定（成果包封存）：返回 (版外地层, 不可判定地层)，均按输入顺序去重。"""
    not_member: List[str] = []
    undeterminable: List[str] = []
    for s in strata:
        status = classify(cur, version, members, assertion_strata, s)
        if status == NOT_MEMBER:
            not_member.append(s)
        elif status == UNDETERMINABLE:
            undeterminable.append(s)
    return not_member, undeterminable
