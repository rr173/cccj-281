#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
随机化差分测试：用完全独立的暴力实现（枚举左点排列求最大匹配规模）
对照 met_coverage 的 BFS 增广匹配与最大性证书，跑多种随机二分图。
运行：python3 -I tests/random_diff.py
"""
import itertools
import random
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin"))
import met_coverage as mc  # noqa: E402


def brute_force_max(left, adj):
    """枚举左点所有排列，贪心取可分配观测，返回最大匹配规模（独立参照实现）。"""
    best = 0
    for perm in itertools.permutations(left):
        used = set()
        size = 0
        for u in perm:
            for v in adj.get(u, []):
                if v not in used:
                    used.add(v)
                    size += 1
                    break
        best = max(best, size)
    return best


def main():
    rng = random.Random(20260301)
    fails = 0
    trials = 400
    for t in range(trials):
        n_left = rng.randint(1, 6)
        n_right = rng.randint(0, 7)
        left = [f"L{i}" for i in range(n_left)]
        right = [f"R{j}" for j in range(n_right)]
        adj = {}
        for u in left:
            k = rng.randint(0, n_right)
            adj[u] = sorted(rng.sample(right, k)) if k else []
        match_L, match_R, _ = mc.maximum_matching(left, adj)
        got = len(match_L)
        expect = brute_force_max(left, adj)
        # 匹配合法性
        right_owners = list(match_R.values())
        ok_valid = (len(right_owners) == len(set(right_owners))
                    and len(set(match_L.keys())) == len(match_L)
                    and all(rgt in adj[lft] for lft, rgt in match_L.items()))
        cert = mc.maximality_certificate(left, adj, match_L, match_R)
        ok_cert = (cert["cover_size"] == got == cert["matching_size"])
        if cert["hall_witness"]:
            hw = cert["hall_witness"]
            ok_cert = ok_cert and len(hw["neighbor_observations_N_ZL"]) < len(hw["station_subset_ZL"])
            ok_cert = ok_cert and cert["deficiency"] == len(cert["unmatched_stations"])
        else:
            ok_cert = ok_cert and not cert["unmatched_stations"]
        if got != expect or not ok_valid or not ok_cert:
            fails += 1
            print(f"[FAIL] trial {t}: got {got} expect {expect} valid {ok_valid} cert {ok_cert}")
            print("  adj =", adj)
    print(f"随机差分 {trials} 组：{'全部一致' if fails == 0 else str(fails) + ' 组不一致'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
