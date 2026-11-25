# 气象风险单元观测覆盖核算

按「小时 × 风险单元」独立核算**最大可证明观测覆盖数（独立来源数）**，输出使用的原始物理观测、
基准站↔观测的分配关系与有向替代链，并判定是否达到输入阈值。纯 Python 标准库实现。

## 1. 核算模型（每个「单元 × 小时」独立）

- **左顶点**：该小时在单元多边形内（射线法，边界点算在内）且基准站有效期覆盖该小时的逻辑基准站。
- **右顶点**：该小时实际产生、质量合格（`quality == "OK"`）、实际站点已知的物理观测。
  零值是合法值；缺测（无观测）不产生右顶点；质量不合格的观测被排除并注明原因。
- **边（基准站 → 观测）**：基准站能沿**当时对该单元有效**的核准有向替代边到达观测的实际站点。
  - 原站物理观测以**长度 0 自身链**服务自己，不需要替代边；
  - 替代链只沿核准方向成链（BFS 单源可达，记录完整站点链与边 id），**绝不压成无向等价组**；
  - 边的有效性同时检查：适用单元集合、半开有效期 `[valid_from, valid_to)`、源站在该单元有效。
- **约束**：每个基准站最多认领 1 份观测；每份物理观测在该单元该小时最多供 1 个基准站。
  这是二分图，求**最大匹配**（BFS 增广路，显式记录每次重配/增广见证，杜绝贪心误判）。
- **不同单元独立核算**：同一物理观测可在不同单元/小时分别被认领，互不相加为“总来源数”。

## 2. 最大性与判定证据

- 达标：给出逐站分配 + 每条链的站点序列/边序列（匹配见证）。
- 不足：给出
  - 未匹配基准站清单与亏缺数；
  - 残差多源 BFS 可达集与 **Kőnig 最小顶点覆盖**（`|覆盖| == |匹配|`，可核验无增广路）；
  - **Hall 见证**：站集 Z_L 及其邻接观测集 N(Z_L)，满足 `|N(Z_L)| < |Z_L|`，
    证明至少缺多少独立来源，且匹配已最大。

## 3. 资料错误（拒绝出版本，退出码 2）

- 有效替代边图出现**有向环**（含自环）；
- 同一输入版内同一物理观测 id 出现**不同载荷冲突**（重复登记也拒绝）；
- 基准站/单元/边/阈值的结构性错误、时间不可解析、非整点观测等。

## 4. 版本化（不可变）

- 替代到期或质量修订必须以**新输入版**重述受影响小时（`restates` 字段声明旧版）；
- 输出按版本写独立目录，已存在且非空则拒绝覆盖；旧版本原样保留；
- 每个 `coverage.json` 记录输入文件绝对路径与 SHA-256，`manifest.json` 记录重述关系与保留的旧版。

## 5. 目录与运行

```
bin/met_coverage.py        覆盖核算主程序（无第三方依赖，原有入口与输出不变）
bin/workorder_store.py     值班工单变更源（SQLite 本地持久化，可跨进程重启）
inputs/                    v1、v2（重述版）与两个资料错误样例
out/<version>/             coverage.json（机器可读）、coverage.md（人读）、manifest.json
tests/run_tests.py         端到端 + 独立复核（81 项）
tests/random_diff.py       400 组随机图对暴力枚举的差分测试
tests/test_workorder_store.py  工单变更源测试（95 项）
```

```bash
# 核算（隔离模式 -I，避免输入目录植入模块）
python3 -I bin/met_coverage.py --input inputs/v1.json --out-root out
python3 -I bin/met_coverage.py --input inputs/v2.json --out-root out

# 验证
python3 -I tests/run_tests.py
python3 -I tests/random_diff.py
python3 -I tests/test_workorder_store.py
```

## 5b. 值班工单变更源（`workorder_store.py`）

在覆盖核算产物之上，把逐小时的 `threshold_met` 流转为**持续观测断供工单**变更流，
落在单个本地 SQLite 文件中，可跨进程重启继续使用。**原核算入口及其输出完全不变。**

### 语义

- 每次导入一份现有格式的 `coverage.json`，调用方另给**正整数发布序号 seq**
  （全局唯一、越大越新、允许乱序送达）。
- `results` 按 `unit_id` + `hour` 标识小时；无时区整点按 **UTC** 解释；只更新报告
  列出的小时，每格以**收到的最高 seq** 为准（迟到的低 seq 重述不生效）。
- `threshold_met` 只接受 `true` / `false` / `null`，**只有 `false` 算断供**；
  达标、未核算（无格）、无阈值（`null`）都隔开前后的断供小时。
- 一个单元内，每段**至少连续两小时的最大断供区间**对应一张工单：身份为
  `(unit_id, start_hour)`，内容给出含首尾的 `[start_hour, end_hour]`；单小时
  断供不成单。
- 一份报告只发布整批前后的**净变化**：新身份或同身份区间变化发 `upsert`，
  消失的身份发 `revoke`；同批按 `(unit_id, start_hour)` 排序。
- 同序号重送**字节相同**的文件 => 幂等 no-op，不产生新变更；同序号不同文件、
  报告内重复格、非字符串/空白单元标识、非无时区整点、非法判定值 => **整批拒绝**，
  已有结果不变（退出码 2）。
- 小时状态、断供区间、导入登记、变更日志在**同一事务**提交；中断重启后只可见
  整批提交前或提交后的结果，重送可安全恢复。
- 已发布变更的位置（日志自增 id，即游标）不随重启或重送变化。

### 运行入口

```bash
# 导入（seq 由调用方分配；可乱序、可重跑）
python3 -I bin/workorder_store.py import \
    --db ./var/workorders.db \
    --coverage out/v1/coverage.json --seq 1001

# 下游从头/从上次游标限量读取；next_cursor 即续读游标，has_more 指示是否还有
python3 -I bin/workorder_store.py read --db ./var/workorders.db \
    --after-cursor 0 --limit 100
python3 -I bin/workorder_store.py read --db ./var/workorders.db \
    --after-cursor 100 --limit 100

# 查看当前全部有效工单（运维用）
python3 -I bin/workorder_store.py tickets --db ./var/workorders.db
```

`read` 返回：

```json
{
  "changes": [
    {"cursor": 1, "seq": 1001, "unit_id": "U1",
     "start_hour": "2026-03-01T08:00", "end_hour": "2026-03-01T11:00",
     "op": "upsert"},
    {"cursor": 2, "seq": 1002, "unit_id": "U1",
     "start_hour": "2026-03-01T08:00", "end_hour": null,
     "op": "revoke"}
  ],
  "next_cursor": 2,
  "has_more": false
}
```

也可作为库嵌入：`WorkOrderStore(path).import_coverage_file(path, seq)` /
`import_coverage_data(dict, seq)` / `read_changes(after_cursor, limit)`。

### 多年历史下的增量代价

- 断供区间以**区间行**存储（不是逐小时日志）：一次合并/拆分/伸缩只做按索引的
  常数次行改动，**不逐小时扫描长断供**；
- 小批回补只读取本批时间窗 ±1 小时内相交的区间，**不遍历无关历史**；
- 变更日志追加写，游标读取走主键索引，读少量新变更**不重扫历史日志**
  （测试以 5 年 × 10 单元 ≈ 43.8 万格验证：小批导入 SQL 次数为小常数且无全表扫描）。

## 6. 输入 JSON 字段

| 字段 | 说明 |
| --- | --- |
| `version` / `restates` | 输入版本号；重述版声明被替代的旧版本 |
| `reference_stations[]` | `id`、`position[x,y]`、`valid_from/valid_to`（半开，可缺省无界） |
| `units[]` | `id`、`polygon`（≥3 顶点的简单环，首尾可重合） |
| `substitution_edges[]` | `id`、`from`、`to`、有效期、`applies_to_units`（非空） |
| `observations[]` | `id`、实际 `station`、整点 `time`、`quality`、`value`（零值可用） |
| `evaluate_hours[]` | 显式要求核算的小时（无观测也出报告，体现缺测） |
| `thresholds[]` | `unit` + 可选 `hour`（缺省为该单元默认）+ `required_coverage` |
