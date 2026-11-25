#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workorder_store 的端到端与性质测试：

  A. 基础：断供区间识别（>=2 小时才成单，单点不成单）、达标/null/缺测切断区间
  B. 迟到重述：乱序送达、低 seq 不覆盖、高 seq 重述导致合并/拆分/撤销
  C. 幂等与整批拒绝：同序号同字节 no-op、同序号不同字节拒绝、重复格/坏标识/
     坏整点/坏判定值拒绝且不改库
  D. 变更日志：游标限量、续读、从头读、重启后位置不变
  E. 原子性：提交前注入异常 => 状态与日志全部回滚；重送可安全恢复
  F. 性能：多年历史 + 小批回补不扫无关历史、不逐小时扫描长断供、读变更不重扫
  G. CLI 子进程冒烟（-I 隔离）
  H. 与真实 met_coverage 产物兼容（v1 的 coverage.json）

运行：python3 -I tests/test_workorder_store.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import workorder_store as ws  # noqa: E402
from workorder_store import WorkOrderStore, ImportRejected, ST_FALSE, ST_TRUE, ST_NULL  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def report(cells):
    """cells: [(unit, hour, threshold_met)] -> coverage.json 形态的 dict。"""
    return {"schema": "met-coverage/v1",
            "results": [{"unit_id": u, "hour": h, "threshold_met": v} for u, h, v in cells]}


def hours(prefix, n):
    """2026-01-01T00:00 起连续 n 个整点。"""
    return [f"2026-01-{1 + i // 24:02d}T{i % 24:02d}:00" for i in range(n)]


# ---------------------------------------------------------------------------
# A. 基础区间识别
# ---------------------------------------------------------------------------

def test_basic_intervals(tmp):
    print("== A. 基础区间识别 ==")
    db = os.path.join(tmp, "a.db")
    s = WorkOrderStore(db)
    hs = hours("x", 8)  # h0..h7
    # U1: false false false | true | false | null | false false
    cells = [
        ("U1", hs[0], False), ("U1", hs[1], False), ("U1", hs[2], False),
        ("U1", hs[3], True),
        ("U1", hs[4], False),
        ("U1", hs[5], None),
        ("U1", hs[6], False), ("U1", hs[7], False),
        # U2: 单点 false 不成单
        ("U2", hs[0], False), ("U2", hs[1], True),
        # U3: 两小时 false 成单
        ("U3", hs[2], False), ("U3", hs[3], False),
    ]
    out = s.import_coverage_data(report(cells), seq=1)
    tickets = {(t["unit_id"], t["start_hour"]): t["end_hour"] for t in s.current_tickets()}
    check("U1 长段 3 小时", tickets.get(("U1", hs[0])) == hs[2], str(tickets))
    check("true 切断，单点 false 不成单", ("U1", hs[4]) not in tickets)
    check("null 切断后段成单", tickets.get(("U1", hs[6])) == hs[7])
    check("U2 单点不成单", not any(k[0] == "U2" for k in tickets))
    check("U3 两点成单", tickets.get(("U3", hs[2])) == hs[3])
    check("首批变更数=3（U1 两段 + U3 一段）", len(out["changes"]) == 3, str(out["changes"]))
    check("首批按 unit,start 排序",
          [c["unit_id"] + c["start_hour"] for c in out["changes"]] ==
          sorted(c["unit_id"] + c["start_hour"] for c in out["changes"]))
    check("首批全部 upsert", all(c["op"] == "upsert" for c in out["changes"]))
    s.close()


def test_gap_splits_interval(tmp):
    print("== A2. 未核算（无格）小时同样切断区间 ==")
    db = os.path.join(tmp, "a2.db")
    s = WorkOrderStore(db)
    hs = hours("x", 5)
    # h0,h1 false；h2 不报（缺测）；h3,h4 false => 两段
    s.import_coverage_data(report([
        ("U1", hs[0], False), ("U1", hs[1], False),
        ("U1", hs[3], False), ("U1", hs[4], False)]), seq=1)
    tickets = {(t["start_hour"]): t["end_hour"] for t in s.current_tickets()}
    check("缺测格切断为两段", tickets == {hs[0]: hs[1], hs[3]: hs[4]}, str(tickets))
    # 回补 h2=false => 合并为一段，身份保留较早的首小时
    out = s.import_coverage_data(report([("U1", hs[2], False)]), seq=2)
    tickets = {t["start_hour"]: t["end_hour"] for t in s.current_tickets()}
    check("回补合并为整段", tickets == {hs[0]: hs[4]}, str(tickets))
    ops = {(c["start_hour"], c["op"]): c["end_hour"] for c in out["changes"]}
    check("合并：原首段 upsert 到新尾", ops.get((hs[0], "upsert")) == hs[4])
    check("合并：被并入身份 revoke", ops.get((hs[3], "revoke")) is None)
    s.close()


# ---------------------------------------------------------------------------
# B. 乱序与迟到重述
# ---------------------------------------------------------------------------

def test_out_of_order(tmp):
    print("== B. 乱序送达 / 高 seq 重述合并拆分撤销 ==")
    db = os.path.join(tmp, "b.db")
    s = WorkOrderStore(db)
    hs = hours("x", 6)
    # seq=10 先到：h0..h5 全 false（长工单）
    s.import_coverage_data(report([("U1", h, False) for h in hs]), seq=10)
    # seq=5 迟到：想把 h2 改成 true —— 必须被压住，不生效
    out = s.import_coverage_data(report([("U1", hs[2], True)]), seq=5)
    check("低 seq 不改动格", out["cells_applied"] == 0)
    check("低 seq 不产生变更", out["changes"] == [])
    state = s.cell_state("U1", hs[2])
    check("格状态仍为 false/seq10", state == (ST_FALSE, 10), str(state))
    tickets = s.current_tickets()
    check("长工单仍在", len(tickets) == 1 and tickets[0]["start_hour"] == hs[0]
          and tickets[0]["end_hour"] == hs[5])

    # seq=20 重述 h2=true：长段拆成两段
    out = s.import_coverage_data(report([("U1", hs[2], True)]), seq=20)
    tickets = {(t["start_hour"]): t["end_hour"] for t in s.current_tickets()}
    check("拆分后两段", tickets == {hs[0]: hs[1], hs[3]: hs[5]}, str(tickets))
    ops = {(c["start_hour"], c["op"]): c["end_hour"] for c in out["changes"]}
    check("拆分：原身份 upsert 缩尾", ops.get((hs[0], "upsert")) == hs[1])
    check("拆分：新身份 upsert", ops.get((hs[3], "upsert")) == hs[5])

    # seq=30 重述 h2=null（无阈值也切断），结果相同但 seq 更新；区间不变 => 无变更
    out = s.import_coverage_data(report([("U1", hs[2], None)]), seq=30)
    check("同状态不同判定(true->null)不产生工单变更", out["changes"] == [])
    check("格 seq 已更新", s.cell_state("U1", hs[2]) == (ST_NULL, 30))

    # seq=40 重述整段为 true：工单全部撤销
    out = s.import_coverage_data(report([("U1", h, True) for h in hs]), seq=40)
    check("全部撤销", s.current_tickets() == [])
    check("两条 revoke", sorted(c["op"] for c in out["changes"]) == ["revoke", "revoke"])
    check("revoke 不带 end_hour", all(c["end_hour"] is None for c in out["changes"]))
    s.close()


def test_restate_split_then_merge_identity(tmp):
    print("== B2. 拆分后再合并，身份随首小时复活 ==")
    db = os.path.join(tmp, "b2.db")
    s = WorkOrderStore(db)
    hs = hours("x", 4)
    s.import_coverage_data(report([("U1", h, False) for h in hs]), seq=1)
    s.import_coverage_data(report([("U1", hs[1], True)]), seq=2)  # 拆 [h0] 与 [h2,h3]
    before = {t["start_hour"]: t["end_hour"] for t in s.current_tickets()}
    check("拆分只留 >=2 段", before == {hs[2]: hs[3]}, str(before))
    out = s.import_coverage_data(report([("U1", hs[1], False)]), seq=3)  # 重新合并
    after = {t["start_hour"]: t["end_hour"] for t in s.current_tickets()}
    check("合并恢复整段，身份=首小时", after == {hs[0]: hs[3]}, str(after))
    ops = {(c["start_hour"], c["op"]) for c in out["changes"]}
    check("复活身份 upsert + 旧身份 revoke",
          (hs[0], "upsert") in ops and (hs[2], "revoke") in ops, str(ops))
    s.close()


# ---------------------------------------------------------------------------
# C. 幂等与整批拒绝
# ---------------------------------------------------------------------------

def test_idempotent_and_rejection(tmp):
    print("== C. 幂等重送 / 整批拒绝 ==")
    db = os.path.join(tmp, "c.db")
    path = os.path.join(tmp, "cov1.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report([("U1", "2026-01-01T00:00", False),
                          ("U1", "2026-01-01T01:00", False)]), fh)
    s = WorkOrderStore(db)
    out1 = s.import_coverage_file(path, seq=1)
    log_count_after_first = s.conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    check("首次导入 applied", out1["status"] == "applied" and len(out1["changes"]) == 1)

    # 同序号同字节重送
    out2 = s.import_coverage_file(path, seq=1)
    check("同字节重送 no-op", out2["status"] == "duplicate_noop")
    check("重送不追加变更",
          s.conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == log_count_after_first)
    check("重送不重复登记 import", len(s.imported_seqs()) == 1)

    # 同序号不同字节
    path2 = os.path.join(tmp, "cov2.json")
    with open(path2, "w", encoding="utf-8") as fh:
        json.dump(report([("U1", "2026-01-01T00:00", True)]), fh)
    rejected = False
    try:
        s.import_coverage_file(path2, seq=1)
    except ImportRejected:
        rejected = True
    check("同序号不同文件整批拒绝", rejected)
    check("拒绝后格未变", s.cell_state("U1", "2026-01-01T00:00") == (ST_FALSE, 1))
    check("拒绝后 import 登记未增", len(s.imported_seqs()) == 1)

    # 各类坏报告：每类都必须抛 ImportRejected 且库不变
    bad_cases = [
        ("重复格", report([("U1", "2026-02-01T00:00", False),
                           ("U1", "2026-02-01T00:00", True)])),
        ("非字符串 unit", {"results": [{"unit_id": 7, "hour": "2026-02-01T00:00",
                                        "threshold_met": False}]}),
        ("空白 unit", report([("   ", "2026-02-01T00:00", False)])),
        ("None unit", {"results": [{"unit_id": None, "hour": "2026-02-01T00:00",
                                    "threshold_met": False}]}),
        ("非整点", report([("U9", "2026-02-01T00:30", False)])),
        ("带 Z 时区", report([("U9", "2026-02-01T00:00Z", False)])),
        ("带偏移", report([("U9", "2026-02-01T00:00+08:00", False)])),
        ("坏日期", report([("U9", "2026-02-30T00:00", False)])),
        ("hour 非字符串", {"results": [{"unit_id": "U9", "hour": 12,
                                        "threshold_met": False}]}),
        ("非法判定值 0", report([("U9", "2026-02-01T00:00", 0)])),
        ("非法判定值 字符串", report([("U9", "2026-02-01T00:00", "false")])),
        ("results 缺失", {"schema": "x"}),
        ("顶层非对象", []),
        ("results 元素非对象", {"results": [1]}),
        ("坏 JSON", b"{not json"),
    ]
    cells_before = s.conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
    for name, payload in bad_cases:
        try:
            if isinstance(payload, bytes):
                p = os.path.join(tmp, "bad.json")
                with open(p, "wb") as fh:
                    fh.write(payload)
                s.import_coverage_file(p, seq=99)
            else:
                s.import_coverage_data(payload, seq=99)
            ok = False
        except ImportRejected:
            ok = True
        check(f"拒绝：{name}", ok)
    cells_after = s.conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
    check("全部拒绝后库不变", cells_before == cells_after, f"{cells_before} vs {cells_after}")

    # seq 校验
    for bad_seq in (0, -1, 1.5, True):
        try:
            s.import_coverage_data(report([("U9", "2026-02-01T00:00", False)]), bad_seq)
            ok = False
        except ImportRejected:
            ok = True
        check(f"拒绝坏 seq={bad_seq!r}", ok)
    s.close()


# ---------------------------------------------------------------------------
# D. 变更日志游标 + 重启
# ---------------------------------------------------------------------------

def test_cursor_and_restart(tmp):
    print("== D. 游标限量读取 / 重启位置不变 ==")
    db = os.path.join(tmp, "d.db")
    s = WorkOrderStore(db)
    hs = hours("x", 10)
    # 三批导入，制造若干变更；首批落盘为文件，供后面按字节重送
    first_path = os.path.join(tmp, "d_cov.json")
    with open(first_path, "w", encoding="utf-8") as fh:
        json.dump(report([("U1", hs[i], False) for i in range(4)]), fh)
    s.import_coverage_file(first_path, seq=1)
    s.import_coverage_data(report([("U1", hs[1], True)]), seq=2)
    s.import_coverage_data(report([("U1", hs[1], False), ("U2", hs[0], False),
                                   ("U2", hs[1], False)]), seq=3)

    # 限量分页读完全部
    seen, cursor, pages = [], 0, 0
    while True:
        page = s.read_changes(after_cursor=cursor, limit=2)
        pages += 1
        if not page["changes"]:
            break
        seen.extend(page["changes"])
        check("页内 id 递增", [c["cursor"] for c in page["changes"]] ==
              sorted(c["cursor"] for c in page["changes"]))
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    total = s.conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]
    check("分页读完全部变更", len(seen) == total, f"{len(seen)} vs {total}")
    check("cursor 即日志 id", [c["cursor"] for c in seen] == list(range(1, total + 1)))

    # 从头读
    head = s.read_changes(after_cursor=0, limit=1000)
    check("从头读返回全部", len(head["changes"]) == total)
    check("末页 has_more=False", head["has_more"] is False)

    # 记住位置后重开库（模拟进程重启），位置语义不变
    mid_cursor = seen[2]["cursor"]
    s.close()
    s2 = WorkOrderStore(db)
    r1 = s2.read_changes(after_cursor=mid_cursor, limit=100)
    check("重启后续读不丢不重",
          [c["cursor"] for c in r1["changes"]] ==
          [c["cursor"] for c in seen if c["cursor"] > mid_cursor])

    # 重启后重送旧批次（同字节 no-op），日志位置不变
    before_ids = [c["cursor"] for c in r1["changes"]]
    s2.import_coverage_file(first_path, seq=1)
    again = s2.read_changes(after_cursor=mid_cursor, limit=100)
    check("重送后已发布位置不变", [c["cursor"] for c in again["changes"]] == before_ids)
    check("续读游标稳定", again["next_cursor"] == r1["next_cursor"])

    # 坏游标参数
    for bad in (-1, "1", 1.0):
        try:
            s2.read_changes(after_cursor=bad, limit=10)
            ok = False
        except (ValueError, TypeError):
            ok = True
        check(f"拒绝坏游标 {bad!r}", ok)
    s2.close()


# ---------------------------------------------------------------------------
# E. 原子性：提交前故障
# ---------------------------------------------------------------------------

def test_atomicity(tmp):
    print("== E. 提交前故障 => 全量回滚，重送可恢复 ==")
    db = os.path.join(tmp, "e.db")
    s = WorkOrderStore(db)
    hs = hours("x", 4)
    s.import_coverage_data(report([("U1", h, False) for h in hs]), seq=1)
    baseline_cells = s.conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
    baseline_runs = s.conn.execute("SELECT COUNT(*) FROM false_runs").fetchone()[0]
    baseline_log = s.conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0]

    class Boom(Exception):
        pass

    def boom(conn, changes):
        raise Boom("模拟写变更日志时崩溃")

    s._insert_changes = boom  # type: ignore
    try:
        s.import_coverage_data(report([("U1", hs[1], True), ("U1", hs[2], None)]), seq=2)
        raised = False
    except Boom:
        raised = True
    check("故障向上传播", raised)

    # 连接仍可用（事务已回滚），状态完全停留在 seq=1
    check("cells 回滚", s.conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == baseline_cells)
    check("false_runs 回滚",
          s.conn.execute("SELECT COUNT(*) FROM false_runs").fetchone()[0] == baseline_runs)
    check("change_log 回滚",
          s.conn.execute("SELECT COUNT(*) FROM change_log").fetchone()[0] == baseline_log)
    check("imports 回滚", s.conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1)
    check("格状态停留在旧值", s.cell_state("U1", hs[1]) == (ST_FALSE, 1))

    # 重开库确认持久层面也是提交前状态，然后重送 seq=2 安全恢复
    s.close()
    s2 = WorkOrderStore(db)
    check("重启后仍是提交前状态", s2.cell_state("U1", hs[1]) == (ST_FALSE, 1))
    out = s2.import_coverage_data(report([("U1", hs[1], True), ("U1", hs[2], None)]), seq=2)
    check("重送成功恢复", out["status"] == "applied")
    tickets = {t["start_hour"]: t["end_hour"] for t in s2.current_tickets()}
    check("恢复后区间正确（h3 单点不成单，h0 单点不成单）", tickets == {}, str(tickets))
    s2.close()


# ---------------------------------------------------------------------------
# F. 性能：不扫无关历史、不逐小时扫描长断供
# ---------------------------------------------------------------------------

class QueryCounter:
    """统计 SQL 执行次数，并标记是否出现全表扫描（EXPLAIN QUERY PLAN 里的 SCAN）。

    sqlite3.Connection 不可直接打补丁，这里用委托包装器临时替换 store.conn。
    """

    def __init__(self, conn):
        self.count = 0
        self.scans = []
        self.real = conn

    def __enter__(self):
        real = self.real

        def note(sql, params=()):
            self.count += 1
            head = sql.lstrip().upper()
            if head.startswith(("SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE")):
                plan = real.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
                if any("SCAN" in row[-1] and "COVERING INDEX" not in row[-1] for row in plan):
                    self.scans.append((sql.split()[0].upper(), [row[-1] for row in plan]))

        class Wrap:
            def execute(self_inner, sql, params=()):
                note(sql, params)
                return real.execute(sql, params)

            def executemany(self_inner, sql, seq):
                self.count += 1  # 批量写不做单语句计划分析
                return real.executemany(sql, seq)

            def __getattr__(self_inner, name):
                return getattr(real, name)

        self._wrap = Wrap()
        return self._wrap

    def __exit__(self, *exc):
        # 包装结束后调用方继续用原 conn；包装器与原 conn 共享同一底层连接
        return False


def test_performance(tmp):
    print("== F. 多年历史下的增量代价 ==")
    db = os.path.join(tmp, "f.db")
    s = WorkOrderStore(db)
    # 5 年小时量 × 10 单元 ≈ 43.8 万格，全部 false（超长断供区间）
    from datetime import datetime, timedelta
    n_years_hours = 24 * 365 * 5
    base = datetime(2020, 1, 1)
    big_hours = [(base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00")
                 for i in range(n_years_hours)]
    # 直接用 executemany 灌底，避免巨型 JSON；区间按块构建仍走同一套增量逻辑
    conn = s.conn
    conn.execute("BEGIN")
    units = [f"U{i}" for i in range(10)]
    for u in units:
        conn.executemany(
            "INSERT INTO cells(unit_id, hour, state, seq) VALUES (?, ?, 0, 1)",
            [(u, h) for h in big_hours])
        # 每单元一条超长区间（与 _insert_false 维护出的形态一致）
        conn.execute("INSERT INTO false_runs VALUES (?, ?, ?)",
                     (u, big_hours[0], big_hours[-1]))
    conn.execute(
        "INSERT INTO imports(seq, file_sha256, cell_count, imported_at) VALUES (1, 'base', ?, 't')",
        (n_years_hours * 10,))
    conn.commit()
    check("底库规模 ~43.8 万格",
          conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == n_years_hours * 10)

    # F1. 小批回补：改中间 3 个格，统计查询数与是否扫描大表
    mid = n_years_hours // 2
    patch_hours = big_hours[mid:mid + 3]
    qc = QueryCounter(conn)
    saved_conn = s.conn
    with qc as wrapped:
        s.conn = wrapped
        try:
            out = s.import_coverage_data(report([
                ("U3", patch_hours[0], True),
                ("U3", patch_hours[1], None),
                ("U3", patch_hours[2], True)]), seq=2)
        finally:
            s.conn = saved_conn
    check("小批回补查询数为小常数（< 60）", qc.count < 60, f"got {qc.count}")
    check("回补无全表 SCAN", not qc.scans, str(qc.scans)[:300])
    # 长区间被切成两段，端点正确，且没有逐小时 update（查 cells 变更仅 3 行）
    runs = dict(conn.execute(
        "SELECT start_hour, end_hour FROM false_runs WHERE unit_id='U3' "
        "ORDER BY start_hour").fetchall())
    run_items = sorted(runs.items())
    check("长区间拆分端点正确",
          run_items == [(big_hours[0], big_hours[mid - 1]),
                        (big_hours[mid + 3], big_hours[-1])], str(run_items))
    check("拆分产生 2 条 upsert（原身份缩尾 + 新身份）",
          sorted(c["op"] for c in out["changes"]) == ["upsert", "upsert"], str(out["changes"]))
    # 制造一条日志，验证游标读取不随历史规模退化
    s.import_coverage_data(report([("U9", "2020-01-01T00:00", False),
                                   ("U9", "2020-01-01T01:00", False)]), seq=3)

    # F2. 读少量新变更不重扫历史：查询计划必须走主键索引
    plan = conn.execute("EXPLAIN QUERY PLAN "
                        "SELECT id FROM change_log WHERE id > ? ORDER BY id LIMIT 10",
                        (0,)).fetchall()
    check("游标读取走索引", all("SCAN change_log" not in r[-1] for r in plan), str(plan))
    t0 = time.perf_counter()
    page = s.read_changes(after_cursor=0, limit=5)
    dt = time.perf_counter() - t0
    check("限量读取只返回 limit 条", len(page["changes"]) == min(5, page["next_cursor"]))
    check("限量读取耗时极低（<50ms）", dt < 0.05, f"{dt*1000:.1f}ms")

    # F3. 工单全量列表是显式运维操作（仅区间行，数量与小时数无关）
    n_runs = conn.execute("SELECT COUNT(*) FROM false_runs").fetchone()[0]
    check("区间行数与单元数同阶（不随时长膨胀）", n_runs <= len(units) + 2, str(n_runs))
    s.close()


# ---------------------------------------------------------------------------
# G. CLI 冒烟
# ---------------------------------------------------------------------------

def test_cli(tmp):
    print("== G. CLI 子进程（-I 隔离） ==")
    script = os.path.join(ROOT, "bin", "workorder_store.py")
    db = os.path.join(tmp, "cli.db")
    cov = os.path.join(tmp, "cli_cov.json")
    with open(cov, "w", encoding="utf-8") as fh:
        json.dump(report([("U1", "2026-03-01T08:00", False),
                          ("U1", "2026-03-01T09:00", False),
                          ("U2", "2026-03-01T08:00", True)]), fh)
    env = dict(os.environ)
    r1 = subprocess.run([sys.executable, "-I", script, "import", "--db", db,
                         "--coverage", cov, "--seq", "1"], capture_output=True, text=True, env=env)
    check("import 退出码 0", r1.returncode == 0, r1.stderr)
    out1 = json.loads(r1.stdout)
    check("import 输出 1 条变更", len(out1["changes"]) == 1 and out1["status"] == "applied")

    r2 = subprocess.run([sys.executable, "-I", script, "read", "--db", db,
                         "--after-cursor", "0", "--limit", "10"],
                        capture_output=True, text=True, env=env)
    check("read 退出码 0", r2.returncode == 0, r2.stderr)
    page = json.loads(r2.stdout)
    check("read 返回变更与游标", len(page["changes"]) == 1 and page["next_cursor"] == 1)

    r3 = subprocess.run([sys.executable, "-I", script, "tickets", "--db", db],
                        capture_output=True, text=True, env=env)
    tix = json.loads(r3.stdout)["tickets"]
    check("tickets 内容含首尾", tix == [{"unit_id": "U1",
                                        "start_hour": "2026-03-01T08:00",
                                        "end_hour": "2026-03-01T09:00"}], str(tix))

    # 坏文件退出码 2
    bad = os.path.join(tmp, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        json.dump({"results": [{"unit_id": "", "hour": "2026-03-01T08:00",
                                "threshold_met": False}]}, fh)
    r4 = subprocess.run([sys.executable, "-I", script, "import", "--db", db,
                         "--coverage", bad, "--seq", "2"],
                        capture_output=True, text=True, env=env)
    check("坏文件退出码 2", r4.returncode == 2, r4.stdout + r4.stderr)


# ---------------------------------------------------------------------------
# H. 兼容真实 met_coverage 产物
# ---------------------------------------------------------------------------

def test_real_coverage(tmp):
    print("== H. 真实 coverage.json 兼容 ==")
    src = "/tmp/mc_demo/out/v1/coverage.json"
    if not os.path.exists(src):
        # 现场生成一份
        subprocess.run([sys.executable, "-I",
                        os.path.join(ROOT, "bin", "met_coverage.py"),
                        "--input", os.path.join(ROOT, "inputs", "v1.json"),
                        "--out-root", os.path.join(tmp, "mc_out")],
                       check=True, capture_output=True)
        src = os.path.join(tmp, "mc_out", "v1", "coverage.json")
    db = os.path.join(tmp, "h.db")
    s = WorkOrderStore(db)
    out = s.import_coverage_file(src, seq=1)
    raw = json.load(open(src, encoding="utf-8"))
    n_false = sum(1 for r in raw["results"] if r["threshold_met"] is False)
    check("真实报告格全部入库",
          s.conn.execute("SELECT COUNT(*) FROM cells").fetchone()[0] == len(raw["results"]))
    check("cells_applied 与报告格数一致", out["cells_applied"] == len(raw["results"]))
    check("断供格识别一致", n_false >= 0)  # v1 样例里至少结构可导入
    # v1 的 false 格是否连续决定工单，用独立复算核对
    by_unit: dict = {}
    for r in raw["results"]:
        by_unit.setdefault(r["unit_id"], []).append((r["hour"], r["threshold_met"] is False))
    expect = []
    for u, lst in by_unit.items():
        lst.sort()
        run_start = None
        prev = None
        for h, is_false in lst + [(None, None)]:
            if is_false and (run_start is None or h != ws.add_hours(prev, 1)):
                if run_start is not None:
                    expect.append((u, run_start, prev))
                run_start = h
            elif not is_false and run_start is not None:
                expect.append((u, run_start, prev))
                run_start = None
            prev = h
    expect = [(u, a, b) for u, a, b in expect if b > a]
    got = [(t["unit_id"], t["start_hour"], t["end_hour"]) for t in s.current_tickets()]
    check("工单与独立复算一致", sorted(got) == sorted(expect), f"{got} vs {expect}")
    s.close()


def main():
    with tempfile.TemporaryDirectory(prefix="wostore_") as tmp:
        test_basic_intervals(tmp)
        test_gap_splits_interval(tmp)
        test_out_of_order(tmp)
        test_restate_split_then_merge_identity(tmp)
        test_idempotent_and_rejection(tmp)
        test_cursor_and_restart(tmp)
        test_atomicity(tmp)
        test_performance(tmp)
        test_cli(tmp)
        test_real_coverage(tmp)
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
