# CS2 AI 导播 — 自检、最小修复与回归记录

- 基线：`frozen_v1.0.0/`（优化前冻结）
- 基线主程序 SHA256：`EBBB29589265C3BD92D8793558E0E9A0A366DE2A7BB4161AF34E377109D7DB0E`
- 自检环境：Windows 11 10.0.26300 / Python 3.14.0 / 管理员
- 状态：✅ 通过　❌ 不通过　⚠️ 无法在本机验证
- **v1.1 增量（2026-10-21）**：新增 HLAE 快捷键“单次/双击”触发；`alt+数字` 不再误判为手动接管。
- **v1.2 增量（2026-10-21）**：包点拆分 A/B 两套快捷键，按炸弹位置自动判定。
- **v1.4 增量（2026-10-21）**：修复“缺少 allplayers_position”（同端口 GSI 配置互相覆盖）；快捷键改为“长按 Alt + 短按两次”；有玩家存活时 POV 优先。
- **v1.5 增量（2026-10-21）**：GSI cfg 改为 Valve KeyValues（JSON 会被 CS2 忽略，导致一直等待数据）；出生点按上一回合胜方分 CT/T。
- **v1.6 增量（2026-10-21）**：移除观战判定；阵亡自动切最近敌人；运镜改为可选 ALT/SHIFT/CTRL + A–Z/0–9；时序 80/30/80/30/50ms；楼层/远距离无交火判定；胜率权重提高；200ms 硬间隔。
- **v1.7 增量（2026-10-21）**：在线学习（round_kills 反馈）+ 从 demo 离线训练同一模型；在线部分已在 v1.9 移除。
- **v1.8 增量（2026-10-21）**：整合专业导播：独立日志、事件动作、OBS WebSocket、FFmpeg 击杀回放、玩家改名。
- **v1.9 增量（2026-10-21）**：移除即时学习；新增 decisions.csv、3 秒标签回填、随机森林训练与模糊区间仲裁。
- **v1.10 增量（2026-10-21）**：Phase 1 专业导播观测层：镜头时间线、六项专业指标、回合故事状态；不改评分/模型。
- **v1.10.1 增量（2026-10-21）**：尝试修复 `observer_slot` 与观察者数字键错位，但方向写反（键 N→slot N+1），已废弃。
- **v1.10.2 增量（2026-10-21）**：按完整实测表更正为键 N→slot (N-1) mod 10（键1→slot0、键0→slot9）。
- **v1.10.3 增量（2026-10-21）**：防碎切（最短停留 2s、10 秒最多 3 次）、当前 POV 交火/拆包锁定、HLAE 管理员权限诊断。
- **v1.10.4 增量（2026-10-21）**：适配 MulNX 的 200ms 连击缓冲，Alt 在最后按键后保持 300ms。
- **v1.10.5 增量（2026-10-21）**：移除 FFmpeg 回放、录制/击杀点/生成回放动作和玩家改名；当前测试 94 项。

---

## 1. 自检清单（修复前 → 修复后）

| # | 检查项 | 修复前 | 修复后 | 检查依据 |
|---|---|---|---|---|
| 1 | 导播相关数据是否合理 | ✅ | ✅ | 评分随距离/血量/武器单调变化：600u 对枪 0.50，1300u 0.22（不切），敌人 20 血 1.00，AWP@1400 0.46，手枪@1400 0，持刀者被判为猎物；单测 `TestScoring` 7 项 |
| 2 | 依赖安装与导入降级 | ✅ | ✅ | 实际执行 pip 安装；分别阻断 `pydirectinput`/`keyboard`/`pygetwindow`/`tkinter` 启动子进程，4 组均能 import、`check_dependencies` 正确标记缺失；tkinter 缺失时 `main` 给出提示而不是崩溃 |
| 3 | GSI 配置必需字段 | ✅ | ✅ | `gsi_config_dict` 输出 12 个 data 字段，含 `allplayers_position`、`bomb`、`phase_countdowns`；单测 `test_all_required_fields` |
| 4 | GSI HTTP 线程安全 | ✅ | ✅ | `SnapshotStore` 加锁；20 线程并发 POST 60 次全部 200，收包计数 60、快照结构完整；单测 `TestGSIConcurrency` |
| 5 | 断流是否跳过决策 | ✅ | ✅ | 快照 10 秒未更新时 `_tick` 标记 `stale` 并直接返回，`_decide_and_switch` 调用次数 0；单测 `test_stale_snapshot_skips_decision` |
| 6 | observer_slot → 数字键映射 | ❌ 旧版直接当键，实测错位 | ✅ v1.10.2：键 N→slot N-1；`slot_to_key`/`key_to_slot`，slot 0→1、1→2、3→4、4→5、9→0；-1/10/99 拒绝且不发键；`TestSlotKeyMapping`、`TestInputIdentity` |
| 7 | 模拟按键前确保 CS2 聚焦 | ✅ | ✅ | `press_slot`/`send_hotkey` 都先 `ensure_foreground()`（前台进程必须是 cs2.exe）；焦点为假时 0 次按键；单测 `test_no_key_without_focus`、`test_hotkey_also_requires_focus` |
| 8 | 程序按键不被误判为手动接管 | ✅ | ✅ | 采用按数字键分别记录的 `_simulating_until`（等价于 simulating_until，但不会吞掉用户按下的其他数字）；钩子事件忽略被标记的 3、仍识别 4；单测 `test_hook_ignores_simulated_key` |
| 9 | 手动按 0-9 暂停约 10 秒 | ✅ | ✅ | 真实 `keybd_event` 按 4，状态显示剩余 9.7s；暂停期间切人调用 0 次；单测 `test_manual_pause_blocks_switch` |
| 10 | DeepSeek 失败/空 content 降级 | ✅ | ✅ | 超时/网络错误只请求 1 次；空 content 1 次并 `_fail`；仅 `response_format` 不兼容时重试 1 次；均返回 None，规则引擎继续；单测 4 项 |
| 11 | 切换最小间隔、目标未变不重复按 | ✅ | ✅ | 1.0s 冷却、0.10 分数迟滞、4s 同 slot 保护；实测新目标出现到切换 0.98s，首次切换 27ms；同目标重复数据不再按键；单测 `TestDecision` |
| 12 | freezetime 出生点运镜同回合一次 | ✅ | ✅ | 按 `map + round` 去重；发送失败 1s 后重试但不重复成功触发；单测 `test_round_cam_once_per_round`、`test_freezetime_retry_after_failed_focus` |
| 13 | 炸弹 ≤8s 包点运镜同次安放一次 | ✅ | ✅ | 10s 不触发、7s 触发一次、6s 不重复、下一回合可再次触发；单测 `test_bomb_cam_threshold_and_once` |
| 14 | 配置持久化 | ✅ | ✅ | 临时 `%APPDATA%` 写入/读取端口、快捷键、Key；缺键自动补默认；单测 `TestConfigPersistence` |
| 15 | GUI 路径推导 | ✅ | ✅ | `game/cs2.exe` 与 `game/bin/win64/cs2.exe` 两种布局都推导到 `game/csgo/cfg`；单测 `test_derive_cfg_dir_win64_layout` |
| 16 | GSI 一键安装覆盖保护 | ✅ | ✅ | 无 overwrite 时抛 `FileExistsError`；覆盖前生成 `.bak_时间戳`；GUI 中调用 `messagebox.askyesno` 确认；单测 `test_install_and_backup` |
| 17 | 异常路径有日志且主循环不退 | ❌ | ✅ | 修复前：钩子/轮询/手动回调与 GUI `_poll` 使用裸 `except: pass`，异常不可见。修复后：5 秒节流 warning、Tk `report_callback_exception`、`_poll` 异常记录并继续；单测 `test_manual_handler_exception_is_logged`、`test_loop_survives_tick_exception`，GUI 注入 `1/0` 后仍存活且日志命中 |
| 18 | 真实 CS2/GSI/HLAE 链路 | ⚠️ | ⚠️ | 本机已安装 CS2（`D:\SteamLibrary\...\game\bin\win64\cs2.exe`）且 GSI cfg 已写入，但游戏未运行；无法在不启动游戏的情况下验证真实推送与宏触发 |
| 19 | 真实 DeepSeek API | ⚠️ | ⚠️ | 无可用 Key，未发起真实计费请求；异常/空响应路径已用桩验证 |
| 20 | GUI 覆盖确认弹窗本身 | ⚠️ | ⚠️ | 代码路径与底层保护已验证，模态弹窗需人工点一次确认 |

---

## 2. 修复前的 ❌ 与最小修复

### ❌ 17a：键盘监听异常被静默吞掉

- 位置：`ManualKeyWatcher._on_event` / `_poll_loop` / `_handle`。
- 风险：钩子回调持续抛异常时，手动接管会悄悄失效，日志里没有任何线索。
- 修复：新增 `_warn()`（5 秒最多一条），三处异常分别记录“键盘钩子事件处理异常 / 按键轮询异常 / 手动接管处理异常”，并保证轮询循环继续。
- 影响面：仅异常分支，不改变正常按键路径。

### ❌ 17b：GUI 状态刷新异常被静默吞掉

- 位置：`DirectorApp._poll`。
- 风险：pythonw 无控制台时，状态刷新异常完全不可见，界面卡在旧数据。
- 修复：`_poll` 捕获后写 warning（5 秒节流）并继续 `after(200)`；同时设置 `root.report_callback_exception`，让所有 Tk 回调异常进入日志。
- 影响面：仅异常分支。

---

## 3. 允许范围内的优化（未改核心前提）

| 方向 | 改动 | 说明 |
|---|---|---|
| API 调用成本 | 只有 `response_format`/`json_object` 不支持时才去掉参数重试；调用时间 deque 裁剪到 60 秒 | 超时/网络错误不再双倍请求、双倍等待；长局内存不增长 |
| 日志可读性 | 切换日志去掉重复的 `slot/昵称`，改为 `切换视角 → slot=1 P1 [规则] 分=… 距敌=… 瞄准=… HP=… 胜率=… 武器=… 对手=…` | 一行即可定位决策依据 |
| 异常降级 | 钩子/GUI 异常可见化（见上） | 不改变正常流程 |
| 切换延迟 | 实测 27ms（无冷却）/ 0.98s（受 1s 冷却限制），未改参数 | 继续保留 1s 冷却，优先防抖动 |
| 误切抖动 | 未改评分与迟滞参数 | 回归测试确认阈值/迟滞/重复保护不变 |

**未改动**：`observer_slot` 到数字键的映射、只模拟按键的实现、GSI 只读边界、两个 HLAE 快捷键的发送方式、评分公式与阈值。

---

## 4. 回归结果（修复后）

```text
python -m unittest test_cs2_ai_director
Ran 45 tests in ~3.3s
OK

python -m ruff check --select F,E9 cs2_ai_director.py test_cs2_ai_director.py
All checks passed!

python cs2_ai_director.py --check
pydirectinput 1.0.4 OK / keyboard 0.13.5 OK / pygetwindow 0.0.9 OK / openai 3.35.1 OK / tkinter 8.6 OK
```

补充实测：

- dry-run 引擎：freezetime 出生点运镜 1 次、炸弹 6.5s 包点运镜 1 次、手动暂停 9.9s、GSI 正常；
- GUI 1000×780 构造成功；“设置 / 日志”两页正常；
- 注入状态刷新异常后 `_poll` 未中断，并写入“状态刷新异常”日志；
- 对比冻结基线：主程序 +70/−22 行，测试 +222 行（32 → 45 项）。

---

## 5. 仍需你实机验证（⚠️）

1. **真实 GSI**：用 MLAE 启动 CS2，进入观战；GUI 应显示“GSI 已连接（x.xs 前，收包 N）”。若一直等待：确认 cfg 在 `game/csgo/cfg`、端口一致、完全重启游戏；必要时在启动项加 `-gamestateintegration`。
2. **焦点与按键**：游戏在前台时观察自动切人；Alt-Tab 到桌面后再等一次切换，确认程序能拉回游戏或日志记录“窗口未聚焦/激活失败”并跳过。
3. **HLAE 两个宏**：freezetime 观察出生点运镜是否只在每回合第一次触发；炸弹剩 8 秒内观察包点运镜是否只触发一次，且 `bomb_cam_grace` 的 1.5 秒让位符合预期。
4. **手动接管**：自动导播中按 4/7 等数字键，确认 AI 暂停约 10 秒且不抢回；连续按不同数字应重新计时。
5. **覆盖确认**：再点一次“一键安装 GSI”，确认弹窗出现；选“否”不写文件，选“是”生成 `.bak_时间戳`。
6. **DeepSeek**：填入 Key，制造一次规则前两名接近的对枪，日志应出现“AI 仲裁建议 slot=…”；断网/清空 Key 后应继续用规则引擎，不中断导播。
7. **断流**：导播中完全退出 CS2，状态应变为“断流”且不再发键；重新进入观战应自动恢复。
8. **DPI**：若系统缩放 125%/150%，确认 1000×780 窗口完整显示（设置页与日志页可切换）。

> 说明：本机已安装 CS2，但自检时游戏未运行，无法替你验证真实键击送达、GSI 字段实际内容与 HLAE 宏触发；这三项是唯一无法自动化的部分。

---

## 6. v1.1 增量：HLAE 快捷键单/双击

- 新增 GUI 每项一个“单次/双击”下拉；默认“双击”，对应指南 `alt+按键` 的连按两次交互。
- `InputController.send_hotkey(hotkey, double=True)` 会连按两次，间隔 `hotkey_double_interval`（默认 0.12s，可调 0.03–0.5）。
- 手动接管现在只认**裸 0–9**：`GetAsyncKeyState` 检测到 Alt/Ctrl/Shift/Win 时直接忽略，因此 `alt+1` 运镜宏不会误暂停 AI 10 秒。
- GSI 路径较长时状态标签自动换行；窗口调整为 1000×800，避免长路径把设置页撑出屏幕。
- 新增测试：
  - `test_send_hotkey_single_and_double`（1 次 / 2 次调用）
  - `test_alt_digit_is_not_manual_takeover`（Alt+1 忽略、裸 1 生效）
- 回归：`Ran 47 tests ... OK`；`ruff` 无告警。
- 需实机确认：HLAE 的宏到底要“单次”还是“双击”；若双击无反应，把 `hotkey_double_interval` 调到 0.15–0.25；若触发两次则改回单次。

---

## 7. v1.2 增量：A/B 包点独立快捷键

- GUI 拆成“出生点 / A 包点 / B 包点”三项，各自可选单次/双击。
- 按 `map.name` + `bomb.position` 自动判定包点：
  - Mirage、Inferno、Dust2、Anubis、Ancient、Cache 用 x/y 最近参考点；
  - Nuke 的 A/B 上下重叠，用 3D 距离。
- 参考点由雷达图包点红色区域反推，并用相机包中带 A/B 标签的路径点交叉验证（7 张图全部命中正确一侧）。
- 未收录地图：日志提示一次并跳过，不会用错相机；可在 `config.json` 的 `bomb_site_refs` 补充。
- 旧配置的单个 `hlae_bomb_key` 自动迁移到 A/B 两项，模式沿用旧值。
- 新增测试：
  - `TestBombSiteDetection`（14 个实际点位、未知地图、缺失位置、自定义覆盖）
  - `TestBombTriggerSelection`（A→A 键/双击，B→B 键/单次，未知地图跳过不重试）
  - `test_legacy_single_bomb_key_migrates_to_ab`
- 回归：`Ran 54 tests ... OK`；`ruff` 无告警；GUI 1000×830 构造正常。
- 需实机确认：分别在 A/B 安包，确认触发对应相机；若某图判反，对调 `bomb_site_refs` 的该图 A/B 坐标。

---

## 8. v1.4 增量：位置缺失、双击语义、POV 优先

### 8.1 “缺少 allplayers_position”根因

本机 `...\game\csgo\cfg` 下同时存在：

- `gamestate_integration_ai_director.cfg` → `http://127.0.0.1:3000`（含 `allplayers_position`）
- `gamestate_integration_yuzi_cup.cfg` → `http://localhost:3000/`（**没有** `allplayers_position`）

CS2 会把两份配置都发到 3000；本程序原来每收到一包就覆盖快照，收到 yuzi 的精简包时位置就“消失”了。
所以重装 GSI、加 `-gamestateintegration` 都不会解决。

修复：

- 只接受带 `provider` 的包；
- 若已有带位置的快照，拒绝“有存活玩家但没有任何 position”的包；
- 启动/安装时扫描同目录其他 GSI 配置，提示端口冲突；
- 仍建议给本程序独立端口（如 32100），不要和 HUD 工具共用。

### 8.2 双击语义

按实际规则改为：**按住 Alt，短按主键两次，再松开 Alt**（不是两次完整 alt+1）。

- 主键按住 `hotkey_press_hold=0.045s`，两次之间 `hotkey_double_interval=0.12s`；
- 避免 HLAE/相机工具用 `GetAsyncKeyState` 轮询时漏掉过短的 keydown；
- 日志改为“已发送”，不再声称“触发成功”（无法从外部确认 HLAE 是否接收）。

### 8.3 POV 优先

- 新增 `bomb_cam_policy`，默认 `pov_first`：只要还有存活玩家，就跳过包点运镜，让 AI 继续切 POV；
- `always` 恢复旧行为；
- 无人存活时才触发包点运镜。

### 8.4 回归

```text
Ran 59 tests ... OK
ruff: All checks passed!
```

新增测试：端口冲突扫描、外来无位置包不覆盖快照、A/B 键选择、POV 优先/全死/always 三种策略、
长按 Alt 双击的按键序列。

仍需实机确认：独立端口后 GSI 位置是否稳定、HLAE 是否接受长按 Alt 双击、以及 POV 优先的体感。

---

## 9. v1.5 增量：KeyValues 配置与 CT/T 出生点

### 9.1 一直“等待数据”的真正原因

旧版 `install_gsi_config` 写的是 **JSON**：

```json
{ "uri": "http://127.0.0.1:3000", "data": { ... } }
```

CS2 的 `gamestate_integration_*.cfg` 只认 Valve KeyValues：

```kv
"CS2 AI Director"
{
    "uri" "http://127.0.0.1:3000"
    "data" { "provider" "1" ... }
}
```

JSON 配置会被静默忽略，所以即使 `-gamestateintegration`、重装、重启都无效；这也是为什么日志里只看到 yuzi 的包、
看不到本程序的 `provider`。v1.5 改写为 KeyValues，安装时回读校验 `allplayers_position` 和 URI。

### 9.2 出生点 CT/T

- 新增 `hlae_spawn_ct_key` / `hlae_spawn_t_key` 及各自模式；
- `Snapshot.round_win_team` 读取 `round.win_team`；缺失时用比分增量兜底；
- 进入 freezetime 时按 `_last_round_winner` 选择 CT/T；第一回合无记录用 `spawn_cam_default_side`（默认 CT）；
- 换图会重置胜方记录；
- 旧配置的 `hlae_round_key` 自动迁移到 CT/T 两项。

### 9.3 回归

```text
Ran 65 tests ... OK
ruff: All checks passed!
```

新增测试：KeyValues 文本格式、安装回读、CT/T 触发选择、默认边、`win_team`、比分兜底、旧出生点键迁移。
仍需实机确认：重装后 GSI 是否变为“已连接”、CT/T 方向是否符合上一回合胜方。

---

## 10. v1.6 增量

### 10.1 观战判定移除

- 删除“当前不是观战视角，已跳过自动切换”的分支，也删除运镜里的同类保护；
- 原因：CS2 某些观战状态下 `observer_slot` 缺失，导致一直不切；
- 风险：正常游玩时启动会按数字键切武器，请只在观战/导播机使用。

### 10.2 阵亡回切

- 当前 `observer_slot` 对应选手阵亡时，按“离尸体最近的敌人”切过去（通常就是击杀者）；
- 即使没有任何“即将交火”的候选，也会执行一次回切；
- 受 200ms 硬间隔与同目标 2 秒保护。

### 10.3 运镜按键与时序

- 快捷键改为下拉：ALT/SHIFT/CTRL 三选一 + A–Z/0–9；
- 双击时序：Alt 按下 → 80ms → N 30ms → 50ms 后第二次 N 30ms → 50ms → 松开 Alt；
- `hotkey_double_interval=0.08` 表示两次主键**按下**的间隔，`hotkey_press_hold=0.03`。

### 10.4 无交火与 POV 优先

- `has_possible_engagement`：存活敌我对距离 > 约 2600 或楼层差 > 300 视为不可能交火（Nuke 放宽到 900）；
- `bomb_cam_policy=pov_first` 只在“仍有可交火目标”时跳过包点运镜；远距离/跨楼层时允许包点运镜；
- 评分层同样跳过跨楼层配对。

### 10.5 更偏向高胜率

- 胜率权重从 `0.30 + 0.70*win` 提升到 `0.25 + 0.75*win`；
- 双方“最近 1.6 秒掉血”都计入紧迫度（正在对枪/换血）。

### 10.6 200ms 硬间隔

- `hard_min_switch_interval=0.2`：任意两次按键切换至少间隔 200ms，优先于其他冷却判断；
- 阵亡回切同样受此限制。

### 10.7 回归

```text
Ran 74 tests ... OK
ruff: All checks passed!
```

新增测试：阵亡回切、远距离/跨楼层无交火、包点运镜放行、200ms 硬间隔、可选快捷键解析。

仍需实机确认：运镜是否按 80/30/80/30/50ms 触发；阵亡回切是否切到击杀者；正常游玩时不要启动自动切换。

---

## 11. v1.7 增量：在线学习与 demo 训练

> v1.9 已移除即时学习；本节只保留历史记录。当前静态 demo 模型仍可用。

### 11.1 在线学习（已移除）

- 数据：每个选手 `state.round_kills` 的增量；
- 窗口：切人后 `learning_window=2.5s` 内；
- 标签：击杀者=1，同一时刻其他候选=0；没有击杀则丢弃；
- 模型：8 特征逻辑回归，SGD 更新；
- 安全：`samples < learning_min_samples=20` 时输出 0.5，完全不改变排序；
- 影响：只对规则分乘 `1 ± learning_blend/2`（默认 ±12.5%），不会推翻强规则；
- 持久化：`%APPDATA%\CS2AIDirector\learner.json`，GUI 可关闭。

### 11.2 demo 训练

`train_from_demos.py` 使用与在线学习完全相同的 `_candidate_features`：

- 解析 `player_death`；
- 回看 2.5 秒（64tick×160）；
- 构建所有存活候选；
- 击杀者=1，其他=0；
- 训练后写同一 `learner.json`。

用本机 3 个 demo 实测：

```text
105 / 104 / 117 个有效击杀事件，544 / 540 / 540 条候选
验证 top-1：37/82 = 45.1%
权重：aim_self +1.17, aim_enemy +0.52, win +1.30,
      enemy_low_hp +0.06, self_low_hp -0.53,
      proximity +1.47, enemy_damaged 0.00, hp_advantage -0.40
```

（top-1 是“4–5 个候选中选中击杀者”的准确率，随机约 20–25%；仍受 demo 质量与特征限制。）

### 11.3 回归

```text
Ran 77 tests ... OK
ruff: All checks passed!
```

新增测试：学习者正/负样本、无击杀不训练、权重持久化、在线学习样本门槛。

仍需实机确认：demo 训练样本量足够后 top-1 是否继续上升。

---

## 13. v1.9：决策日志与随机森林仲裁

### 13.1 即时学习移除

- `KillLearner` 的 `poll/on_switch/_learn` 全部删除；
- 不再在运行中修改权重，不写 `learner.json`；
- 保留 `DemoModel`：只加载 `train_from_demos.py` 生成的静态权重；
- GUI 文案改为“启用 demo 模型（离线权重，不即时更新）”。

### 13.2 决策 CSV

每次切换，以及每 1 秒一次的保持决策，写入：

```text
%APPDATA%\CS2AIDirector\decisions.csv
```

字段包含 map/round/action/target_slot/current_slot/reason/rule_score/目标血量/距离，
以及 8 个特征：aim_self、aim_enemy、win、enemy_low_hp、self_low_hp、proximity、
enemy_damaged、hp_advantage。

### 13.3 3 秒标签

- 记录时保存目标 `round_totaldmg` 与 `round_kills` 基线；
- 3 秒后差值 > 0 → label=1（造成伤害或击杀）；
- 回合切换则丢弃该待标注样本，避免 `round_totaldmg` 重置造成假标签。

### 13.4 训练与仲裁

训练器已拆到独立仓库：

https://github.com/YuGaN525/cs2-ai-decision-trainer

- 随机森林（300 棵树，max_depth=8，class_weight=balanced）；
- 输出 AUC、混淆矩阵、分类报告、特征重要性；
- 模型写入 `decision_model.pkl`；
- 导播只在规则前两名分差 ≤ `decision_model_margin` 时用模型重排；
- 模型推荐目标的规则分不得低于第一名 × `decision_model_min_factor`；
- 之后仍走阈值/迟滞/200ms 硬间隔，模型不能绕过安全约束。

### 13.5 回归

```text
Ran 80 tests ... OK
ruff: All checks passed!
```

新增测试：决策 CSV 正/负标签、事件动作、日志过滤。
仍需实机确认：至少积累 1000 条样本后训练，检查 AUC 与线上仲裁是否真的减少误切。

---

## 14. v1.9 exe 打包自检

### 14.1 结果

```text
PyInstaller 6.22.3 / Python 3.14.0 / Windows x64
dist\CS2AIDirector.exe  约 35 MB
启动后窗口标题：CS2 AI 导播 v1.9.0
依赖状态：pydirectinput / keyboard / pygetwindow / openai / tkinter 均通过
```

### 14.2 打包策略

- onefile + windowed（无控制台）；
- 内置 `obsws_python` / websocket；
- 排除 `sklearn` / `joblib` / `demoparser2` / pandas / matplotlib，控制体积；
- 随机森林通过 `decision_model.json.gz` 纯 Python 加载，exe 不需要 sklearn；
- 训练脚本仍需要 Python + scikit-learn。

### 14.3 仍需确认

- exe 未数字签名，SmartScreen 会提示；
- 打包后的管理员权限需要右键运行（键盘钩子/前台激活更稳）；
- 打包后的 OBS 真实连接、FFmpeg 实际剪辑尚未在本机联调（OBS 未开 WebSocket、ffmpeg 未安装）；
- 训练脚本不在主 exe 内；现已提供独立的 `CS2AIDecisionTrainer.exe`。

---

## 15. v1.9.1：独立训练器（已拆仓库）

训练器源码与发布已移到：

https://github.com/YuGaN525/cs2-ai-decision-trainer

本仓库只保留主程序和静态 demo 模型；以下记录保留作历史。

### 15.1 产物

```text
dist\CS2AIDecisionTrainer.exe  约 179 MB（onefile，内置 sklearn + demoparser2 + pandas）
dist\CS2AIDirector.exe         约 35 MB（不打包 sklearn）
```

训练器默认：

```text
输入：%APPDATA%\CS2AIDirector\decisions.csv
输出：%APPDATA%\CS2AIDirector\decision_model.pkl / .json.gz / _metrics.json
```

### 15.2 训练器

- GUI 选择 CSV、输出目录、最少样本、树数量、最大深度、验证集比例；
- GUI 可直接选 `.dem` 文件/文件夹，生成 `demo_decisions.csv` 后训练；
- demo 回看 160 tick，击杀者 label=1、同刻其他候选 label=0；
- 少于最少样本时拒绝训练；
- 显示 AUC、混淆矩阵、分类报告、特征重要性；
- 输出纯 JSON 森林，主程序无需 sklearn；
- 支持 `--cli` 参数，便于自动化验证。

已实测：

```text
dist\CS2AIDecisionTrainer.exe --cli --csv ... --out ... --min-samples 100 --trees 50
exit=0
生成 decision_model.pkl / decision_model.json.gz / metrics.json

dist\CS2AIDecisionTrainer.exe --cli --demos "...4081612816...dem" --min-samples 300
exit=0
demo_decisions.csv：544 行 / 105 个击杀事件
decision_model.json.gz：31 KB
```

首次打包时排除了 pandas，导致 `demoparser2.parse_ticks()` 在 exe 内报
`ModuleNotFoundError: pandas`；已把 pandas 纳入训练器打包并复测通过。

### 15.3 主程序读取

- 启动时加载 `decision_model.json.gz`；
- 每 5 秒检查文件 mtime，训练器完成后自动重新加载；
- “专业导播”页提供“打开训练器…”和“重新加载模型”按钮；
- 已用临时模型验证：启动加载成功、覆盖文件后 `reload_if_changed()` 返回 True。

### 15.4 回归

```text
Ran 80 tests ... OK
两个 exe 均启动成功
窗口标题：CS2 AI 导播 v1.9.0 / CS2 AI 决策模型训练器 v1.0.0
训练器 SHA256：37D5E6D8B3B54B70291BA3EFF0D3ACBBDE100A9380CF55238C0BCC0D0FA849CC
```

---

---

## 16. v1.10 Phase 1：镜头时间线 / 专业指标 / 回合故事

### 16.1 结果

```text
python -m unittest test_cs2_ai_director
Ran 86 tests ... OK

python -m ruff check --select F,E9 cs2_ai_director.py director_story.py test_cs2_ai_director.py
All checks passed!

python -m py_compile cs2_ai_director.py director_story.py
OK
```

新增 6 项测试：

- `TestStoryTracker.test_economy_clutch_post_plant_match_point`；
- `TestStoryTracker.test_phase_transitions_and_events`；
- `TestShotTimeline.test_timeline_kill_coverage_corpse_cut_rate_manual`；
- `TestShotTimeline.test_kill_coverage_zero_when_watching_other_player`；
- `TestShotTimeline.test_disabled_does_not_create_files`；
- `TestEngineTimelineIntegration.test_tick_updates_story_timeline_and_damage_events`。

### 16.2 已验证

- 模拟 GSI 序列：POV 切换 → 首次伤害 → 击杀 → 阵亡回切 → 人工接管 → 回合结束；
- `round_metrics.csv` 的 `kill_coverage=1.0`、`pre_fight_coverage=1.0`、`corpse_time=0.5`、
  `shot_duration_mean=2.0`、`cut_rate=36/min`、`manual_takeover=1` 与手算一致；
- 镜头不是被观察时击杀者 → `kill_coverage=0.0`；
- `shot_timeline.csv` / `shot_events.csv` / `round_metrics.csv` / `director_metrics.json` 均生成；
- `shot_timeline_enabled=false` 时不创建文件；
- 引擎 `_tick` 能在不传 `timeline` 时保持旧行为（原有 80 项测试全过）。

### 16.3 未验证 / 限制

- 真实 GSI + HLAE 下的镜头类型/归还时间（GSI 不提供 HLAE 当前机位）；
- 真实比赛的经济分档与残局判定；
- `pre_fight_coverage` 与人工标注的一致性；
- OBS/FFmpeg 的 `REPLAY` 机位尚未接入 timeline；
- 回合中停止/重开会产生 `completed=0` 的 partial 行，累计指标包含这些行；
- 训练器仓库已同步 v1.10.4 共享核心（`792e4a3`）；训练器 exe 未重建，指标不作为训练特征。

### 16.4 exe

已重新打包：

```text
PyInstaller 6.22.3 / Python 3.14.0 / Windows x64
dist\CS2AIDirector.exe
大小：35,036,402 字节
SHA256：A5E5104470C8D79CCBE56A7D4310AE9F74873E3CD5EF752B6A601AB053604F17
启动窗口标题：CS2 AI 导播 v1.10.0
```

构建命令：

```bat
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

> `dist/` 被 .gitignore 排除；同一 exe 已上传到 GitHub Release `v1.10.0`：
> https://github.com/YuGaN525/cs2-ai-director/releases/tag/v1.10.0

---

## 17. v1.10.2：observer_slot/按键映射修正

### 17.1 现象

完整实机反馈：

```text
键 1 → GSI slot 0     键 6 → GSI slot 5
键 2 → GSI slot 1     键 7 → GSI slot 6
键 3 → GSI slot 2     键 8 → GSI slot 7
键 4 → GSI slot 3     键 9 → GSI slot 8
键 5 → GSI slot 4     键 0 → GSI slot 9
```

旧版 `press_slot(slot)` 直接把 GSI slot 当数字键（`str(int(slot))`），导致整体错位；
v1.10.1 的修正方向又写反，v1.10.2 按上表更正。

### 17.2 修正

```text
键 N        → GSI slot (N-1) mod 10
GSI slot S  → 键 (S+1) mod 10
```

- `slot_to_key(0)=1, slot_to_key(1)=2, slot_to_key(3)=4, slot_to_key(4)=5, slot_to_key(9)=0`；
- `key_to_slot(1)=0, key_to_slot(2)=1, key_to_slot(9)=8, key_to_slot(0)=9`；
- 人工接管按下的数字键先转成 GSI slot，再写入 timeline 的 `target_slot`；
- 日志/状态显示 `slot=N（键X）`；timeline/decisions CSV 仍用 GSI slot；
- 不是“改评分”：只改执行层的按键映射和人工接管归因。

### 17.3 回归

```text
python -m unittest test_cs2_ai_director
Ran 90 tests ... OK

ruff F/E9: All checks passed!
```

新增测试：`TestSlotKeyMapping`（完整 10 键表、10 个 slot 往返、人工按键→GSI slot），
并更新 `TestInputIdentity` 的按键期望。

### 17.4 exe

```text
dist\CS2AIDirector.exe
大小：35,036,437 字节
SHA256：2EFCEDA544B23DD1359E5E66F78EE704748796706F0B7FC17D1B9E5104C03AB1
窗口标题：CS2 AI 导播 v1.10.2
```

Release：https://github.com/YuGaN525/cs2-ai-director/releases/tag/v1.10.2

---

## 18. v1.10.3：防碎切 / 交火锁定 / HLAE 权限诊断

### 18.1 问题

1. 镜头切换过于细碎；
2. 当前 POV 正在与敌人交火时不应切走；
3. 日志显示 HLAE 运镜“已发送”，但游戏里未触发；CS2 由 MULNX 启动，手动测试正常。

### 18.2 修正

- **最短停留**：`min_shot_duration=2.0`，与 `min_dwell_seconds` 取较大值；
- **切率上限**：`max_cuts_per_10s=3`，10 秒窗口内超过 3 次则保持；
- **交火锁定**：`fight_lock_enabled=true`，当前 POV 满足以下任一条件时不切：
  - 当前玩家正在拆包；
  - 敌人在当前武器射程包络内，且任一方最近 1.6s 掉过血；
  - 双方都朝对方瞄准（`fight_lock_aim=0.60`）。
- **死亡回切例外**：当前观察者阵亡时仍立即切到最近敌人，不受最短停留/切率上限限制；
- **HLAE 权限诊断**：启动时打印“权限=管理员/普通用户”；非管理员时警告
  “CS2/HLAE 若以管理员运行，UIPI 会拦截模拟按键，日志已发送但游戏无反应”；
  运镜日志追加“若画面未变化：确认管理员权限/单次双击”。
- 以上均为执行层约束，没有改 `score_candidates` / `choose_slot` / 模型特征。

### 18.3 回归

```text
python -m unittest test_cs2_ai_director
Ran 93 tests ... OK

ruff F/E9: All checks passed!
```

新增测试：

- `TestDirectorStability.test_fight_lock_blocks_switch_away`；
- `TestDirectorStability.test_min_shot_duration_blocks_immediate_switch`；
- `TestDirectorStability.test_max_cuts_per_10s_blocks_switch`。

### 18.4 exe / Release

```text
dist\CS2AIDirector.exe
大小：35,040,575 字节
SHA256：1B2AC658DF9373D464D4A1D2C8C0C1DC2AFC5FABCC3458E14EF370123DD63D2D
窗口标题：CS2 AI 导播 v1.10.3
```

Release：https://github.com/YuGaN525/cs2-ai-director/releases/tag/v1.10.3

### 18.5 仍需实机确认

- 当前 POV 交火锁定的实际时长是否合适（`fight_lock_aim` / 掉血窗口）；
- 2s 最短停留 + 3 次/10s 是否仍偏快或偏慢；
- MULNX 启动场景下，本程序以管理员运行时 HLAE 是否正常；
- 若仍不触发，尝试把该快捷键从“双击”改为“单次”，并确认 HLAE 内绑定。

---

## 19. v1.10.4：MulNX 连击缓冲时序适配

### 19.1 现象

- 导播以管理员运行，CS2 由 MULNX/CS2Injector 启动；
- 导播日志显示 HLAE/MulNX 出生点运镜“已发送”，游戏画面不变化；
- 手动按 Alt+N 可以正常触发；
- 导播与 MulNX 都改成“单次”仍不触发。

### 19.2 根因（读 MulNX 源码）

MulNX 的 `source/MulNX/Systems/InputSystem/InputSystem.cpp`：

```text
每 10ms: GetAsyncKeyState(vkCode)
主键按下沿: ComboClick++
距最后按下 > 200ms: BufferComboClick = ComboClick; ComboClick = 0
CheckWithPack: 先检查 Ctrl/Shift/Alt 当前状态必须完全匹配，
               再 CheckComboClick(vkCode, comboClick)
```

即 MulNX 不是 HLAE 的边沿触发，而是“轮询 + 200ms 缓冲 + 消费时仍要求 Alt 按住”。

旧版 `hotkey_modifier_release_delay=0.05`：第二次按键后 50ms 就松开 Alt。到 200ms
MulNX 准备消费连击时，Alt 已经松开，`CheckWithPack` 的 Alt 检查失败，所以永远不触发。

补充核查：CS2 进程 `TokenIsElevated=False`，因此本问题不是 UIPI/管理员权限。

### 19.3 修正

- 新增 `hotkey_modifier_hold_after=0.30`：最后一次按键后保持 Alt 300ms 再松开；
- 实际保持时间取 `max(hotkey_modifier_release_delay, hotkey_modifier_hold_after)`，兼容旧配置；
- 导播“单次/双击”必须与 MulNX 运镜包/宏 YAML 的 `comboClick` 完全一致；
- 这是输入时序修复，不改评分/模型。

### 19.4 回归

```text
python -m unittest test_cs2_ai_director
Ran 95 tests ... OK

ruff F/E9: All checks passed!
```

新增测试：`test_hotkey_modifier_hold_default_supports_mulnx`、
`test_hotkey_holds_alt_after_last_press`（断言最后 sleep 0.30s）。

### 19.5 exe / Release

```text
dist\CS2AIDirector.exe（已替换为 v1.10.4）
大小：35,039,316 字节
SHA256：358364B65296961C014C5764F37C6C95886F4A76BBC19FC35DDEE133B596D2D1
窗口标题：CS2 AI 导播 v1.10.4
```

Release：https://github.com/YuGaN525/cs2-ai-director/releases/tag/v1.10.4

> 构建时旧 v1.10.3 仍在运行，因此先把新 exe 构建为 `CS2AIDirector_v1104.exe`，
> 再复制替换为 `CS2AIDirector.exe`；旧文件备份为 `CS2AIDirector_v1103_backup.exe`。
> 正在运行的旧进程需要关闭并重新启动才会使用 v1.10.4。

---

## 20. v1.10.5：移除 FFmpeg 回放与玩家改名

### 20.1 删除范围

- GUI“专业导播”页：
  - 删除“录制 / FFmpeg 回放”整块（ffmpeg 路径、输出目录、游戏/回放场景、回放源、开始录制、记录击杀点、生成回放）；
  - 删除“玩家改名（赛事 API → HLAE 命令）”整块（API BaseUrl、获取命令并复制）。
- director_pro.py：
  - 删除 ClipSegment、merge_clips、build_clip_commands、FfmpegService、ReplayManager；
  - 删除 build_rename_commands；
  - 删除事件动作 start_recording / record_kill / generate_replay；
  - 删除 OBS 的录制/媒体源写入/媒体结束轮询方法；
  - 导播日志类别缩为 GSI / OBS / EventAction / App。
- 配置默认值删除 ffmpeg_path、replay_output_dir、obs_game_scene、obs_replay_scene、
  obs_replay_source、player_api_base_url。
- timeline 删除未使用的 REPLAY 类型，保留 POV/BOMB/SPAWN/OVERVIEW。

保留：OBS WebSocket 连接、场景切换、媒体源播放/停止、事件动作规则。

### 20.2 回归

```text
python -m unittest test_cs2_ai_director
Ran 94 tests ... OK

ruff F/E9: All checks passed!
GUI 构造 smoke test: GUI_OK 1.10.5
```

删除 1 项旧的 test_merge_clips；事件动作测试改为播放/停止媒体源 + 切换场景。

### 20.3 exe / Release

```text
dist\CS2AIDirector.exe（已替换为 v1.10.5）
大小：35,026,519 字节
SHA256：373B53E613AB74823EB65990B86F9DC9F90C0C9698D2C8696E9E6EAF9207BC30
窗口标题：CS2 AI 导播 v1.10.5
```

Release：https://github.com/YuGaN525/cs2-ai-director/releases/tag/v1.10.5

> 旧 v1.10.4 exe 备份为 `dist\CS2AIDirector_v1104_backup.exe`。

## 12. v1.8：专业导播整合

### 12.1 来源与边界

参考 `bilibili-CLY/CS2-Director-Tool`（C#/Avalonia）的**行为**重写为 Python，未复制其代码；
对方仓库没有 LICENSE，因此不直接搬运 C# 源码。

### 12.2 已实现

- **独立日志**：`director_pro.ProLog`，类别 GSI/OBS/FFmpeg/Replay/EventAction/Player/App；
  GUI 独立“导播日志”页，支持类别/级别/时间窗口/关键字过滤。
- **事件动作**：13 类 GSI 事件；6 种动作；多动作规则；启用/禁用；JSON 预设保存/加载。
- **OBS**：连接/断开、场景自动创建/切换、媒体源创建/更新、媒体播放/停止/重播、录制状态、媒体结束轮询。
- **FFmpeg 回放**：首杀 -4s/5s、后续 -1s/2s，片段合并、重编码、concat。
- **玩家改名**：`GET /api/v1/players/steam/{steam64}` → `mirv_replace_name byXuid add`，剪贴板。
- **GUI**：新增“专业导播”“导播日志”两页；原有 AI 设置、日志、运镜、学习不变。

### 12.3 回归

```text
Ran 81 tests ... OK
ruff: All checks passed!
```

新增测试：日志过滤、事件检测（回合/击杀/炸弹）、片段合并、事件动作执行。

### 12.4 仍需实机确认

- 本机没有运行中的 OBS/FFmpeg，OBS 请求与 FFmpeg 命令只做了接口/命令级测试；
- 需确认你的 OBS 场景名、媒体源名与配置一致；
- 玩家改名 API 默认是魔女杯，换赛事要改 BaseUrl；
- OBS 密码与原来一样明文保存在 `config.json`。

未做到与 C# 原版逐像素 UI 对齐，但功能面（日志/事件动作/OBS/回放/改名）已接入同一进程。
