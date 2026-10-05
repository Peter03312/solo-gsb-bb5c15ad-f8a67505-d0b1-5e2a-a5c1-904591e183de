"""变化追踪性能基准（无需数据库）。

复现用户场景：大量彼此无关的地层已登记后，一次局部断言变更的差异计算耗时。
对比旧“全库逐对 Dijkstra”口径与新“锥集定位 + 候选精算”口径。

运行：python3 tests/bench_relation_diffs.py
"""
from __future__ import annotations

import os
import sys
import time
import types
from typing import List, Sequence

_psycopg2 = types.ModuleType("psycopg2")
_extras = types.ModuleType("psycopg2.extras")
_extras.Json = lambda obj: obj
_psycopg2.extras = _extras
sys.modules.setdefault("psycopg2", _psycopg2)
sys.modules.setdefault("psycopg2.extras", _extras)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.changes import _candidate_pairs, relation_diffs  # noqa: E402
from app.derivation import Assertion, Derivation, EARLIER  # noqa: E402


def brute_force_diffs(strata: Sequence[str], before, after) -> List[tuple]:
    before_d = Derivation(before)
    after_d = Derivation(after)
    out = []
    ordered = sorted(strata)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            old_rel, old_path = before_d.relation(a, b)
            new_rel, new_path = after_d.relation(a, b)
            if old_rel != new_rel or (old_path or []) != (new_path or []):
                out.append((a, b))
    return out


def make_case(n_strata: int):
    strata = [f"L{i:05d}" for i in range(n_strata)]
    # 全库已有互不相连的既存同一期证据（每个地层都被断言触及）
    before: List[Assertion] = [
        Assertion(f"E{i:05d}", strata[i], strata[i + 1], "same_period")
        for i in range(0, n_strata - 1, 2)
    ]
    # 一次局部断言变更：在 0/1/2 三个地层上新增一条早于断言
    after = before + [Assertion("E-LOCAL", strata[0], strata[2], EARLIER)]
    return strata, before, after


def run(n_strata: int, also_brute: bool) -> None:
    strata, before, after = make_case(n_strata)
    t0 = time.perf_counter()
    cands = _candidate_pairs(strata, before, after)
    t1 = time.perf_counter()
    diffs = relation_diffs(strata, before, after)
    t2 = time.perf_counter()
    line = (
        f"n={n_strata:>6} pairs={n_strata*(n_strata-1)//2:>11} "
        f"candidates={len(cands):>3} changes={len(diffs):>2} "
        f"new: locate={1000*(t1-t0):7.2f}ms total={1000*(t2-t0):7.2f}ms"
    )
    if also_brute:
        old = brute_force_diffs(strata, before, after)
        t3 = time.perf_counter()
        assert [ (d['stratum_a'], d['stratum_b']) for d in diffs] == old
        old_ms = 1000 * (t3 - t2)
        line += f" | old pairwise={old_ms:9.2f}ms speedup={old_ms/max(1000*(t2-t0),1e-9):8.1f}x"
    print(line, flush=True)


if __name__ == "__main__":
    for n in (500, 1000, 2000):
        run(n, also_brute=True)
    for n in (10_000, 50_000):
        run(n, also_brute=False)
