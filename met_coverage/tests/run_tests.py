#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端验证：
  1. 运行 v1/v2 与两个错误输入，检查退出码与版本不可变；
  2. 对机器可读结果独立复核：
     - 认领唯一性（每站至多 1 份、每份观测在该单元该小时至多被 1 站认领）
     - 每条分配的有向链确实沿当时有效边（或长度0自身链）
     - 最大性证书：|最小顶点覆盖| == 匹配规模；Hall 不等式 |N(Z_L)| < |Z_L|
     - 覆盖数与阈值判定；跨单元同一观测被独立复用；
  3. 断言两个规定场景（竞争、替代路径/重配）确实出现。
运行：python3 -I tests/run_tests.py
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import met_coverage as mc  # noqa: E402

PY = sys.executable
SCRIPT = os.path.join(ROOT, "bin", "met_coverage.py")
IN = os.path.join(ROOT, "inputs")
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def run(inp, out_root, expect_rc):
    rc = subprocess.run([PY, "-I", SCRIPT, "--input", inp, "--out-root", out_root],
                        capture_output=True, text=True)
    check(f"退出码={expect_rc} ({os.path.basename(inp)})", rc.returncode == expect_rc,
          f"got {rc.returncode}, stderr={rc.stderr.strip()}")
    return rc


def load(out_root, version):
    with open(os.path.join(out_root, version, "coverage.json"), encoding="utf-8") as fh:
        return json.load(fh)


def independently_verify(report, raw):
    """对报告的每个 cell 做独立复核。"""
    # 重新构造原始边集合用于链校验
    edge_by_id = {e["id"]: e for e in raw["substitution_edges"]}
    for r in report["results"]:
        hk, uid = r["hour"], r["unit_id"]
        assigns = r["assignments"]
        left_used = [a["reference_station"] for a in assigns]
        right_used = [a["physical_observation_id"] for a in assigns]
        check(f"{hk}/{uid} 每站至多认领1份", len(left_used) == len(set(left_used)))
        check(f"{hk}/{uid} 每份观测至多被1站认领", len(right_used) == len(set(right_used)))
        check(f"{hk}/{uid} 覆盖数=认领数", r["maximum_provable_coverage"] == len(assigns))

        # 链方向与有效性复核
        for a in assigns:
            chain_st, chain_ed = a["directed_chain_stations"], a["directed_chain_edges"]
            if not chain_ed:
                check(f"{hk}/{uid} {a['reference_station']} 长度0链指向自身站",
                      chain_st == [a["reference_station"]] and a["actual_station"] == a["reference_station"])
            else:
                ok = len(chain_st) == len(chain_ed) + 1 and chain_st[-1] == a["actual_station"]
                for i, eid in enumerate(chain_ed):
                    e = edge_by_id[eid]
                    ok = ok and e["from"] == chain_st[i] and e["to"] == chain_st[i + 1]
                    ok = ok and uid in e["applies_to_units"]
                    ok = ok and mc.covers(mc.parse_dt(e["valid_from"], "x"),
                                          mc.parse_dt(e["valid_to"], "x"),
                                          mc.parse_dt(hk, "x"))
                check(f"{hk}/{uid} {a['reference_station']} 替代链沿有效有向边", ok)

        # 最大性证书
        c = r["maximality_certificate"]
        check(f"{hk}/{uid} |最小顶点覆盖|==匹配规模(Kőnig)", c["cover_size"] == c["matching_size"] == len(assigns))
        if c["hall_witness"]:
            hw = c["hall_witness"]
            check(f"{hk}/{uid} Hall 不等式成立",
                  len(hw["neighbor_observations_N_ZL"]) < len(hw["station_subset_ZL"]))
            check(f"{hk}/{uid} 亏缺数=未匹配站数",
                  c["deficiency"] == len(c["unmatched_stations"]) == r["effective_station_count"] - len(assigns))
            # 独立重算 N(Z_L)
            adj = r["bipartite_adjacency"]
            n_zl = sorted({v for s in hw["station_subset_ZL"] for v in adj.get(s, [])})
            check(f"{hk}/{uid} Hall 邻接集可独立重算", n_zl == hw["neighbor_observations_N_ZL"])
        else:
            check(f"{hk}/{uid} 满配无未匹配站", not c["unmatched_stations"]
                  and len(assigns) == r["effective_station_count"])

        # 阈值判定
        if r["threshold_required"] is not None:
            check(f"{hk}/{uid} 阈值判定正确",
                  r["threshold_met"] == (r["maximum_provable_coverage"] >= r["threshold_required"]))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        os.makedirs(out)

        print("== 1. 错误输入必须被拒绝 ==")
        run(os.path.join(IN, "err_cycle.json"), out, 2)
        run(os.path.join(IN, "err_conflict.json"), out, 2)
        check("错误输入不产生任何版本目录", os.listdir(out) == [])

        print("== 2. v1 正常核算 ==")
        run(os.path.join(IN, "v1.json"), out, 0)
        v1_raw = json.load(open(os.path.join(IN, "v1.json"), encoding="utf-8"))
        rep1 = load(out, "v1")
        independently_verify(rep1, v1_raw)
        cells = {(r["hour"], r["unit_id"]): r for r in rep1["results"]}

        r = cells[("2026-03-01T08:00", "U1")]
        check("v1 08/U1 3 站满配（非贪心，增广重配后达到）", r["maximum_provable_coverage"] == 3
              and r["threshold_met"] is True)
        reassign_events = [e for e in r["augmentation_log"]
                           if e["augmented"] and e["reassignments"]]
        check("v1 08/U1 存在重配见证（贪心陷阱被增广路纠正）", len(reassign_events) == 1,
              json.dumps(r["augmentation_log"], ensure_ascii=False))
        # R2 处理时：OBS-06 从 R1 重配给 R2，R1 改沿替代链认领 R3 的观测
        r2_event = next(e for e in r["augmentation_log"] if e["station"] == "R2")
        check("v1 08/U1 R2 增广路把 OBS-06 由 R1 重配给 R2",
              r2_event["reassignments"] == [
                  {"observation": "OBS-06", "from_station": "R1", "to_station": "R2"}])
        # 替代路径见证：R1 无自身观测，最终认领的观测必须带有效有向替代链
        r1 = next(a for a in r["assignments"] if a["reference_station"] == "R1")
        check("v1 08/U1 R1 经有向替代链（E1 或 E2）认领下游站观测",
              r1["chain_kind"] == "替代链" and r1["directed_chain_edges"] in (["E1"], ["E2"])
              and r1["directed_chain_stations"][0] == "R1"
              and r1["actual_station"] in ("R2", "R3"))
        # 有向分叉：R1 可经 E1/E2 到达 R2、R3 的全部合格观测
        check("v1 08/U1 R1 邻接覆盖 R2、R3 全部合格观测（有向分叉）",
              set(r["bipartite_adjacency"]["R1"]) == {"OBS-06", "OBS-07", "OBS-09", "OBS-10"})
        # 有向性：R2 在 U1 无核准出边，只能用自身观测；R3 也只能用自身 R3 观测
        check("v1 08/U1 R2 邻接受限于自身 OBS-06（方向不被压成等价组）",
              r["bipartite_adjacency"]["R2"] == ["OBS-06"])
        check("v1 08/U1 R3 只能用自身 R3 观测（方向不被压成等价组）",
              set(r["bipartite_adjacency"]["R3"]) == {"OBS-07", "OBS-09", "OBS-10"})
        # 零值观测被正常使用
        check("v1 08/U1 零值 OBS-06 被正常认领",
              any(a["physical_observation_id"] == "OBS-06" for a in r["assignments"]))
        check("v1 08/U1 质量 BAD 的 OBS-05 被排除",
              any(x["observation_id"] == "OBS-05" for x in r["excluded_observations"]))

        r = cells[("2026-03-01T09:00", "U1")]
        check("v1 09/U1 两站竞争一份 OBS-04，覆盖=1 且不足",
              r["maximum_provable_coverage"] == 1 and r["threshold_met"] is False)
        # R1 按站序先经 E2 增广占用 OBS-04；R3（本站）与 R2（无 U1 出边、空邻接）未匹配
        check("v1 09/U1 未匹配站为 R2,R3",
              r["maximality_certificate"]["unmatched_stations"] == ["R2", "R3"])
        check("v1 09/U1 唯一认领者 R1 拿到 OBS-04（经 E2 替代链）",
              r["assignments"][0]["reference_station"] == "R1"
              and r["assignments"][0]["physical_observation_id"] == "OBS-04"
              and r["assignments"][0]["directed_chain_edges"] == ["E2"])
        # 竞争两方邻接完全相同（一份替代观测不能冒充多站）；R2 邻接为空
        check("v1 09/U1 R1 与 R3 邻接都仅为 OBS-04，R2 邻接为空",
              r["bipartite_adjacency"]["R1"] == ["OBS-04"]
              and r["bipartite_adjacency"]["R3"] == ["OBS-04"]
              and r["bipartite_adjacency"]["R2"] == [])

        r2u = cells[("2026-03-01T08:00", "U2")]
        check("v1 08/U2 到期前沿 E4 满配 2", r2u["maximum_provable_coverage"] == 2)
        r2u9 = cells[("2026-03-01T09:00", "U2")]
        check("v1 09/U2 E4 到期，覆盖降为 1 且不足",
              r2u9["maximum_provable_coverage"] == 1 and r2u9["threshold_met"] is False)
        check("v1 09/U2 到期边被标注原因",
              any(e["edge_id"] == "E4" and "到期" in e["reason"] for e in r2u9["excluded_substitution_edges"]))

        # 跨单元独立：OBS-07 在 08 点被 U1、U2 各认领一次
        u1_obs = {a["physical_observation_id"] for a in cells[("2026-03-01T08:00", "U1")]["assignments"]}
        u2_obs = {a["physical_observation_id"] for a in cells[("2026-03-01T08:00", "U2")]["assignments"]}
        check("v1 08 同一观测跨单元独立复用、不相加", "OBS-07" in u1_obs and "OBS-07" in u2_obs)

        print("== 3. v2 重述：旧版保留、不可覆盖 ==")
        # 重复运行 v1 必须被拒绝
        rc = subprocess.run([PY, "-I", SCRIPT, "--input", os.path.join(IN, "v1.json"), "--out-root", out],
                            capture_output=True, text=True)
        check("重复出版本 v1 被拒绝（版本不可变）", rc.returncode == 2)

        run(os.path.join(IN, "v2.json"), out, 0)
        check("v1 输出目录仍保留", os.path.isdir(os.path.join(out, "v1")))
        rep2 = load(out, "v2")
        v2_raw = json.load(open(os.path.join(IN, "v2.json"), encoding="utf-8"))
        independently_verify(rep2, v2_raw)
        for r in rep2["results"]:
            check(f"v2 {r['hour']}/{r['unit_id']} 质量修订后覆盖=0、不足",
                  r["maximum_provable_coverage"] == 0 and r["threshold_met"] is False)
            check(f"v2 {r['hour']}/{r['unit_id']} OBS-04 因质量被排除",
                  any(x["observation_id"] == "OBS-04" for x in r["excluded_observations"]))
        man = json.load(open(os.path.join(out, "v2", "manifest.json"), encoding="utf-8"))
        check("v2 manifest 记录 restates=v1 且保留旧版",
              man["restates"] == ["v1"] and "v1" in man["prior_versions_preserved"])
        h1 = json.load(open(os.path.join(out, "v1", "coverage.json"), encoding="utf-8"))["input_file"]["sha256"]
        h2 = rep2["input_file"]["sha256"]
        check("两版输入哈希不同", h1 != h2)

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
