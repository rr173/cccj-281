#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
气象风险单元 —— 按小时/单元核算“最大可证明观测覆盖数”。

模型（每个「小时 × 单元」独立核算）：
  左顶点 L : 该小时在该单元多边形内且逻辑基准站有效期覆盖该小时的基准站
  右顶点 R : 该小时实际产生、质量合格、且实际站点已知的物理观测
  边  L->R : 基准站的物理站点 能沿【当时对该单元有效】的核准有向替代边
             （含长度 0 的自身链）到达观测的实际站点
  原站物理观测天然可服务自己（长度 0 链），无需替代边。
  约束：每个基准站最多认领一份观测；每份物理观测在该单元该小时最多供一个基准站。
  => 二分图最大匹配（Hopcroft–Karp 风格不必，这里用带 BFS 增广路的 Kuhn，
     显式输出重配/增广见证，避免贪心误判；再给残差 BFS 的 Hall 最大性证明）。

替代链始终保留方向：绝不压成无向等价组。环、同输入版内同 ID 载荷冲突 => 资料错误，拒绝出版本。
不同单元独立：同一物理观测可在不同单元分别被认领，互不相加。
版本化：替代到期或质量修订必须以新输入版重述，旧版本输出原样保留。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------
# 1. 基础工具：时间 / 几何
# ----------------------------------------------------------------------------

def parse_dt(s: str, where: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where}: 无法解析时间 {s!r}（需 ISO 8601，如 2026-03-01T09:00）") from exc


def hour_key(dt: datetime) -> str:
    """小时键，精确到整点。"""
    if dt.minute != 0 or dt.second != 0 or dt.microsecond != 0:
        raise ValueError(f"观测时间 {dt.isoformat()} 不是整点；本核算按小时进行")
    return dt.strftime("%Y-%m-%dT%H:00")


def covers(valid_from: Optional[datetime], valid_to: Optional[datetime], t: datetime) -> bool:
    """半开区间 [valid_from, valid_to)；端点缺省表示无界。"""
    if valid_from is not None and t < valid_from:
        return False
    if valid_to is not None and not (t < valid_to):
        return False
    return True


def point_in_polygon(point: List[float], ring: List[List[float]]) -> bool:
    """射线法（ray casting）判定点是否在多边形内；边界点视为在内。ring 首尾可重合也可不重合。"""
    x, y = float(point[0]), float(point[1])
    n = len(ring)
    if n < 3:
        raise ValueError(f"多边形至少需要 3 个顶点，收到 {n} 个")
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(ring[i][0]), float(ring[i][1])
        xj, yj = float(ring[j][0]), float(ring[j][1])
        # 边界点（顶点或落在边上）直接视为在内
        dxi, dyi = xj - xi, yj - yi
        cross = (x - xi) * dyi - (y - yi) * dxi
        if cross == 0 and min(xi, xj) <= x <= max(xi, xj) and min(yi, yj) <= y <= max(yi, yj):
            return True
        if (yi > y) != (yj > y):
            x_cross = xi + (y - yi) * dxi / dyi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------
# 2. 输入载入与校验（环、同 ID 载荷冲突等资料错误在此拒绝）
# ----------------------------------------------------------------------------

class InputError(Exception):
    pass


def load_input(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise InputError(f"输入文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"输入不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError("输入顶层必须是对象")
    for key in ("version", "reference_stations", "observations", "units", "substitution_edges", "thresholds"):
        if key not in data:
            raise InputError(f"输入缺少必填字段: {key}")
    return data


def validate(data: Dict[str, Any]) -> Dict[str, Any]:
    """全面校验，返回解析后的结构；任何资料错误抛 InputError。"""
    errors: List[str] = []

    version = str(data["version"])
    if not version:
        errors.append("version 不能为空")

    # ---- 基准站 ----
    ref_by_id: Dict[str, Dict[str, Any]] = {}
    for st in data["reference_stations"]:
        rid = st.get("id")
        if not rid:
            errors.append("存在缺少 id 的基准站")
            continue
        if rid in ref_by_id:
            errors.append(f"基准站 id 重复: {rid}")
            continue
        try:
            pos = st["position"]
            x, y = float(pos[0]), float(pos[1])
        except (KeyError, TypeError, ValueError, IndexError):
            errors.append(f"基准站 {rid}: position 必须是 [x, y]")
            continue
        vf = parse_dt(st["valid_from"], f"基准站 {rid}.valid_from") if st.get("valid_from") else None
        vt = parse_dt(st["valid_to"], f"基准站 {rid}.valid_to") if st.get("valid_to") else None
        if vf and vt and vt <= vf:
            errors.append(f"基准站 {rid}: valid_to 必须晚于 valid_from（半开区间）")
        ref_by_id[rid] = {
            "id": rid,
            "name": st.get("name", rid),
            "physical_station_id": st.get("physical_station_id", rid),
            "position": [x, y],
            "valid_from": vf,
            "valid_to": vt,
        }

    # ---- 单元多边形 ----
    unit_by_id: Dict[str, Dict[str, Any]] = {}
    for un in data["units"]:
        uid = un.get("id")
        if not uid:
            errors.append("存在缺少 id 的单元")
            continue
        if uid in unit_by_id:
            errors.append(f"单元 id 重复: {uid}")
            continue
        ring = un.get("polygon")
        if not isinstance(ring, list) or len(ring) < 3:
            errors.append(f"单元 {uid}: polygon 至少需要 3 个顶点")
            continue
        try:
            norm = [[float(p[0]), float(p[1])] for p in ring]
        except (TypeError, ValueError, IndexError):
            errors.append(f"单元 {uid}: polygon 顶点必须是 [x, y] 数值对")
            continue
        unit_by_id[uid] = {"id": uid, "name": un.get("name", uid), "polygon": norm}

    # ---- 有向替代边 ----
    edges: List[Dict[str, Any]] = []
    edge_ids: set = set()
    for i, ed in enumerate(data["substitution_edges"]):
        eid = ed.get("id", f"edge#{i}")
        if eid in edge_ids:
            errors.append(f"替代边 id 重复: {eid}")
        edge_ids.add(eid)
        src, dst = ed.get("from"), ed.get("to")
        if src not in ref_by_id:
            errors.append(f"替代边 {eid}: from={src!r} 不是已知基准站")
        if dst not in ref_by_id:
            errors.append(f"替代边 {eid}: to={dst!r} 不是已知基准站")
        if src == dst and src is not None:
            errors.append(f"替代边 {eid}: 自环 {src}->{dst} 构成环（资料错误）")
        try:
            vf = parse_dt(ed["valid_from"], f"替代边 {eid}.valid_from") if ed.get("valid_from") else None
            vt = parse_dt(ed["valid_to"], f"替代边 {eid}.valid_to") if ed.get("valid_to") else None
        except ValueError as exc:
            errors.append(str(exc))
            vf, vt = None, None
        if vf and vt and vt <= vf:
            errors.append(f"替代边 {eid}: valid_to 必须晚于 valid_from")
        applies = ed.get("applies_to_units", [])
        if not isinstance(applies, list) or not applies:
            errors.append(f"替代边 {eid}: applies_to_units 必须是非空数组")
            applies = []
        for u in applies:
            if u not in unit_by_id:
                errors.append(f"替代边 {eid}: applies_to_units 含未知单元 {u!r}")
        edges.append({"id": eid, "from": src, "to": dst,
                      "valid_from": vf, "valid_to": vt, "applies_to_units": set(applies)})

    # ---- 物理观测：同输入版内同 ID 载荷冲突 = 资料错误 ----
    obs_by_id: Dict[str, Dict[str, Any]] = {}
    hours: set = set()
    for ob in data["observations"]:
        oid = ob.get("id")
        if not oid:
            errors.append("存在缺少 id 的物理观测")
            continue
        try:
            t = parse_dt(ob["time"], f"观测 {oid}.time")
            hk = hour_key(t)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        payload = {
            "station": ob.get("station"),
            "time": ob["time"],
            "quality": ob.get("quality"),
            "value": ob.get("value"),
        }
        if oid in obs_by_id:
            prev = obs_by_id[oid]
            prev_tuple = (prev["station"], prev["time"], prev["quality"], prev["value"])
            new_tuple = (payload["station"], payload["time"], payload["quality"], payload["value"])
            if prev_tuple != new_tuple:
                errors.append(
                    f"同输入版 {version} 内物理观测 id 载荷冲突: {oid} 出现多份不同载荷"
                    f"（{prev['station']}@{prev['time']} vs {payload['station']}@{payload['time']}）"
                )
            else:
                errors.append(f"物理观测 id 重复: {oid}（同载荷也不允许重复登记）")
            continue
        if payload["station"] is None:
            errors.append(f"观测 {oid}: 缺少实际站点 station")
        hours.add(hk)
        obs_by_id[oid] = {"id": oid, **payload, "hour": hk, "time_dt": t}

    # 质量合格标记（零值是合法值，只有显式质量标记不合格才排除）
    for oid, ob in obs_by_id.items():
        ob["quality_ok"] = ob["quality"] == "OK"

    # ---- 阈值 ----
    thr_raw = data["thresholds"]
    if not isinstance(thr_raw, list):
        errors.append("thresholds 必须是数组")
        thr_raw = []
    thresholds: Dict[Tuple[str, Optional[str]], int] = {}
    for item in thr_raw:
        uid = item.get("unit")
        if uid not in unit_by_id:
            errors.append(f"阈值条目引用未知单元: {uid!r}")
            continue
        try:
            need = int(item["required_coverage"])
            assert need >= 0
        except (KeyError, TypeError, ValueError, AssertionError):
            errors.append(f"单元 {uid} 阈值 required_coverage 必须是非负整数")
            continue
        h = item.get("hour")  # 缺省 => 该单元所有小时
        if h is not None:
            try:
                h = hour_key(parse_dt(h, f"thresholds[{uid}].hour"))
            except ValueError as exc:
                errors.append(str(exc))
                continue
        key = (uid, h)
        if key in thresholds:
            errors.append(f"阈值重复定义: 单元 {uid} 小时 {h or '(默认)'}")
        thresholds[key] = need

    # 显式要求核算的小时（即使该小时无观测也要出报告，体现“缺测无观测”）
    eval_hours = set(hours)
    for rh in data.get("evaluate_hours", []):
        try:
            eval_hours.add(hour_key(parse_dt(rh, "evaluate_hours")))
        except ValueError as exc:
            errors.append(str(exc))

    if errors:
        raise InputError("输入校验失败（资料错误，拒绝出版本）：\n  - " + "\n  - ".join(errors))

    return {
        "version": version,
        "description": data.get("description", ""),
        "notes": data.get("notes", []),
        "restates": data.get("restates", []),
        "ref_by_id": ref_by_id,
        "unit_by_id": unit_by_id,
        "edges": edges,
        "obs_by_id": obs_by_id,
        "thresholds": thresholds,
        "hours": eval_hours,
    }


# ----------------------------------------------------------------------------
# 3. 每「小时 × 单元」：有效边图、环检测、有向可达
# ----------------------------------------------------------------------------

def build_effective_graph(parsed: Dict[str, Any], hour_dt: datetime, uid: str,
                          valid_station_ids: Optional[set] = None
                          ) -> Tuple[Dict[str, List[Dict[str, str]]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """构造当时对该单元有效的有向图；同时返回被排除的边及原因。

    valid_station_ids 给定时，源点不在该单元有效站集合内的边不计入有效图
    （它对本单元任何基准站都不可用），单独标注，避免“无源边”污染环检测与报告。
    """
    adj: Dict[str, List[Dict[str, str]]] = {rid: [] for rid in parsed["ref_by_id"]}
    active, excluded = [], []
    for ed in parsed["edges"]:
        reason = None
        if uid not in ed["applies_to_units"]:
            reason = "不适用于该单元"
        elif not covers(ed["valid_from"], ed["valid_to"], hour_dt):
            reason = "该小时不在边有效期内（替代到期）"
        elif valid_station_ids is not None and ed["from"] not in valid_station_ids:
            reason = f"源基准站 {ed['from']} 该小时不在本单元有效站集合内，边对本单元不可用"
        if reason:
            excluded.append({"edge_id": ed["id"], "from": ed["from"], "to": ed["to"], "reason": reason})
        else:
            active.append({"edge_id": ed["id"], "from": ed["from"], "to": ed["to"]})
            adj[ed["from"]].append({"to": ed["to"], "edge_id": ed["id"]})
    return adj, active, excluded


def detect_cycle(adj: Dict[str, List[Dict[str, str]]]) -> Optional[List[str]]:
    """DFS 三色环检测；返回环上的站点 id 序列（含重复的起点），无环返回 None。"""
    color: Dict[str, int] = {n: 0 for n in adj}  # 0白 1灰 2黑
    stack: List[str] = []

    def dfs(u: str) -> Optional[List[str]]:
        color[u] = 1
        stack.append(u)
        for e in adj[u]:
            v = e["to"]
            if color.get(v, 0) == 1:
                idx = stack.index(v)
                return stack[idx:] + [v]
            if color.get(v, 0) == 0:
                found = dfs(v)
                if found:
                    return found
        stack.pop()
        color[u] = 2
        return None

    for n in adj:
        if color[n] == 0:
            found = dfs(n)
            if found:
                return found
    return None


def reachable_from(adj: Dict[str, List[Dict[str, str]]], source: str
                   ) -> Dict[str, Dict[str, Any]]:
    """单源 BFS 求有向可达，记录每个目标的一条站点链与边链（边链为空表示长度0自身链）。

    注意不能多源共享 visited 预播种：那会让源点之间的边永远无法扩展，
    从而丢掉跨站替代路径。每个基准站单独 BFS，链也天然属于该站。
    """
    info: Dict[str, Dict[str, Any]] = {source: {"stations": [source], "edges": []}}
    dq: deque = deque([source])
    while dq:
        u = dq.popleft()
        for e in adj[u]:
            v = e["to"]
            if v not in info:
                info[v] = {"stations": info[u]["stations"] + [v],
                           "edges": info[u]["edges"] + [e["edge_id"]]}
                dq.append(v)
    return info


# ----------------------------------------------------------------------------
# 4. 二分图最大匹配（BFS 增广路，含重配见证）+ 残差 BFS 最大性证明
# ----------------------------------------------------------------------------

def maximum_matching(left: List[str], adj_bip: Dict[str, List[str]]
                     ) -> Tuple[Dict[str, str], Dict[str, str], List[Dict[str, Any]]]:
    """
    Kuhn + BFS 增广。按左顶点 id 顺序处理；每次增广的重配链都记录为见证，
    证明算法不会因“先配某站”而把本可满配的单元判不足。
    返回 match_L(站->观测), match_R(观测->站), 增广日志。
    """
    match_L: Dict[str, str] = {}
    match_R: Dict[str, str] = {}
    log: List[Dict[str, Any]] = []

    for u in sorted(left):
        # BFS 在残差图上找 u 的增广路
        prev: Dict[str, Tuple[str, str]] = {}
        seen = {u}
        dq: deque = deque([u])
        found: Optional[str] = None
        while dq and found is None:
            x = dq.popleft()
            for v in sorted(adj_bip.get(x, [])):
                if v in seen:
                    continue
                seen.add(v)
                prev[v] = (x, "fwd")
                if v not in match_R:
                    found = v
                    break
                w = match_R[v]
                prev[w] = (v, "back")
                dq.append(w)
        if found is None:
            log.append({"station": u, "augmented": False,
                        "reason": "残差 BFS 穷尽仍无空闲可达观测（该站在当前最大匹配下无法再增广）"})
            continue
        # 回溯增广路：prev 记录右点->(左点,fwd) 与 左点->(右点,back)。
        # 先把整条增广路的 (左,右) 配对序列算出，再整体替换两个匹配表，
        # 避免只 pop 直接旧主而漏掉链式挤让导致的残留/错误解除。
        pairs: List[Tuple[str, str]] = []
        reassign: List[Dict[str, str]] = []
        cur = found
        while cur in prev:
            nxt, kind = prev[cur]
            if kind == "fwd":  # nxt(左) -> cur(右)
                pairs.append((nxt, cur))
                owner = match_R.get(cur)
                if owner is not None and owner != nxt:
                    reassign.append({"observation": cur, "from_station": owner, "to_station": nxt})
            cur = nxt
        pairs.reverse()
        involved_left = {lft for lft, _ in pairs}
        involved_right = {rgt for _, rgt in pairs}
        # 清掉路上所有旧配对，再按增广路重新落配
        for lft in involved_left:
            match_L.pop(lft, None)
        for rgt in involved_right:
            match_R.pop(rgt, None)
        for lft, rgt in pairs:
            match_R[rgt] = lft
            match_L[lft] = rgt
        log.append({
            "station": u,
            "augmented": True,
            "augmenting_path_observations": [rgt for _, rgt in pairs],
            "reassignments": list(reversed(reassign)),
        })
    return match_L, match_R, log


def maximality_certificate(left: List[str], adj_bip: Dict[str, List[str]],
                           match_L: Dict[str, str], match_R: Dict[str, str]
                           ) -> Dict[str, Any]:
    """
    残差多源 BFS（从所有未匹配左点出发）：
      Z = 可达点集；K = (L\\Z_L) ∪ (R∩Z_R) 即最小顶点覆盖（Kőnig），
      |K| = 最大匹配数 => 不可增广的可核验证明。
      Hall 见证：Z_L 中未匹配站构成的亏缺说明。
    """
    reachable_L = set(u for u in left if u not in match_L)
    reachable_R: set = set()
    dq: deque = deque(("L", u) for u in sorted(reachable_L))
    while dq:
        side, node = dq.popleft()
        if side == "L":
            for v in adj_bip.get(node, []):
                if v not in reachable_R:
                    reachable_R.add(v)
                    dq.append(("R", v))
        else:
            owner = match_R.get(node)
            if owner is not None and owner not in reachable_L:
                reachable_L.add(owner)
                dq.append(("L", owner))
    cover_L = sorted(set(left) - reachable_L)
    cover_R = sorted(reachable_R)
    unmatched = sorted(u for u in left if u not in match_L)
    certificate = {
        "unmatched_stations": unmatched,
        "deficiency": len(unmatched),
        "residual_reachable_left": sorted(reachable_L),
        "residual_reachable_right": sorted(reachable_R),
        "min_vertex_cover": {"left_stations": cover_L, "right_observations": cover_R},
        "cover_size": len(cover_L) + len(cover_R),
        "matching_size": len(match_L),
        "hall_witness": None,
    }
    if unmatched:
        zl = sorted(reachable_L)
        neighbor_R = sorted({v for u in zl for v in adj_bip.get(u, [])})
        certificate["hall_witness"] = {
            "station_subset_ZL": zl,
            "neighbor_observations_N_ZL": neighbor_R,
            "inequality": f"|N(Z_L)| = {len(neighbor_R)} < |Z_L| = {len(zl)}",
            "explanation": "Hall 条件被违反：该站集的全部可达合格观测数少于站数，"
                           "故至少有 " + str(max(0, len(zl) - len(neighbor_R))) + " 个站无法被认领；"
                           "最小顶点覆盖规模等于匹配规模，证明匹配已最大、不可增广。",
        }
    return certificate


# ----------------------------------------------------------------------------
# 5. 核算主流程
# ----------------------------------------------------------------------------

def threshold_for(parsed: Dict[str, Any], uid: str, hk: str) -> Optional[int]:
    if (uid, hk) in parsed["thresholds"]:
        return parsed["thresholds"][(uid, hk)]
    return parsed["thresholds"].get((uid, None))


def evaluate(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for hk in sorted(parsed["hours"]):
        hour_dt = parse_dt(hk, "hour")
        for uid in sorted(parsed["unit_by_id"]):
            unit = parsed["unit_by_id"][uid]

            # 5.1 该小时在单元内有效的基准站
            effective_stations, stations_outside, stations_expired = [], [], []
            for rid, st in parsed["ref_by_id"].items():
                in_poly = point_in_polygon(st["position"], unit["polygon"])
                in_time = covers(st["valid_from"], st["valid_to"], hour_dt)
                if in_poly and in_time:
                    effective_stations.append(rid)
                elif in_poly and not in_time:
                    stations_expired.append(rid)
                elif not in_poly and in_time:
                    stations_outside.append(rid)
            effective_stations.sort()

            # 5.2 有效替代边图 + 环检测（资料错误）
            adj_dir, active_edges, excluded_edges = build_effective_graph(
                parsed, hour_dt, uid, valid_station_ids=set(effective_stations))
            cyc = detect_cycle(adj_dir)
            if cyc is not None:
                raise InputError(
                    f"资料错误：{hk} 单元 {uid} 的有效替代边图存在有向环: "
                    + " -> ".join(cyc) + "（替代关系必须是有向无环链）"
                )

            # 5.3 该小时物理观测的资格筛选
            usable, excluded_obs = [], []
            for oid, ob in sorted(parsed["obs_by_id"].items()):
                if ob["hour"] != hk:
                    continue
                if not ob["quality_ok"]:
                    excluded_obs.append({"observation_id": oid, "station": ob["station"],
                                         "reason": f"质量 {ob['quality']!r} 不合格（零值本身可用，质量标记不合格才排除）"})
                    continue
                if ob["station"] not in parsed["ref_by_id"]:
                    excluded_obs.append({"observation_id": oid, "station": ob["station"],
                                         "reason": "实际站点不是已知基准站，无法进入替代链"})
                    continue
                usable.append(ob)

            # 5.4 有向可达 => 二分图邻接（保留完整有向链见证）
            # 约定：基准站 id 与其物理站点 id 同名；替代边与观测均以物理站点 id 寻址。
            # 每个基准站单独 BFS：替代关系严格按方向，绝不压成无向等价组。
            adj_bip: Dict[str, List[str]] = {rid: [] for rid in effective_stations}
            edge_witness: Dict[Tuple[str, str], Dict[str, Any]] = {}
            for rid in effective_stations:
                reach = reachable_from(adj_dir, rid)
                for ob in usable:
                    target_phys = ob["station"]
                    # 长度 0 自身链：原站物理观测服务自己，无需任何替代边
                    if target_phys == rid:
                        chain = {"stations": [rid], "edges": [], "kind": "自身观测(长度0链)"}
                    elif target_phys in reach:
                        chain = {"stations": reach[target_phys]["stations"],
                                 "edges": reach[target_phys]["edges"], "kind": "替代链"}
                    else:
                        continue
                    adj_bip[rid].append(ob["id"])
                    edge_witness[(rid, ob["id"])] = chain
                adj_bip[rid].sort()

            # 5.5 最大匹配
            match_L, match_R, aug_log = maximum_matching(effective_stations, adj_bip)
            cert = maximality_certificate(effective_stations, adj_bip, match_L, match_R)

            assignments = []
            for rid in sorted(match_L):
                oid = match_L[rid]
                ob = parsed["obs_by_id"][oid]
                chain = edge_witness[(rid, oid)]
                assignments.append({
                    "reference_station": rid,
                    "reference_station_name": parsed["ref_by_id"][rid]["name"],
                    "physical_observation_id": oid,
                    "actual_station": ob["station"],
                    "time": ob["time"],
                    "quality": ob["quality"],
                    "value": ob["value"],
                    "chain_kind": chain["kind"],
                    "directed_chain_stations": chain["stations"],
                    "directed_chain_edges": chain["edges"],
                })

            need = threshold_for(parsed, uid, hk)
            achieved = len(match_L)
            met = None if need is None else achieved >= need

            results.append({
                "hour": hk,
                "unit_id": uid,
                "unit_name": unit["name"],
                "effective_stations": effective_stations,
                "stations_outside_polygon": sorted(stations_outside),
                "stations_expired_at_hour": sorted(stations_expired),
                "active_substitution_edges": active_edges,
                "excluded_substitution_edges": excluded_edges,
                "usable_observations": [{"id": o["id"], "station": o["station"],
                                         "time": o["time"], "quality": o["quality"], "value": o["value"]}
                                        for o in usable],
                "excluded_observations": excluded_obs,
                "bipartite_adjacency": adj_bip,
                "maximum_provable_coverage": achieved,
                "effective_station_count": len(effective_stations),
                "assignments": assignments,
                "augmentation_log": aug_log,
                "maximality_certificate": cert,
                "threshold_required": need,
                "threshold_met": met,
            })
    return results


# ----------------------------------------------------------------------------
# 6. 输出：机器可读 JSON + 人读 Markdown + 版本清单
# ----------------------------------------------------------------------------

def render_markdown(parsed: Dict[str, Any], results: List[Dict[str, Any]]) -> str:
    L: List[str] = []
    L.append(f"# 气象风险单元观测覆盖核算报告（输入版本 {parsed['version']}）")
    if parsed["description"]:
        L.append("")
        L.append(parsed["description"])
    if parsed["restates"]:
        L.append("")
        L.append(f"> 本版为重述版，替代旧版本: {', '.join(parsed['restates'])}；旧版本输出原样保留。")
    if parsed["notes"]:
        L.append("")
        L.append("## 场景说明")
        for n in parsed["notes"]:
            L.append(f"- {n}")

    for r in results:
        L.append("")
        L.append("---")
        L.append("")
        L.append(f"## 小时 {r['hour']} ｜ 单元 {r['unit_id']}（{r['unit_name']}）")
        L.append("")
        L.append(f"- 有效基准站（在多边形内且有效期覆盖该小时）: "
                 f"{', '.join(r['effective_stations']) or '无'}（共 {r['effective_station_count']} 个）")
        if r["stations_outside_polygon"]:
            L.append(f"- 在单元多边形外、不计入: {', '.join(r['stations_outside_polygon'])}")
        if r["stations_expired_at_hour"]:
            L.append(f"- 基准站有效期未覆盖该小时: {', '.join(r['stations_expired_at_hour'])}")
        if r["active_substitution_edges"]:
            L.append("- 当时对本单元有效的有向替代边: "
                     + ", ".join(f"{e['from']}→{e['to']}[{e['edge_id']}]" for e in r["active_substitution_edges"]))
        if r["excluded_substitution_edges"]:
            L.append("- 未生效替代边: "
                     + "; ".join(f"{e['from']}→{e['to']}[{e['edge_id']}]（{e['reason']}）"
                                 for e in r["excluded_substitution_edges"]))
        if r["usable_observations"]:
            L.append("- 合格物理观测: "
                     + ", ".join(f"{o['id']}(站{o['station']}, 值={o['value']}, 质量={o['quality']})"
                                 for o in r["usable_observations"]))
        else:
            L.append("- 合格物理观测: 无（缺测无观测；零值是合法值，不算缺测）")
        for ex in r["excluded_observations"]:
            L.append(f"- 排除观测 {ex['observation_id']}: {ex['reason']}")

        L.append("")
        L.append("### 二分图邻接（基准站 → 可认领观测；方向链已保留，未压成等价组）")
        for rid in r["effective_stations"]:
            L.append(f"- {rid} → {', '.join(r['bipartite_adjacency'].get(rid, [])) or '（无可达合格观测）'}")

        L.append("")
        L.append(f"### 最大可证明覆盖数: **{r['maximum_provable_coverage']}** / "
                 f"有效基准站 {r['effective_station_count']} 个")
        if r["threshold_required"] is None:
            L.append("- 阈值判定: 该单元/小时未配置阈值")
        else:
            verdict = "✅ 达到阈值" if r["threshold_met"] else "❌ 未达到阈值"
            L.append(f"- 阈值要求: {r['threshold_required']} 个独立来源 → {verdict}")

        L.append("")
        L.append("### 匹配见证（分配关系 + 有向链）")
        if r["assignments"]:
            for a in r["assignments"]:
                chain = " → ".join(a["directed_chain_stations"])
                edges = f"，经边 {a['directed_chain_edges']}" if a["directed_chain_edges"] else ""
                L.append(f"- 基准站 **{a['reference_station']}** 认领物理观测 "
                         f"**{a['physical_observation_id']}**（实际站 {a['actual_station']}，"
                         f"时间 {a['time']}，值={a['value']}，质量={a['quality']}）；"
                         f"{a['chain_kind']}: {chain}{edges}")
        else:
            L.append("- 无任何认领（全部站缺测或不可达）")

        L.append("")
        L.append("### 增广过程见证（证明非贪心、可重配）")
        for e in r["augmentation_log"]:
            if e["augmented"]:
                msg = f"站 {e['station']} 增广成功，增广路观测序列 {e['augmenting_path_observations']}"
                if e["reassignments"]:
                    msg += "；重配: " + ", ".join(
                        f"{x['observation']} 由 {x['from_station']} 改配 {x['to_station']}"
                        for x in e["reassignments"])
                L.append(f"- {msg}")
            else:
                L.append(f"- 站 {e['station']}: {e['reason']}")

        c = r["maximality_certificate"]
        L.append("")
        L.append("### 最大性可核验依据")
        L.append(f"- 匹配规模 = {c['matching_size']}；最小顶点覆盖规模 = {c['cover_size']}"
                 "（二者相等，由 Kőnig 定理保证匹配最大、无增广路）")
        L.append(f"- 最小顶点覆盖: 左点 {c['min_vertex_cover']['left_stations'] or '∅'}，"
                 f"右点 {c['min_vertex_cover']['right_observations'] or '∅'}")
        L.append(f"- 残差可达左点: {c['residual_reachable_left'] or '∅'}；"
                 f"残差可达右点: {c['residual_reachable_right'] or '∅'}")
        if c["unmatched_stations"]:
            L.append(f"- **未匹配基准站: {', '.join(c['unmatched_stations'])}**（亏缺 {c['deficiency']} 个独立来源）")
            hw = c["hall_witness"]
            L.append(f"- Hall 见证: 站集 Z_L={hw['station_subset_ZL']}，"
                     f"其全部可达合格观测 N(Z_L)={hw['neighbor_observations_N_ZL']}，{hw['inequality']}")
            L.append(f"  - {hw['explanation']}")
        else:
            L.append("- 所有有效基准站均已认领观测（无未匹配站）。")
    L.append("")
    L.append("---")
    L.append("")
    L.append("### 跨单元独立核算声明")
    L.append("同一物理观测在不同单元/小时的认领互不相加；每个「单元×小时」独立求最大匹配，"
             "独立来源数只按本单元本小时的匹配边计。零值观测可用；缺测（无观测）不产生右顶点。")
    return "\n".join(L) + "\n"


def write_outputs(parsed: Dict[str, Any], results: List[Dict[str, Any]],
                  input_path: str, out_root: str) -> Tuple[str, str, str]:
    vdir = os.path.join(out_root, parsed["version"])
    if os.path.exists(vdir) and os.listdir(vdir):
        raise InputError(f"输出版本目录已存在且非空，拒绝覆盖: {vdir}（版本必须不可变，修订请用新输入版重述）")
    os.makedirs(vdir, exist_ok=True)

    machine = {
        "schema": "met-coverage/v1",
        "input_version": parsed["version"],
        "description": parsed["description"],
        "restates": parsed["restates"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_file": {"path": os.path.abspath(input_path), "sha256": sha256_file(input_path)},
        "results": results,
    }
    json_path = os.path.join(vdir, "coverage.json")
    md_path = os.path.join(vdir, "coverage.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(machine, fh, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_markdown(parsed, results))

    manifest = {
        "version": parsed["version"],
        "restates": parsed["restates"],
        "input_sha256": machine["input_file"]["sha256"],
        "outputs": [os.path.basename(json_path), os.path.basename(md_path)],
        "unit_hour_cells": len(results),
        "cells_below_threshold": [
            {"hour": r["hour"], "unit_id": r["unit_id"],
             "achieved": r["maximum_provable_coverage"], "required": r["threshold_required"]}
            for r in results if r["threshold_met"] is False
        ],
        "generated_at": machine["generated_at"],
        "prior_versions_preserved": sorted(
            d for d in os.listdir(out_root)
            if os.path.isdir(os.path.join(out_root, d)) and d != parsed["version"]
        ),
    }
    manifest_path = os.path.join(vdir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    return json_path, md_path, manifest_path


# ----------------------------------------------------------------------------
# 7. CLI
# ----------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="按小时/单元核算气象风险单元最大可证明观测覆盖数")
    ap.add_argument("--input", required=True, help="输入 JSON 路径（含版本、站、观测、单元、核准替代边、阈值）")
    ap.add_argument("--out-root", required=True, help="输出根目录；每个输入版在其下建不可变版本子目录")
    args = ap.parse_args(argv)

    try:
        data = load_input(args.input)
        parsed = validate(data)
        results = evaluate(parsed)
        json_path, md_path, manifest_path = write_outputs(parsed, results, args.input, args.out_root)
    except InputError as exc:
        print(f"[拒绝出版本] {exc}", file=sys.stderr)
        return 2

    below = [r for r in results if r["threshold_met"] is False]
    print(f"版本 {parsed['version']} 核算完成：共 {len(results)} 个「单元×小时」，"
          f"未达阈值 {len(below)} 个")
    for r in results:
        verdict = "无阈值" if r["threshold_required"] is None else ("达标" if r["threshold_met"] else "不足")
        print(f"  {r['hour']} | {r['unit_id']}: 覆盖 {r['maximum_provable_coverage']}/"
              f"{r['effective_station_count']}，阈值 {r['threshold_required']} -> {verdict}")
    print(f"输出: {json_path}\n      {md_path}\n      {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
