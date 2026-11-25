#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
值班工单变更源 —— 在 met_coverage 覆盖核算产物之上，维护「持续观测断供」工单。

设计目标
========
1. 本地持久化（单个 SQLite 文件），跨进程重启继续使用；已发布变更记录的位置
   （变更日志自增 id）不随后续导入、重送或重启而变化。
2. 每次导入一份现有格式的 coverage.json + 一个全局唯一正整数发布序号（seq，
   越大越新，允许乱序送达）。只更新报告列出的小时格，每格以收到的最高 seq 为准。
3. 工单 = 同一单元内连续 false（threshold_met == False）小时的**最大**区间，且
   长度至少 2 小时；身份 (unit_id, start_hour)，内容含首尾。true / null / 未核算
   （没有格的小时，天然断开）都切断断供区间。
4. 一份报告只发布整批前后的净变化：新身份或同身份区间变化 => upsert；消失的身份
   => revoke。同一批按 (unit_id, start_hour) 排序。
5. 整批原子：小时状态、断供区间、导入登记、变更日志在同一事务提交；崩溃后只可能
   看到提交前或提交后的状态。同序号同字节文件重送为幂等 no-op；同序号不同文件整批
   拒绝。
6. 增量代价与历史长度无关：
   - 断供区间用「区间行」存储（不是逐小时日志），一次区间合并/拆分/伸缩只做按
     索引的定位与常数次行改动，**不逐小时扫描长断供**；
   - 变更日志追加写，游标读取走主键索引，读少量新变更不重扫历史；
   - 小批回补只触碰本批格及其邻接区间，不遍历无关历史。

纯 Python 标准库（sqlite3）。也可作为库使用：

    from workorder_store import WorkOrderStore, ImportRejected

    store = WorkOrderStore("/path/to/store.db")
    summary = store.import_coverage_file("coverage.json", seq=7)
    page = store.read_changes(after_cursor=0, limit=100)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# 格状态编码：只有 FALSE 参与断供区间
ST_FALSE = 0   # threshold_met is False  -> 断供
ST_TRUE = 1    # threshold_met is True   -> 达标
ST_NULL = 2    # threshold_met is None   -> 无阈值，同样切断断供

# 变更类型
OP_UPSERT = "upsert"
OP_REVOKE = "revoke"

HOUR_FMT = "%Y-%m-%dT%H:00"


class ImportRejected(Exception):
    """整批拒绝：任何已做改动都会回滚，库内状态不变。"""


# ----------------------------------------------------------------------------
# 时间：值班台把报告中的无时区整点视为 UTC
# ----------------------------------------------------------------------------

_UTC_HOUR_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):00$")


def parse_utc_hour(value: Any, where: str) -> str:
    """严格校验「无时区 UTC 整点」并规范化为 YYYY-MM-DDTHH:00。

    只接受形如 2026-03-01T09:00 的无时区字符串；拒绝：
    非字符串、空白首尾、带时区/偏移（Z、+08:00 等）、非整点、不可解析日期。
    """
    if not isinstance(value, str):
        raise ImportRejected(f"{where}: hour 必须是字符串，收到 {type(value).__name__}: {value!r}")
    if value != value.strip():
        raise ImportRejected(f"{where}: hour 不允许首尾空白: {value!r}")
    m = _UTC_HOUR_RE.match(value)
    if m is None:
        raise ImportRejected(
            f"{where}: hour 必须是无时区整点（格式 YYYY-MM-DDTHH:00，视为 UTC）: {value!r}"
        )
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    except ValueError as exc:
        raise ImportRejected(f"{where}: hour 日期不合法: {value!r} ({exc})") from exc
    return dt.strftime(HOUR_FMT)


def add_hours(hour: str, delta: int) -> str:
    return (datetime.strptime(hour, HOUR_FMT) + timedelta(hours=delta)).strftime(HOUR_FMT)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------------------------
# 报告解析与全量校验（在任何写库动作之前完成；任何错误整批拒绝）
# ----------------------------------------------------------------------------

def parse_coverage_report(data: Any) -> Dict[str, List[Tuple[str, str, int]]]:
    """把 coverage.json 校验并解析为 {unit_id: [(hour, state_code), ...]}。

    校验规则（任一不满足 => ImportRejected，调用方整批拒绝）：
      - 顶层为对象且含 results 数组；
      - 每条结果的 unit_id 必须是非空白字符串；
      - hour 必须是无时区 UTC 整点；
      - threshold_met 只允许 true / false / null；
      - 同一份报告内 (unit_id, hour) 不得重复。
    """
    if not isinstance(data, dict):
        raise ImportRejected("coverage.json 顶层必须是对象")
    results = data.get("results")
    if not isinstance(results, list):
        raise ImportRejected("coverage.json 缺少 results 数组（现有核算输出格式）")

    by_unit: Dict[str, List[Tuple[str, str, int]]] = {}
    seen: set = set()
    for i, item in enumerate(results):
        where = f"results[{i}]"
        if not isinstance(item, dict):
            raise ImportRejected(f"{where}: 必须是对象")

        uid = item.get("unit_id")
        if not isinstance(uid, str) or not uid.strip():
            raise ImportRejected(
                f"{where}: unit_id 必须是非空白字符串，收到 {uid!r}"
            )
        uid = uid.strip()
        if uid != item["unit_id"]:
            raise ImportRejected(f"{where}: unit_id 不允许首尾空白: {item['unit_id']!r}")

        hour = parse_utc_hour(item.get("hour"), where)

        verdict = item.get("threshold_met", None)
        # bool 是 int 的子类，必须先判 bool；只接受 true/false/null
        if verdict is True:
            state = ST_TRUE
        elif verdict is False:
            state = ST_FALSE
        elif verdict is None:
            state = ST_NULL
        else:
            raise ImportRejected(
                f"{where}: threshold_met 只允许 true / false / null，收到 {verdict!r}"
            )

        key = (uid, hour)
        if key in seen:
            raise ImportRejected(f"{where}: 报告内重复格 unit_id={uid!r} hour={hour!r}")
        seen.add(key)
        by_unit.setdefault(uid, []).append((uid, hour, state))
    return by_unit


# ----------------------------------------------------------------------------
# 持久化工单变更源
# ----------------------------------------------------------------------------

class WorkOrderStore:
    """基于 SQLite 的本地工单变更源；一个文件一个库，可跨进程重开。"""

    SCHEMA = """
    PRAGMA journal_mode=WAL;

    -- 每个「单元 × 小时」的最新状态与写入它的最高发布序号
    CREATE TABLE IF NOT EXISTS cells (
        unit_id   TEXT NOT NULL,
        hour      TEXT NOT NULL,
        state     INTEGER NOT NULL,          -- 0=false 1=true 2=null
        seq       INTEGER NOT NULL,
        PRIMARY KEY (unit_id, hour)
    ) WITHOUT ROWID;

    -- 断供区间（state=false 的最大连续小时段，按小时相邻即连续）
    -- 只保留实际存在的 false 段；长度 1 的段也在此表，但不作为工单发布。
    CREATE TABLE IF NOT EXISTS false_runs (
        unit_id    TEXT NOT NULL,
        start_hour TEXT NOT NULL,
        end_hour   TEXT NOT NULL,
        PRIMARY KEY (unit_id, start_hour)
    ) WITHOUT ROWID;
    CREATE INDEX IF NOT EXISTS idx_runs_unit_end ON false_runs(unit_id, end_hour);

    -- 追加写、永不修改的变更日志；id 即下游游标位置
    CREATE TABLE IF NOT EXISTS change_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        seq        INTEGER NOT NULL,          -- 发布该变更的导入序号
        unit_id    TEXT NOT NULL,
        start_hour TEXT NOT NULL,
        end_hour   TEXT,                       -- revoke 时为 NULL
        op         TEXT NOT NULL CHECK (op IN ('upsert', 'revoke'))
    );
    CREATE INDEX IF NOT EXISTS idx_change_cursor ON change_log(id);

    -- 导入登记：同序号幂等/冲突检测
    CREATE TABLE IF NOT EXISTS imports (
        seq          INTEGER PRIMARY KEY,
        file_sha256  TEXT NOT NULL,
        cell_count   INTEGER NOT NULL,
        imported_at  TEXT NOT NULL
    ) WITHOUT ROWID;
    """

    def __init__(self, db_path: str, timeout: float = 30.0):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=timeout)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "WorkOrderStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------

    def import_coverage_file(self, path: str, seq: int) -> Dict[str, Any]:
        """从文件导入一份 coverage.json。文件按不可信数据读取（二进制哈希）。"""
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            raise ImportRejected(f"发布序号必须是正整数，收到 {seq!r}")
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise ImportRejected(f"无法读取 coverage 文件 {path!r}: {exc}") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImportRejected(f"coverage.json 不是合法 UTF-8 JSON: {exc}") from exc
        return self.import_coverage_data(data, seq, file_sha256=sha256_bytes(raw))

    def import_coverage_data(self, data: Any, seq: int,
                             file_sha256: Optional[str] = None) -> Dict[str, Any]:
        """导入已解析的 coverage 报告（主要供测试/嵌入调用）。"""
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            raise ImportRejected(f"发布序号必须是正整数，收到 {seq!r}")
        # 1) 先做完全部校验，再碰数据库写事务
        by_unit = parse_coverage_report(data)
        cell_count = sum(len(v) for v in by_unit.values())
        if file_sha256 is None:
            file_sha256 = hashlib.sha256(
                json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # 2) 同序号：同字节 => 幂等 no-op；不同字节 => 整批拒绝
            row = conn.execute(
                "SELECT file_sha256 FROM imports WHERE seq=?", (seq,)
            ).fetchone()
            if row is not None:
                if row[0] == file_sha256:
                    conn.execute("ROLLBACK")
                    return {
                        "seq": seq,
                        "status": "duplicate_noop",
                        "cells_in_report": cell_count,
                        "cells_applied": 0,
                        "changes": [],
                    }
                raise ImportRejected(
                    f"发布序号 {seq} 已用于另一份不同字节的 coverage.json"
                    f"（已存 sha256={row[0][:12]}…，本次 {file_sha256[:12]}…）；整批拒绝"
                )

            # 3) 逐格按最高 seq 应用，并增量维护断供区间、收集净变更
            all_changes: List[Dict[str, Any]] = []
            applied = 0
            for uid in sorted(by_unit):
                cells = by_unit[uid]
                before_tickets = self._tickets_in_window(
                    conn, uid,
                    min(h for _, h, _ in cells),
                    max(h for _, h, _ in cells),
                )
                for _, hour, state in cells:
                    if self._apply_cell(conn, uid, hour, state, seq):
                        applied += 1
                after_tickets = self._tickets_in_window(
                    conn, uid,
                    min(h for _, h, _ in cells),
                    max(h for _, h, _ in cells),
                )
                all_changes.extend(self._diff_tickets(seq, uid, before_tickets, after_tickets))

            # 同批按 (unit_id, start_hour) 排序（_diff_tickets 内已按 start 排，
            # 外层按 unit 排，这里显式归并保证契约）
            all_changes.sort(key=lambda c: (c["unit_id"], c["start_hour"]))

            # 4) 变更日志、导入登记与状态在同一事务提交
            self._insert_changes(conn, all_changes)
            conn.execute(
                "INSERT INTO imports(seq, file_sha256, cell_count, imported_at) "
                "VALUES (?, ?, ?, ?)",
                (seq, file_sha256, cell_count,
                 datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")),
            )
            conn.commit()
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return {
            "seq": seq,
            "status": "applied",
            "cells_in_report": cell_count,
            "cells_applied": applied,
            "changes": all_changes,
        }

    def _insert_changes(self, conn: sqlite3.Connection, changes: List[Dict[str, Any]]) -> None:
        """单独成方法，便于测试注入提交前故障（验证原子性）。"""
        conn.executemany(
            "INSERT INTO change_log(seq, unit_id, start_hour, end_hour, op) "
            "VALUES (?, ?, ?, ?, ?)",
            [(c["seq"], c["unit_id"], c["start_hour"], c["end_hour"], c["op"])
             for c in changes],
        )

    # ------------------------------------------------------------------
    # 单格增量：全部按索引定位，单次调用代价与区间长度/历史长度无关
    # ------------------------------------------------------------------

    def _apply_cell(self, conn: sqlite3.Connection, uid: str, hour: str,
                    state: int, seq: int) -> bool:
        """应用一个格；返回是否真正改变了状态（False 表示被更高 seq 压住）。"""
        row = conn.execute(
            "SELECT state, seq FROM cells WHERE unit_id=? AND hour=?", (uid, hour)
        ).fetchone()
        if row is not None:
            old_state, old_seq = row
            if old_seq >= seq:
                return False  # 迟到的低序号重述，不更新
            if old_state == state:
                conn.execute(
                    "UPDATE cells SET seq=? WHERE unit_id=? AND hour=?",
                    (seq, uid, hour),
                )
                return False
        if row is None:
            conn.execute(
                "INSERT INTO cells(unit_id, hour, state, seq) VALUES (?, ?, ?, ?)",
                (uid, hour, state, seq),
            )
        else:
            conn.execute(
                "UPDATE cells SET state=?, seq=? WHERE unit_id=? AND hour=?",
                (state, seq, uid, hour),
            )

        if state == ST_FALSE:
            self._insert_false(conn, uid, hour)
        else:
            self._remove_false(conn, uid, hour)
        return True

    def _run_containing(self, conn: sqlite3.Connection, uid: str,
                        hour: str) -> Optional[Tuple[str, str]]:
        """定位包含 hour 的断供区间（hour 本身为 false 时必命中）。"""
        row = conn.execute(
            "SELECT start_hour, end_hour FROM false_runs "
            "WHERE unit_id=? AND start_hour<=? AND end_hour>=?",
            (uid, hour, hour),
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def _insert_false(self, conn: sqlite3.Connection, uid: str, hour: str) -> None:
        """hour 变为 false：可能新建、左延、右延或合并两段，常数次索引操作。"""
        prev_h = add_hours(hour, -1)
        next_h = add_hours(hour, 1)
        left = conn.execute(
            "SELECT start_hour, end_hour FROM false_runs "
            "WHERE unit_id=? AND end_hour=?", (uid, prev_h)
        ).fetchone()
        right = conn.execute(
            "SELECT start_hour, end_hour FROM false_runs "
            "WHERE unit_id=? AND start_hour=?", (uid, next_h)
        ).fetchone()
        # 理论上 left/right 不会同时包含 hour（hour 原本不是 false）
        start = left[0] if left else hour
        end = right[1] if right else hour
        if left:
            conn.execute(
                "DELETE FROM false_runs WHERE unit_id=? AND start_hour=?",
                (uid, left[0]),
            )
        if right:
            conn.execute(
                "DELETE FROM false_runs WHERE unit_id=? AND start_hour=?",
                (uid, right[0]),
            )
        conn.execute(
            "INSERT INTO false_runs(unit_id, start_hour, end_hour) VALUES (?, ?, ?)",
            (uid, start, end),
        )

    def _remove_false(self, conn: sqlite3.Connection, uid: str, hour: str) -> None:
        """hour 变为 true/null：所在区间删除、缩短或拆成左右两段，不逐小时扫描。"""
        found = self._run_containing(conn, uid, hour)
        if found is None:
            return
        start, end = found
        conn.execute(
            "DELETE FROM false_runs WHERE unit_id=? AND start_hour=?", (uid, start)
        )
        if hour == start and hour == end:
            return  # 单点段，直接消失
        if hour != start:
            conn.execute(
                "INSERT INTO false_runs(unit_id, start_hour, end_hour) VALUES (?, ?, ?)",
                (uid, start, add_hours(hour, -1)),
            )
        if hour != end:
            conn.execute(
                "INSERT INTO false_runs(unit_id, start_hour, end_hour) VALUES (?, ?, ?)",
                (uid, add_hours(hour, 1), end),
            )

    # ------------------------------------------------------------------
    # 工单视图与整批前后净变化
    # ------------------------------------------------------------------

    def _tickets_in_window(self, conn: sqlite3.Connection, uid: str,
                           lo_hour: str, hi_hour: str) -> Dict[str, str]:
        """取受影响窗口内、区间长度 >= 2 小时的工单 {start_hour: end_hour}。

        窗口为 [lo-1h, hi+1h]：本批任何格变化能影响到的区间只可能与该窗口相交
        （相邻区间通过 lo-1 / hi+1 接入），其余历史区间不可能变化，无需读取。
        """
        lo = add_hours(lo_hour, -1)
        hi = add_hours(hi_hour, 1)
        rows = conn.execute(
            "SELECT start_hour, end_hour FROM false_runs "
            "WHERE unit_id=? AND start_hour<=? AND end_hour>=? ORDER BY start_hour",
            (uid, hi, lo),
        ).fetchall()
        tickets: Dict[str, str] = {}
        for start, end in rows:
            # 至少两个整点小时 => end 严格晚于 start（定宽字符串可直接比较）
            if end > start:
                tickets[start] = end
        return tickets

    def _diff_tickets(self, seq: int, uid: str,
                      before: Dict[str, str], after: Dict[str, str]) -> List[Dict[str, Any]]:
        changes: List[Dict[str, Any]] = []
        for start in sorted(set(before) | set(after)):
            old_end = before.get(start)
            new_end = after.get(start)
            if old_end == new_end:
                continue
            if new_end is None:
                changes.append({"seq": seq, "unit_id": uid, "start_hour": start,
                                "end_hour": None, "op": OP_REVOKE})
            else:
                changes.append({"seq": seq, "unit_id": uid, "start_hour": start,
                                "end_hour": new_end, "op": OP_UPSERT})
        return changes

    # ------------------------------------------------------------------
    # 下游读取：游标 + 限量，游标不随重启/重送变化
    # ------------------------------------------------------------------

    def read_changes(self, after_cursor: int = 0, limit: int = 100) -> Dict[str, Any]:
        """读取 id > after_cursor 的至多 limit 条变更，并返回续读游标。

        返回 {"changes": [...], "next_cursor": int, "has_more": bool}。
        游标即 change_log.id（不透明整数）；从头读取传 0。
        """
        if not isinstance(after_cursor, int) or isinstance(after_cursor, bool) or after_cursor < 0:
            raise ValueError("after_cursor 必须是非负整数")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit 必须是正整数")
        limit = min(limit, 10_000)
        rows = self.conn.execute(
            "SELECT id, seq, unit_id, start_hour, end_hour, op FROM change_log "
            "WHERE id > ? ORDER BY id ASC LIMIT ?",
            (after_cursor, limit),
        ).fetchall()
        changes = [{
            "cursor": r[0], "seq": r[1], "unit_id": r[2],
            "start_hour": r[3], "end_hour": r[4], "op": r[5],
        } for r in rows]
        next_cursor = rows[-1][0] if rows else after_cursor
        has_more = len(rows) == limit and self.conn.execute(
            "SELECT 1 FROM change_log WHERE id > ? LIMIT 1", (next_cursor,)
        ).fetchone() is not None
        return {"changes": changes, "next_cursor": next_cursor, "has_more": has_more}

    def current_tickets(self) -> List[Dict[str, Any]]:
        """当前全部有效工单（长度 >= 2 的断供区间），按单元、首小时排序。"""
        rows = self.conn.execute(
            "SELECT unit_id, start_hour, end_hour FROM false_runs "
            "WHERE end_hour > start_hour ORDER BY unit_id, start_hour"
        ).fetchall()
        return [{"unit_id": u, "start_hour": s, "end_hour": e} for u, s, e in rows]

    # ------------------------------------------------------------------
    # 诊断/核对用只读接口
    # ------------------------------------------------------------------

    def cell_state(self, uid: str, hour: str) -> Optional[Tuple[int, int]]:
        row = self.conn.execute(
            "SELECT state, seq FROM cells WHERE unit_id=? AND hour=?", (uid, hour)
        ).fetchone()
        return None if row is None else (row[0], row[1])

    def imported_seqs(self) -> List[Tuple[int, str]]:
        return list(self.conn.execute(
            "SELECT seq, file_sha256 FROM imports ORDER BY seq"
        ).fetchall())


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="值班工单变更源：导入 coverage.json，发布持续断供工单变更（SQLite 本地持久化）"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_imp = sub.add_parser("import", help="导入一份 coverage.json（带全局唯一发布序号）")
    p_imp.add_argument("--db", required=True, help="本地工单库 SQLite 文件路径")
    p_imp.add_argument("--coverage", required=True, help="coverage.json 路径")
    p_imp.add_argument("--seq", required=True, type=int, help="正整数发布序号（越大越新，可乱序送达）")

    p_read = sub.add_parser("read", help="从游标位置限量读取变更，返回续读游标")
    p_read.add_argument("--db", required=True)
    p_read.add_argument("--after-cursor", type=int, default=0, help="不传或 0 表示从头读")
    p_read.add_argument("--limit", type=int, default=100)

    p_tix = sub.add_parser("tickets", help="列出当前全部有效工单")
    p_tix.add_argument("--db", required=True)

    args = ap.parse_args(argv)
    try:
        with WorkOrderStore(args.db) as store:
            if args.cmd == "import":
                summary = store.import_coverage_file(args.coverage, args.seq)
                _print_json(summary)
                return 0
            if args.cmd == "read":
                _print_json(store.read_changes(args.after_cursor, args.limit))
                return 0
            if args.cmd == "tickets":
                _print_json({"tickets": store.current_tickets()})
                return 0
    except ImportRejected as exc:
        print(f"[整批拒绝] {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[参数错误] {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
