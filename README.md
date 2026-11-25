# 材料台架双路交班（handoff）

一个用 Python 3 标准库 + SQLite 实现的班次交班台账：设备替身按序供给双路
原值，采录逐路持久化，按班帧释放、归档、签发交班号，关源停桥后仍可凭交班号
取回两路原据、精确换算值、单位、出处和首次执行执据。

## 运行

```bash
python3 -m handoff <目录|库文件> <操作> [--json '<载荷>' | --in 文件 | stdin]
```

* `<目录>`：目录内使用 `handoff.sqlite3`；也可直接给 `.sqlite3` 文件路径。
* 每条命令只处理一个 JSON 对象：从 `--json`、`--in 文件`（`-` 为 stdin）
  或非交互式 stdin 读取；结果以单个 JSON 对象写到 stdout。
* 退出码：`0` 成功（含“待齐”这类正常业务状态）；`1` 业务拒绝
  （`{"ok": false, "error": {...}}`）；`2` 用法/JSON 错误。
* 常见字段也可用参数：`--shift`、`--frame`、`--channel T|D|温度|位移`、
  `--raw`、`--source`、`--handoff-no`、`--simulate-failure none|before|after`。
* 中文操作名与英文等价，例如 `采录` = `intake`，`释放` = `release`。

### 操作一览

| 操作 | 作用 |
| --- | --- |
| `open-shift` / `开班` | 冻结两路单位与有理定标 `y=ax+b`；同号同参复用，异参拒绝 |
| `load` / `装载` | 按序装载 `[帧号,温度,位移,温度出处,位移出处]` |
| `current` / `当前帧` | 查询设备当前帧 |
| `intake` / `采录` | 采录当前帧的一路，原值（含 `null` 缺测）持久化 |
| `release` / `释放` | 两路都持久化后释放原帧；`simulate_failure` 模拟失答 |
| `release-status` / `释放回执` | 按班帧查询释放回执，恢复“生效后失答” |
| `prepare` / `投件` | 按冻结口径生成该班帧的分路投件内容 |
| `archive-submit` / `归档` | 按班/帧/路分别提交，成功才签发首执 |
| `archive-status` / `归档状态` | 查看各班帧归档与交班号 |
| `handoff` / `交班` | 按班帧签发；缺一路实收则待齐，齐备才固定交班号 |
| `close-source` / `关源` | 停桥并关闭归档 |
| `open-archive` / `开归档` | 重开归档 |
| `close-archive` / `关归档` | 关闭归档 |
| `claim` / `领件` | 凭交班号领取两路交付，同件复领原执，未知号报未找到 |
| `status` / `状态` | 台账总览 |

## 调用样例（F7/F8）

```bash
DB=./bench-shift

# 1. 开班：温度 ℃，y=x+1/2；位移 mm，y=x
python3 -m handoff $DB open-shift --json '{
  "shift": "S1",
  "channels": {
    "T": {"unit": "℃", "calibration": "y=x+1/2"},
    "D": {"unit": "mm", "calibration": "y=x"}
  }
}'

# 2. 装载设备替身：F7=[7,0,null,T7,D7]，F8=[8,4,9,T8,D8]
python3 -m handoff $DB load --json '{
  "shift": "S1",
  "frames": [[7, 0, null, "T7", "D7"], [8, 4, 9, "T8", "D8"]]
}'

# 3. 采录 F7 两路
python3 -m handoff $DB intake --json '{"shift":"S1","channel":"T"}'
python3 -m handoff $DB intake --json '{"shift":"S1","channel":"D"}'

# 4. 释放 F7，模拟“生效后失答”（返回 ok=false / release_ack_unknown）
python3 -m handoff $DB release --json '{"shift":"S1","simulate_failure":"after"}'

# 5. 按原帧查询回执：释放已持久化，设备已到 F8
python3 -m handoff $DB release-status --json '{"shift":"S1","frame_no":7}'

# 6. 采录 F8 位移
python3 -m handoff $DB intake --json '{"shift":"S1","channel":"D"}'

# 7. 按冻结口径生成 F7 分路投件
python3 -m handoff $DB prepare --json '{"shift":"S1","frame_no":7}'

# 8. 归档 F7 温度成功，给出首执
python3 -m handoff $DB archive-submit --json '{"shift":"S1","frame_no":7,"channel":"T"}'

# 9. F7 缺位移实收 -> 待齐；F7 位移提交失败仍待齐
python3 -m handoff $DB handoff --json '{"shift":"S1","frame_no":7}'
python3 -m handoff $DB archive-submit --json '{"shift":"S1","frame_no":7,"channel":"D","simulate_failure":true}'
python3 -m handoff $DB handoff --json '{"shift":"S1","frame_no":7}'

# 10. 补交位移后齐备，固定交班号
python3 -m handoff $DB archive-submit --json '{"shift":"S1","frame_no":7,"channel":"D"}'
python3 -m handoff $DB handoff --json '{"shift":"S1","frame_no":7}'

# 11. 关源停桥（归档随之关闭），重开归档后凭交班号领件
python3 -m handoff $DB close-source --json '{"shift":"S1"}'
python3 -m handoff $DB open-archive --json '{"shift":"S1"}'
python3 -m handoff $DB claim --json '{"handoff_no":"H-XXXXXXXX"}'
```

`claim` 返回两路：`raw` 原值、`state`（`measured`/`missing`）、`value`
精确换算值（有理数字符串）、`value_display`（带单位）、`unit`、`source`
出处、`first_execution_token` 首执。缺测一路为 `state: "missing"`、
`value: null`，但其出处与首执照常返回。

## 语义要点

* **冻结**：`open-shift` 写入后，同班重复开班只有在两路单位与 `a/b` 完全
  相同时才复用（`created: false`），否则 `calibration_conflict`。系数一律
  以有理数保存与输出（如 `"1/2"`），避免浮点误差。
* **设备**：只供当前帧；帧号班内唯一；原值必须是整数或 `null`；坏行
  （类型错、行长度错、两路同时 `null`、重复帧号且内容不同）被拒收，且
  不越过它继续装载后面的行。重复装载同一批相同行是幂等的。
* **采录**：逐路独立提交，可重开；只接受设备当前帧的原值，不能改写；
  缺测写 `null` 也属持久化成功。采录不等待归档。
* **释放**：两路都持久化后才可释放；每次只结清一帧，绝不顺带释放下一帧。
  `simulate_failure: "before"` 表示生效前失答（未写入，原帧可续办）；
  `"after"` 表示已写入但回执丢失：台账记下 `pending_ack`，用
  `release-status`、原帧号或不带帧号的重试只结清原帧，在此之前显式指定
  下一帧会被 `release_pending_ack` 拒绝。
* **归档**：按班/帧/路分别提交；首次成功签发首执（确定性 token）；同件
  重交返回原执；同键不同凭据拒绝；模拟失败不入档、不发执。
* **交班**：按班帧签发；缺任一路实收即 `status: "waiting"`（正常状态，
  退出码 0），齐备后才固定 `handoff_no`，之后查询恒定不变。
* **关源**：同时停桥并关闭归档；重开归档后才可领件。未知交班号返回
  `handoff_not_found`（退出码 1）。

## 自检与演示

```bash
python3 -m handoff.verify   # 全部规则回归，失败退出非零
python3 -m handoff.demo     # F7/F8 全流程，含断源后凭号领件
python3 -B -m unittest discover -s tests -v
```

`.pointwise-runtime.json` 为运行清单：`verify` 覆盖两种失答与补收，
`demo` 展示断源领件。

## 布局

```
handoff/
  __main__.py    python3 -m handoff 入口
  cli.py         JSON 收发与参数解析
  service.py     全部业务规则（SQLite 事务内完成）
  db.py          表结构与事务
  rational.py    有理数解析/格式化、y=ax+b
  scenario.py    F7/F8 场景与临时库
  verify.py      自检
  demo.py        演示
tests/
  test_handoff.py  CLI 级回归
```
