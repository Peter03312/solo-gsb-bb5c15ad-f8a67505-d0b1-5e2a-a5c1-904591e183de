"""变化追踪候选定位正确性属性测试（无需数据库）。

用旧的“全库逐对 Dijkstra”口径作为参照，对大量随机 / 构造场景验证新的
锥集候选精算结果与其逐条一致（关系、路径、顺序完全相同）。

运行：python3 tests/test_relation_diffs_equivalence.py
"""
from __future__ import annotations

import os
import random
import sys
import types
from typing import Dict, List, Sequence, Tuple

# changes.py 顶部 import psycopg2.extras.Json；纯函数测试不需要数据库
_psycopg2 = types.ModuleType("psycopg2")
_extras = types.ModuleType("psycopg2.extras")
_extras.Json = lambda obj: obj
_psycopg2.extras = _extras
sys.modules.setdefault("psycopg2", _psycopg2)
sys.modules.setdefault("psycopg2.extras", _extras)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.changes import _candidate_pairs, relation_diffs  # noqa: E402
from app.derivation import Assertion, Derivation, EARLIER, SAME  # noqa: E402


def brute_force_diffs(
    strata: Sequence[str],
    before_assertions: Sequence[Assertion],
    after_assertions: Sequence[Assertion],
) -> List[dict]:
    """旧口径：全库逐对比较（参照实现）。"""
    before = Derivation(before_assertions)
    after = Derivation(after_assertions)
    out: List[dict] = []
    ordered = sorted(strata)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            old_rel, old_path = before.relation(a, b)
            new_rel, new_path = after.relation(a, b)
            old_path = old_path or []
            new_path = new_path or []
            if old_rel != new_rel or old_path != new_path:
                out.append(
                    {
                        "stratum_a": a,
                        "stratum_b": b,
                        "old_relation": old_rel,
                        "new_relation": new_rel,
                        "old_path": old_path,
                        "new_path": new_path,
                    }
                )
    return out


def random_assertions(rng: random.Random, strata: List[str], n: int, evidence_prefix: str) -> List[Assertion]:
    assertions: Dict[str, Assertion] = {}
    for i in range(n):
        a, b = rng.sample(strata, 2)
        eid = f"{evidence_prefix}-{i:04d}"
        assertions[eid] = Assertion(eid, a, b, rng.choice([SAME, EARLIER]))
    return list(assertions.values())


def check_case(name: str, strata: List[str], before: List[Assertion], after: List[Assertion]) -> Tuple[int, int, int]:
    expected = brute_force_diffs(strata, before, after)
    got = relation_diffs(strata, before, after)
    candidates = _candidate_pairs(strata, before, after)
    if expected != got:
        print(f"FAIL [{name}]")
        print(" expected:", expected)
        print(" got     :", got)
        raise AssertionError(name)
    # 真正变化的对必须全部在候选中
    changed_pairs = {(d["stratum_a"], d["stratum_b"]) for d in expected}
    cand_set = set(candidates)
    if not changed_pairs <= cand_set:
        missing = changed_pairs - cand_set
        print(f"FAIL [{name}] missing candidates: {missing}")
        raise AssertionError(name)
    return len(got), len(cand_set), len(strata) * (len(strata) - 1) // 2


def main() -> None:
    rng = random.Random(20261003)
    cases = 0
    total_candidates = 0
    total_pairs = 0
    total_changes = 0

    # 1) 纯随机两侧断言（after 可能有冲突也没关系：relation 口径不依赖 validate）
    for t in range(400):
        ns = rng.randint(2, 26)
        strata = [f"L{i:02d}" for i in range(ns)]
        nb = rng.randint(0, 30)
        na = rng.randint(0, 30)
        before = random_assertions(rng, strata, nb, f"RB{t}b")
        after = random_assertions(rng, strata, na, f"RA{t}a")
        changed, ncand, npairs = check_case(f"random-{t}", strata, before, after)
        total_changes += changed
        total_candidates += ncand
        total_pairs += npairs
        cases += 1

    # 2) “大部分无关 + 局部变更”：大量孤立地层，只动一小簇（性能主张场景）
    for t in range(100):
        strata = [f"U{i:04d}" for i in range(500)]
        # 全库随机铺一批互不相连的成对断言
        before = []
        used = list(strata)
        rng.shuffle(used)
        for i, (a, b) in enumerate(zip(used[0::2], used[1::2])):
            before.append(Assertion(f"BASE-{t}-{i:04d}", a, b, rng.choice([SAME, EARLIER])))
        # 只在 6 个地层的局部簇上做一次新增/撤回
        local = rng.sample(strata, 6)
        after = list(before)
        eid = f"LOCAL-{t}"
        if rng.random() < 0.5:
            after.append(Assertion(eid, local[0], local[1], rng.choice([SAME, EARLIER])))
        else:
            # 撤回：从 before 里挑一条删掉（after 模拟其被撤回）
            after = after[:-1]
        changed, ncand, npairs = check_case(f"sparse-{t}", strata, before, after)
        total_changes += changed
        total_candidates += ncand
        total_pairs += npairs
        cases += 1

    # 3) 构造：同期合并扩散 —— A、B 两个无关同期组，A 早于 C；
    #    一条同期边合并 A/B 后，B 的成员也对 C 变“早于”（跨类扩散）。
    for t in range(50):
        strata = [f"M{i:02d}" for i in range(6)]
        before = [
            Assertion("SAA", "M0", "M1", SAME),       # 组 A
            Assertion("SBB", "M2", "M3", SAME),       # 组 B
            Assertion("SCC", "M4", "M5", SAME),       # 组 C
            Assertion("EAC", "M1", "M4", EARLIER),    # A 早于 C
        ]
        after = before + [Assertion("MERGE", "M1", "M3", SAME)]  # 合并 A、B
        changed, ncand, npairs = check_case(f"merge-{t}", strata, before, after)
        total_changes += changed
        total_candidates += ncand
        total_pairs += npairs
        cases += 1

    # 4) 构造：同期拆分 —— 撤回唯一同期桥：S0~S1、S1<S2，
    #    拆桥后 S0 对 S1 变未定、S0 对 S2 由“早于”变未定。
    for t in range(50):
        strata = [f"D{i:02d}" for i in range(rng.randint(3, 16))]
        assertions = [
            Assertion("BRIDGE", strata[0], strata[1], SAME),
            Assertion("EDGE", strata[1], strata[2], EARLIER),
        ]
        # 组内再加些同期成员，扩散面更大
        for i in range(3, len(strata)):
            assertions.append(Assertion(f"DS{i}", strata[i - 1], strata[i], SAME))
        before = assertions
        after = [a for a in assertions if a.evidence_id != "BRIDGE"]
        changed, ncand, npairs = check_case(f"split-{t}", strata, before, after)
        total_changes += changed
        total_candidates += ncand
        total_pairs += npairs
        cases += 1

    # 5) 构造：平行证据导致仅最优路径改变（关系不变）
    for t in range(50):
        strata = [f"P{i}" for i in range(4)]
        before = [
            Assertion("PA1", "P0", "P1", EARLIER),
            Assertion("PA2", "P1", "P3", EARLIER),
            Assertion("PB1", "P0", "P2", EARLIER),
            Assertion("PB2", "P2", "P3", EARLIER),
        ]
        after = before + [Assertion("DIRECT", "P0", "P3", EARLIER)]
        changed, ncand, npairs = check_case(f"path-only-{t}", strata, before, after)
        total_changes += changed
        total_candidates += ncand
        total_pairs += npairs
        cases += 1

    # 6) 边界：空 / 单地层 / 无变更
    check_case("empty", [], [], [])
    check_case("single", ["S0"], [], [Assertion("X", "S0", "S0", SAME)])
    base = [Assertion("Q1", "Q0", "Q1", SAME)]
    check_case("no-change", ["Q0", "Q1", "Q2"], base, list(base))
    check_case("first-revision", ["Q0", "Q1"], [], [Assertion("Q1", "Q0", "Q1", SAME)])

    # 7) 含未出现在任何断言中的孤立地层（普通批次只登记地层的场景）
    strata = [f"ISO{i}" for i in range(10)]
    before = [Assertion("I0", "ISO0", "ISO1", EARLIER)]
    after = before + [Assertion("I1", "ISO1", "ISO2", EARLIER)]
    check_case("isolated", strata, before, after)

    # 8) 多修订顺序游走：在一个不断演化的断言集上做数百次随机新增 / 撤回，
    #    每个修订都用旧逐对口径比对（模拟普通批次、撤回、勘误、推演并入共用口径）
    for t in range(20):
        rng2 = random.Random(1000 + t)
        n = rng2.randint(5, 30)
        strata = [f"W{i:02d}" for i in range(n)]
        active: Dict[str, Assertion] = {}
        seq = 0
        for step in range(120):
            before = sorted(active.values(), key=lambda a: a.evidence_id)
            choice = rng2.random()
            if choice < 0.6 or not active:
                a, b = rng2.sample(strata, 2)
                eid = f"W{t}-{seq:04d}"
                seq += 1
                active[eid] = Assertion(eid, a, b, rng2.choice([SAME, EARLIER]))
            elif choice < 0.8:
                # 撤回一条随机生效证据
                del active[rng2.choice(list(active))]
            else:
                # 同批多条：新增 + 撤回混合
                if active:
                    del active[rng2.choice(list(active))]
                a, b = rng2.sample(strata, 2)
                eid = f"W{t}-{seq:04d}"
                seq += 1
                active[eid] = Assertion(eid, a, b, rng2.choice([SAME, EARLIER]))
            after = sorted(active.values(), key=lambda a: a.evidence_id)
            changed, ncand, npairs = check_case(f"walk-{t}-{step}", strata, before, after)
            total_changes += changed
            total_candidates += ncand
            total_pairs += npairs
            cases += 1

    print(
        f"ALL {cases} CASES PASSED | total changes={total_changes} "
        f"candidates={total_candidates} / pairs={total_pairs} "
        f"({100.0 * total_candidates / max(total_pairs, 1):.2f}% of pairwise work)"
    )


if __name__ == "__main__":
    main()
