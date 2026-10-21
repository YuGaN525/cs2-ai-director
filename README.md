# Yuzi_CS2_AIDirector

**Yuzi Cup 专用 CS2 AI 专业导播** —— 只读取 CS2 GSI 数据的自动观战导播：

- 规则引擎（可选 DeepSeek 仲裁）自动切到“未来 1–3 秒最可能拿到击杀”的选手第一视角；
- `freezetime` 按上一回合胜方触发 CT 或 T 出生点运镜宏；
- 炸弹剩余 ≤ 8 秒按 A/B 包点触发对应 HLAE 包点运镜宏；
- 观察对象阵亡后自动切到离尸体最近的敌人（通常是击杀者），不会停在尸体视角；
- 即时学习已移除；保留离线 demo 静态权重，并新增决策 CSV 记录 + 随机森林离线仲裁模型；
- 专业导播：独立导播日志、事件动作规则、OBS WebSocket 场景/媒体控制；
- **v1.10 Phase 1**：镜头时间线（ShotTimeline）+ 六项专业指标 + 回合故事状态（阶段/经济/残局/下包/比分），不参与评分；
- **v1.10.2**：修复 GSI `observer_slot` 与观察者数字键错位（键 N→slot N-1 mod 10），日志/GUI 同时显示 slot 与键；
- **v1.10.3**：防碎切（最短停留 2s + 10 秒切率上限 3）+ 当前 POV 交火/拆包锁定不切；HLAE 增加管理员权限检测与提示；
- **v1.10.4**：适配 MulNX 的 200ms 连击缓冲——最后一次按键后 Alt 保持 300ms 再松开，修复“日志已发送但运镜不触发”；
- **v1.10.5**：移除 FFmpeg 回放、录制/击杀点/生成回放动作和玩家改名；只保留 OBS 场景/媒体事件动作；
- **v1.10.6**：品牌化为 `Yuzi_CS2_AIDirector` / `Yuzi_CS2_AIDecisionTrainer`，加入鱼籽杯图标、深色页头与卡片式界面；
- **v1.10.7**：训练器按回合/事件分组切分、模糊样本加权、top-1 评估；demo 标签对齐伤害窗口；仲裁增加概率边际；
- **v1.10.8**：勾选框改为原生 √；修复保存配置后事件规则不同步到 OBS 事件线程；增加“测试选中规则”和 OBS 媒体源/场景名校验；
- **v1.10.9**：修复比赛结束时 `phase_countdowns="over"` 覆盖 `map.phase="gameover"`，导致“比赛结束”事件不触发；增加 GSI 阶段切换日志；
- **v1.10.10**：GSI 不提供暂停状态，新增暂停热键/按钮，手动派发 `pause_started/pause_over`；
- **v1.10.11**：修复 CS2 在 over 阶段提前改 round 导致下一回合被立即结算、真实指标丢失；新增回合边界回归测试；
- tkinter GUI：检测/选择 `cs2.exe`、一键安装 GSI 配置、录入快捷键与 API Key、持久化配置。

> 安全边界：程序**只**读取 GSI HTTP 推送。对游戏的动作只有“模拟 0–9 数字键”和“模拟 HLAE 快捷键（出生点 + A/B 包点）”，不读内存、不注入、不发送游戏控制台命令。

---

## 0. 直接运行 exe（推荐）

本仓库 Release 只提供 AI 导播主程序：

| 程序 | 用途 |
|---|---|
| `Yuzi_CS2_AIDirector.exe` | AI 导播主程序 v1.10.11，约 36 MB（Release v1.10.11） |

当前本地构建：35,955,842 字节，SHA256 `C3E40F98FA1E6B389536C717A8E27A0B96B0A98EFB9CC85B71632B2D372014FC`。

- 不需要安装 Python；
- 双击即可，建议右键“以管理员身份运行”；
- 配置、日志、`decisions.csv`、`learner.json`、`shot_timeline.csv`、`round_metrics.csv` 仍在 `%APPDATA%\CS2AIDirector\`；
- 主程序内置 OBS WebSocket 依赖，但不打包 scikit-learn；它读取训练器生成的
  `decision_model.json.gz`；
- 未签名，Windows SmartScreen 可能提示，选择“更多信息 → 仍要运行”。

**训练器已拆到独立私密仓库**：

https://github.com/YuGaN525/cs2-ai-decision-trainer

训练器支持 `decisions.csv` 和 `.dem`，输出 `decision_model.json.gz`。
主程序每 5 秒检测文件更新，训练完无需重启；也可在“专业导播 → 重新加载模型”。

v1.10.7 的共享核心（`director_story.py` / `cs2_ai_director.py` / `decision_model.py`）已同步到训练器仓库；
但 `kill_coverage` 等六项指标只用于评估，不进入 8 特征击杀预测模型，也不需要重训。

自行重新打包主程序：

```bat
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

产物：`dist\Yuzi_CS2_AIDirector.exe`。

## 1. 环境与安装

- Windows 10/11，Python 3.10+（本机验证：Python 3.14.0 / Windows 11）
- 建议以管理员身份运行（全局键盘钩子、向游戏注入按键更稳）

```bat
python -m pip install -r requirements.txt
```

或：

```bat
python -m pip install pydirectinput keyboard pygetwindow openai
```

自检：

```bat
python cs2_ai_director.py --check
```

启动：

```bat
python cs2_ai_director.py
```

也可以双击 `start_cs2_director.bat`（优先用 `pythonw` 无控制台启动）。

---

## 2. 首次配置（按顺序）

1. **选择 cs2.exe**：点“自动检测”（注册表 SteamPath/InstallPath + `libraryfolders.vdf` + 常见盘符 `SteamLibrary`），或“浏览…”手动选择。
2. **选择 HLAE 快捷键**：CT 出生点、T 出生点各 1 个，包点 A/B 各 1 个。每项用下拉选择 **ALT / SHIFT / CTRL** 之一 + **A–Z / 0–9** 之一，再选择“单次/双击”；不再支持自由输入。出生点按**上一回合胜方**选择 CT/T；包点按炸弹安放位置判定 A/B；未选择完整会被拒绝启动。
3. **一键安装 GSI**：程序从 `cs2.exe` 向上找到 `game/csgo/cfg`，写入
   `gamestate_integration_ai_director.cfg`。已存在同名文件会弹窗确认，并自动备份为 `.bak_时间戳`。
4. **完全退出并重启 CS2**（不是退回主菜单）。
5. **可选**：填写 DeepSeek API Key，勾选“启用 AI 仲裁”。
6. 先勾选“演练模式（不实际发送按键）”启动，观察日志/状态是否正常，再取消演练正式使用。

GSI 配置端口必须与 GUI 里“GSI 端口”一致（默认 3000）。改端口后要重新“一键安装 GSI”并重启游戏。

> **端口不要和其他 GSI 工具共用。** CS2 会把同一份状态发给所有 `gamestate_integration_*.cfg` 的 URI。
> 如果另一个配置（例如 HUD/导播工具）也指向 3000 且没有开启 `allplayers_position`，它的精简数据包会覆盖本程序的快照，
> 表现为间歇性“缺少 allplayers_position”。程序会忽略这类不完整包并在日志/安装时提示冲突；最稳妥的做法是给本程序一个独立端口（如 32100）。

---

## 3. 使用

1. 进入 CS2 观战（GOTV / demo / 死亡后观战），确认数字键 0–9 能手动切第一视角。
2. 回到导播程序点“启动导播”，再切回游戏。
3. 状态区会显示：GSI 连接状态、阶段、当前观察 slot、AI 目标、手动接管倒计时、运镜触发次数。
4. 你随时按**裸 0–9**（不带 Alt/Ctrl/Shift/Win）手动接管：AI 自动暂停 10 秒（可配置），期间不会抢你的视角。
   `alt+数字` 是指南里的运镜宏，不会被当成手动接管。
5. 点“停止”结束；配置会自动保存到
   `%APPDATA%\CS2AIDirector\config.json`，日志在 `%APPDATA%\CS2AIDirector\director.log`。

> **不要把 GSI slot 直接当按键。** 实测 CS2 的 `observer_slot` 与观察者数字键错开一位：
> 按数字键 `N` 实际选中 GSI slot `(N-1) mod 10`。所以程序发送按键时使用反推映射：
>
> ```text
> GSI slot 0 → 键 1
> GSI slot 1 → 键 2
> GSI slot 3 → 键 4
> GSI slot 4 → 键 5
> GSI slot 9 → 键 0
> ```
>
> 日志和状态区会同时显示 `slot=N（键X）`。`shot_timeline.csv` / `round_metrics.csv`
> 里的 `target_slot` 仍是 **GSI slot**，不是键盘数字。

### 演练模式

勾选后只计算、只写日志，不发送任何按键/快捷键，适合先验证 GSI 与评分是否符合预期（比如放 demo）。

---

## 4. 算法说明（规则粗筛，可选 AI 仲裁）

对每个可切换的选手 A，取所有存活敌人 B 中威胁最高的一对：

```
score(A, B) = prox^0.55
            × (0.55 + 0.45 × aim_pair)
            × urgency
            × (0.30 + 0.70 × win_prob)
            × readiness
```

- **prox**：以 A 当前武器的有效射程为包络（AK 1500、AWP 2600、手枪 800…），越近越高；
- **aim_pair**：`0.6×max(双方瞄准度) + 0.4×min(...)`，朝向由 GSI 的 `forward` 与 A→B 方向夹角计算；
- **urgency**：任一方残血、B 最近 1.6 秒掉过血（正在交火）、B 正在拆包，都会提高；
- **win_prob**：血量/护甲/武器威力/瞄准差/闪光/是否持枪的 1v1 胜率 logistic 近似；
- **readiness**：拿刀/雷/空仓/拆包时大幅降低。

决策层再叠加：`switch_min_score=0.30` 阈值、`switch_margin=0.10` 迟滞、`min_dwell_seconds=1.0` 冷却、
`repeat_guard_seconds=4.0` 同 slot 重复按键保护；`emergency_score=0.72` 时允许绕过迟滞。
因此“残血对枪”会切，“满血架远点”基本不切，且不会每帧乱按。

**v1.10.3 防碎切层**（在评分/阈值之后、实际发键之前）：

- `min_shot_duration=2.0`：任何新 POV 至少停留 2 秒（与 `min_dwell_seconds` 取较大值）；
- `max_cuts_per_10s=3`：10 秒窗口内最多切 3 次，超过后保持；
- `fight_lock_enabled=true`：当前 POV 正在与敌人交火（距离在射程内 + 任一方近期掉血/双向瞄准）或正在拆包时，**不切走**；
- 当前观察者阵亡时死亡回切不受以上限制，避免停在尸体；
- 这些是执行层约束，不改变 `score_candidates` / `choose_slot` / 模型特征。

**AI 仲裁**：仅当规则前两名分差 ≤ `ai_ambiguity_margin`（默认 0.08）时才请求 `deepseek-chat`，
最多 12 次/分钟、超时 2.5 秒；返回空 content、超时、网络错误、返回候选外 slot 时全部回退规则引擎。
超时/网络错误只请求一次（仅 `response_format` 不兼容时才去掉该参数重试一次），避免双倍等待与重复计费。
AI 建议仍要通过阈值/迟滞/冷却检查，不能绕过安全约束。

**即时学习已移除**：程序不再在运行中更新权重。原来的 demo 训练保留为**静态**权重模型：
```bat
python -m pip install -r requirements-train.txt
python train_from_demos.py --demos "D:\demos" --out "%APPDATA%\CS2AIDirector\learner.json"
```
GUI 的“从 demo 训练…”按钮执行同一脚本；训练后重启导播加载，运行期间不再变化。

**决策日志 + 随机森林仲裁**：每次真正切换（或按 1 秒节流记录一次保持）写一行 CSV：

```text
%APPDATA%\CS2AIDirector\decisions.csv
```

行内包含目标 slot、规则分、8 个候选特征，以及 v1.10.7 新增的
`rule_gap` / `is_ambiguous` / `group` / `group_size`；3 秒后按目标选手的
`round_totaldmg` / `round_kills` 增量回填 `label`（1=造成伤害或击杀，0=没有）。

**v1.10.7 训练器对齐当前导播逻辑**：

- 训练/验证按 `group`（回合或 demo 击杀事件）切分，避免同一回合泄漏；
- 模型只在“规则前两名接近”的样本上实际生效，这些 `is_ambiguous=1` 样本权重更高；
- demo 标签改为“回看窗口内造成伤害/击杀的候选=1”，与在线 `label` 一致，而不是只标击杀者；
- 训练指标增加 **top-1**（候选组内概率最高者是否命中），不再只看 AUC；
- 仲裁增加概率边际：模型推荐目标必须领先当前目标 `decision_model_prob_margin`，且概率 ≥ `decision_model_min_prob`，避免 50.1% 噪声切镜头。

样本达到 1000 条后：

训练器已拆到独立仓库，下载 `Yuzi_CS2_AIDecisionTrainer.exe`：

https://github.com/YuGaN525/cs2-ai-decision-trainer

训练器支持：

- 导入本程序的 `decisions.csv`；
- 直接导入 `.dem` 文件/文件夹，生成 `demo_decisions.csv`；
- 训练后输出 `decision_model.json.gz` 并显示 AUC/特征重要性。

只有当规则前两名分差 ≤ `decision_model_margin` 时，导播才用模型概率重排候选；
模型推荐目标的规则分不得低于当前第一名的 `decision_model_min_factor`，随后仍走原有阈值/迟滞/冷却检查。
导播每 5 秒检查一次模型文件修改时间，训练器写完后自动加载。

**运镜去抖**：出生点运镜按 `map + round` 去重，包点运镜按回合去重；发送失败不标记完成，1 秒后重试。

---

## 4.5 专业导播（OBS / 事件动作）

这部分按 CS2-Director-Tool 的行为独立重写，未复制其 C# 代码；入口是 GUI 的 **“专业导播”** 页。

### 独立日志

“导播日志”页与原来的 AI 日志分开，类别为：

```text
GSI / OBS / EventAction / App
```

支持按类别、级别、时间窗口（1/5/30 分钟）和关键字过滤。

### 事件动作

事件：

```text
比赛开始 / 回合开始 / 回合结束 / 比赛结束 / 热身开始 / 热身结束
暂停开始 / 暂停结束 / 炸弹安装 / 炸弹拆除 / 炸弹爆炸
玩家死亡 / 玩家击杀
```

动作：

| 动作 | 目标 |
|---|---|
| 播放媒体源 | OBS 媒体源名 |
| 停止媒体源 | OBS 媒体源名 |
| 切换场景 | OBS 场景名（不存在会自动创建） |

规则可添加多条、每条可含多个动作，支持启用/禁用；规则可保存为 JSON 预设并重新加载。
典型配置：

```text
暂停开始 → 播放媒体源（暂停音乐）
暂停结束 → 停止媒体源（暂停音乐）
回合结束 → 切换场景
```

### OBS WebSocket

1. OBS 开启 WebSocket Server（默认 4455），在“专业导播”页填地址/端口/密码并连接；
2. 用事件动作播放/停止媒体源、切换场景；
3. OBS 是可选依赖，未配置时 AI 导播、GSI、HLAE 运镜不受影响。

> v1.10.5 起已移除 FFmpeg 回放、录制/击杀点/生成回放动作和玩家改名功能。

## 4.6 镜头时间线、专业指标与回合故事（v1.10 Phase 1）

这一版不训练新模型，也不改评分公式；只把“导播到底拍了什么”变成可记录、可统计的数据。

### 镜头时间线

每个镜头写入 `shot_timeline.csv`：

```text
shot_type     POV / BOMB / SPAWN / OVERVIEW
target        slot=3 P3 / site=A / side=CT
start / end   开始与结束时间
duration      停留秒数
reason        导演/决策原因（切换、阵亡回切、人工接管、运镜…）
source        rule / ai / model / death / manual / bomb_cam / spawn_cam / observed
manual        是否人工接管
story         当时的回合故事快照
```

同一目录还有：

- `shot_events.csv`：击杀、死亡、每段交火首次伤害、人工接管、下包/拆包；
- `round_metrics.csv`：每回合一行，故事状态 + 六项指标 + 分子/分母；
- `director_metrics.json`：本机累计指标与上一回合指标（重启后从 CSV 恢复累计）。

默认在 `%APPDATA%\CS2AIDirector\`，可在“专业导播”页点“打开数据目录”。

### 六项专业指标

| 指标 | 口径 | 方向 |
|---|---|---|
| `kill_coverage` | 击杀前 `metric_kill_lookback`（默认 2.5s）内，POV 是否在击杀者 | 越高越好 |
| `pre_fight_coverage` | 每段交火“首次伤害”前 2.5s 内，POV 是否在当事人（攻击者或受害者） | 越高越好 |
| `corpse_time` | POV 停在已阵亡选手身上的总秒数；另有 `corpse_ratio` | 越低越好 |
| `cut_rate` | 每 live 分钟的镜头切换次数（镜头数 − 1） | 有上限 |
| `shot_duration` | POV 镜头平均/中位停留秒数；另记全镜头均长 | 目标 2–5s |
| `manual_takeover` | 人工裸 0–9 接管次数；`manual_confirmed` 为 GSI 确认次数 | 越低越好 |

补充字段：`round_first_damage_coverage`、`shot_count`、`pov_shots`、`kills`、`first_damages`、`live_seconds`。
所有指标都基于**实际 GSI `observer_slot`** 与导演意图的确认结果，不是“按键次数”。

### 回合故事状态

`StoryTracker` 从 GSI 推断：

- 阶段：`freezetime / opening / control / engage / post_plant / clutch / round_over / warmup / paused`；
- 经济：`full / force / eco`（live 时按已买到的武器+护甲；freezetime 按金钱，尽量在买入前采样）；
- 残局：`1v1 / 1v2 / 1v3…` 与残局方；
- 下包：A/B、倒计时、拆包中；
- 比分、分差、赛点（MR12，先到 12 分）。

故事状态会显示在“运行状态”和“专业导播”页，并在每次阶段/经济/下包/残局/比分变化时写日志。
它目前**不参与** `score_candidates` / `choose_slot`，也不影响模型；这是 Phase 2 镜头调度器的输入。

> 已知边界：GSI 不直接告诉程序 HLAE 当前是哪个机位，所以 `SPAWN/BOMB` 镜头按“已发送运镜 + 预计归还时间”记录；
> `OVERVIEW` 仅在 timeline 类型中预留，当前不会产生；指标是启发式统计，需用真实比赛录像校准。

## 5. 关键参数（config.json）

| 键 | 默认 | 说明 |
|---|---|---|
| `gsi_port` | 3000 | GSI 监听端口，必须与 cfg 里 `uri` 一致 |
| `hlae_spawn_ct_key` / `hlae_spawn_t_key` | 空 | CT/T 出生点快捷键，格式 `alt+n`；GUI 用下拉选择 |
| `hlae_bomb_a_key` / `hlae_bomb_b_key` | 空 | A/B 包点快捷键，格式 `alt+1`；GUI 用下拉选择 |
| `hlae_spawn_ct_mode` / `hlae_spawn_t_mode` / `hlae_bomb_a_mode` / `hlae_bomb_b_mode` | double | 触发方式：`single` 单次 / `double` 连按两次 |
| `spawn_cam_default_side` | CT | 第一回合/无上回合记录时用哪边出生点 |
| `bomb_site_refs` | 内置 7 图 | A/B 包点参考坐标；新地图可在此补充 |
| `hotkey_double_interval` | 0.08 | 两次主键**按下**的间隔（秒） |
| `hotkey_press_hold` | 0.03 | 主键按住时长（秒） |
| `hotkey_modifier_lead` | 0.08 | 按住 Alt 后等多久再按主键 |
| `hotkey_modifier_release_delay` | 0.05 | 兼容项：第二次主键后等多久松开 Alt |
| `hotkey_modifier_hold_after` | 0.30 | v1.10.4：最后按键后保持 Alt 的时长；MulNX 需 >200ms 才消费连击，取与 release_delay 较大值 |
| `manual_pause_seconds` | 10.0 | 手动按裸 0–9 后 AI 暂停秒数 |
| `switch_min_score` | 0.30 | 切换阈值，越低越容易切 |
| `switch_margin` | 0.10 | 新目标需领先当前目标的分数 |
| `min_dwell_seconds` | 1.0 | 两次切换最小间隔（基础值） |
| `min_shot_duration` | 2.0 | v1.10.3 最短镜头停留；与 min_dwell 取较大值 |
| `max_cuts_per_10s` | 3 | v1.10.3 10 秒内最多切换次数；0 关闭 |
| `fight_lock_enabled` | true | v1.10.3 当前 POV 交火/拆包时不切走 |
| `fight_lock_aim` | 0.60 | 双方瞄准度都过该值也视为交火 |
| `hard_min_switch_interval` | 0.2 | 任意两次切换的硬下限（200ms） |
| `repeat_guard_seconds` | 4.0 | 防止 GSI 延迟导致重复按同一键 |
| `max_vertical_diff` | 300.0 | 楼层高度差超过此值视为不可能交火（Nuke 自动放宽） |
| `bomb_cam_countdown` | 8.0 | 炸弹剩余多少秒触发包点运镜 |
| `bomb_cam_policy` | pov_first | `pov_first`：场上仍有可交火目标时跳过包点运镜；`always`：总是触发 |
| `bomb_cam_grace` | 1.5 | 包点运镜后 AI 让位秒数，设 0 可让 AI 立刻接管 |
| `ai_enabled` / `deepseek_api_key` | 开 / 空 | AI 仲裁开关与 Key（明文保存） |
| `ai_min_interval` / `ai_max_per_minute` | 3.0 / 12 | 成本与频率限制 |
| `demo_model_enabled` | true | 是否加载静态 demo 权重；不做在线更新 |
| `demo_model_min_samples` | 20 | 少于该样本数时不使用 demo 权重 |
| `demo_model_blend` | 0.25 | demo 模型对规则分的最大加权比例 |
| `decision_log_enabled` | true | 是否记录 decisions.csv |
| `decision_log_interval` | 1.0 | 保持决策的采样间隔（秒） |
| `decision_label_window` | 3.0 | 切换后多久回填标签 |
| `decision_model_enabled` | true | 是否加载随机森林仲裁模型 |
| `decision_model_margin` | 0.08 | 规则前两名分差 ≤ 该值才让模型仲裁 |
| `decision_model_min_factor` | 0.9 | 模型推荐目标的规则分下限系数 |
| `decision_model_prob_margin` | 0.05 | 模型推荐目标需领先当前目标的概率；过低不切 |
| `decision_model_min_prob` | 0.55 | 模型推荐目标的最低概率 |
| `shot_timeline_enabled` | true | 是否记录镜头时间线/指标 |
| `shot_timeline_dir` | 空 | 时间线输出目录，空则 `%APPDATA%\CS2AIDirector` |
| `metric_kill_lookback` | 2.5 | 击杀覆盖回看窗口（秒） |
| `metric_prefight_lookback` | 2.5 | 交火前覆盖回看窗口（秒） |
| `metric_engagement_gap` | 3.0 | 多久没有伤害算新的一段交火 |
| `metric_min_live_seconds` | 5.0 | live 时间少于此值不计算 cut_rate |
| `story_opening_seconds` | 12.0 | 开局阶段时长 |
| `story_engage_recent_seconds` | 3.0 | 最近伤害多久内算“交火中” |
| `economy_full_avg` / `economy_force_avg` | 3500 / 2000 | freezetime 金钱兜底分档 |
| `economy_equip_full` / `economy_equip_force` | 1.5 / 0.8 | live 装备平均档位分档 |
| `match_point_score` | 12 | MR12 赛点分数 |
| `obs_host` / `obs_port` / `obs_password` | 127.0.0.1 / 4455 / 空 | OBS WebSocket 连接（密码明文保存） |

| `event_actions_enabled` | true | 事件动作总开关 |
| `event_rules` | [] | 事件动作规则数组（GUI 编辑） |
| `auto_focus` | true | 按键前自动把 CS2 拉到前台 |
| `dry_run` | false | 演练模式 |

其余参数见 `DEFAULT_CONFIG`（文件顶部）。

---

## 6. 故障排查

**GSI 一直“等待数据”或间歇“缺少 allplayers_position”**
- 打开 `gamestate_integration_ai_director.cfg`，第一行必须是 `"CS2 AI Director"`、第二行是 `{`（Valve KeyValues）；如果是 JSON 的 `{ "uri": ... }`，CS2 会静默忽略 → 必须用 v1.5 重新“一键安装 GSI”，再完全重启 CS2；
- 先看 `...\game\csgo\cfg\` 里是否还有别的 `gamestate_integration_*.cfg` 指向同一端口（本机常见的是 `gamestate_integration_yuzi_cup.cfg` → `localhost:3000`，且没有 `allplayers_position`）；
- 让本程序用独立端口（如 32100）重新安装 GSI，或修改/停用对方配置的端口；**不要两个工具共用一个端口**；
- 程序已会过滤缺少 `provider` / `allplayers_position` 的包，并在启动/安装时提示端口冲突；
- 确认 cfg 在 `...\game\csgo\cfg\`、游戏已完全重启、端口一致；
- 用浏览器/命令行 `curl` 无法直接验证（GSI 只推送），看日志有没有 POST；
- 部分环境需要在 CS2 启动项加 `-gamestateintegration`（通过 MLAE 启动时加在 MLAE/CS2 的快捷方式上）；
- 只有观战/有 `allplayers` 数据时才能自动导播；防火墙需允许本机 127.0.0.1 回环。

**切视角没反应 / 停在尸体视角**
- v1.10.2 修复了 `observer_slot` 与数字键错位：旧版把 slot 直接当按键；v1.10.1 的映射方向写反，已由 v1.10.2 更正；请使用 v1.10.2 及以上；
- v1.6 起已移除“不是观战视角就跳过”的判定；观察对象阵亡会切到离尸体最近的敌人；
- 若仍停在尸体，确认 `observer_slot` 在死亡后仍有上报（否则无法判断哪名选手死亡）；
- 日志出现“窗口未聚焦/激活失败”：以管理员运行本程序，确认游戏窗口标题是 `Counter-Strike 2`；
- 游戏必须在前台；程序每次按键前都会检查前台进程是不是 `cs2.exe`；
- 确认游戏内 0–9 数字键确实绑定观察者切换；
- 演练模式不会真按键。

**手动接管没反应**
- 日志里“手动接管检测=hook”表示钩子正常；若为 `polling` 说明钩子被拦截，轮询仍可用；
- 某些杀毒软件会拦全局钩子，加入白名单即可。

**AI 一直失败**
- 检查 Key、余额、网络；失败会自动回退规则引擎，不会停摆；
- 不想用时清空 Key 或取消勾选。

**HLAE/MulNX 运镜不触发或触发两次**
- **MulNX 不是 HLAE**：它用 `GetAsyncKeyState` 每 10ms 轮询，并把按键后 **200ms** 的连击计数放进缓冲区；检查连击时还要求 Alt 仍按住。v1.10.4 起默认在最后按键后保持 Alt 300ms（`hotkey_modifier_hold_after=0.30`）；旧版 50ms 就松 Alt，所以会出现“日志已发送但不触发”；
- 导播的“单次/双击”必须与 MulNX 运镜包/宏 YAML 里的 `comboClick` 完全一致：`comboClick: 1` 用单次，`comboClick: 2` 用双击。只改导播 GUI 不会改 MulNX 的宏；
- v1.10.3 启动时会打印“权限=管理员/普通用户”；如果 CS2/HLAE 由 MULNX 等启动器以管理员运行，本程序也必须**右键以管理员身份运行**，否则 Windows UIPI 会拦截模拟按键：日志显示“已发送”，游戏里却没有反应；
- 先在设置里把该项改成“单次”试一次，再改回“双击”；双击是“按住 Alt 连点两次”，若 HLAE 只认单次 alt+N，双击可能先触发再取消；
- 日志只会写“已发送”，无法确认 HLAE 是否接收；若没反应，把 `hotkey_press_hold` 调到 0.06–0.08，或把 `hotkey_double_interval` 调到 0.15–0.25；
- 快捷键现在是下拉选择：**ALT/SHIFT/CTRL 三选一 + A–Z/0–9 之一**，不再支持 `f6`、`ctrl+shift+f9` 这类自由输入；旧配置会显示“请选择”，需要重新选；
- 指南的交互格式是 `alt+按键`：选择 ALT + 对应字母/数字；
- 相机需要**长按 Alt 同时短按两次其它键**：对应项选“双击”。程序已实现为“按住 Alt → 点主键 45ms → 间隔 120ms → 再点一次 → 松开 Alt”，不是两次完整 alt+1；
- 如果一按就触发两次/镜头被取消：改回“单次”；
- 确认 HLAE/运镜工具以管理员运行，且没有忽略模拟输入。

**A/B 包点判断不对或没触发**
- 内置了 Mirage / Inferno / Dust2 / Anubis / Ancient / Cache / Nuke 的包点参考点；
- 其他地图会在日志提示“没有 A/B 包点参考点”，可在 `config.json` 的 `bomb_site_refs` 里按
  `"地图名": {"A": [x, y], "B": [x, y]}` 补充（Nuke 用 3D，其他图用 x/y）；
- 日志会打印“判定为 A/B 包点”，若与实际相反，把该地图的 A/B 坐标对调；
- 炸弹位置来自 `bomb.position`，确认 GSI 的 `bomb` 字段已开启。

**端口被占用**
- 改端口 → 重新“一键安装 GSI” → 重启 CS2。

---

## 7. 需要你手动确认的事项

1. `cs2.exe` 路径是否正确（自动检测可能找到多个 Steam 库）。
2. GSI cfg 已安装并**完全重启** CS2；端口与 GUI 一致，且没有和其他 GSI 工具（如 `yuzi_cup`）共用 3000。
3. **只在观战/导播机使用**：v1.6 移除了“观战视角”保护；正常游玩时启动会自动按数字键切武器，后果自负。
4. 观察对象阵亡后应自动切到最近敌人；若没有，记录当时 `observer_slot`/`allplayers` 日志。
5. CT 出生点、T 出生点、A 包点、B 包点四个快捷键与 HLAE 内绑定**完全一致**（下拉选择 ALT/SHIFT/CTRL + A–Z/0–9）。
6. 四个宏的“单次/双击”与实际 HLAE 行为一致；带修饰键的数字/字母不会暂停 AI，裸数字才会。
7. 出生点：确认上一回合胜方与触发方向一致（CT 胜→CT 出生点，T 胜→T 出生点）；第一回合用 `spawn_cam_default_side`。
8. 进游戏分别安放 A/B 包，确认触发的是对应包点的相机；若 A/B 反了，检查 `bomb_site_refs`。
9. 有可交火目标时包点运镜会被跳过（`bomb_cam_policy=pov_first`）；远距离/跨楼层无交火时会触发包点运镜。想总是触发就改成 `always`。
10. 游戏内 0–9 数字键的观察者切换可用；建议先用 demo/离线 bot 局试。
11. 程序与游戏的前台/管理员权限关系（游戏若以管理员启动，本程序也要管理员）。
12. 是否接受 DeepSeek API 费用；Key 以明文保存在本机 JSON。
13. `bomb_cam_grace=1.5` 是否合适：包点运镜触发后 AI 会让位 1.5 秒；想 AI 绝对优先就设为 0。
14. 自动化输入是否符合你所在平台/比赛规则（本程序不碰内存与进程，但仍是模拟按键）。
15. 即时学习已移除；静态 demo 模型需要 `demoparser2`，训练后重启导播加载 `learner.json`。
16. 决策模型需要至少 1000 条已标注 CSV；训练用 `scikit-learn`，生成 `decision_model.pkl`，重启导播加载。
17. 专业导播只剩 OBS WebSocket：先在“专业导播”页连接，再用事件动作播放/停止媒体源或切换场景；FFmpeg 回放、录制/击杀点动作和玩家改名已在 v1.10.5 移除。
19. 若显示器缩放不是 100%，检查窗口是否完整显示（GUI 采用分页以适配较小高度）。

---

## 8. 文件

- `cs2_ai_director.py`：完整程序（单文件）
- `test_cs2_ai_director.py`：核心逻辑测试，共 99 项（不启动 GUI、不发真实按键）
- `director_pro.py`：独立日志、事件动作、OBS WebSocket 场景/媒体控制
- `director_story.py`：镜头时间线、六项专业指标、回合故事状态（Phase 1）
- `decision_model.py`：决策 CSV、3 秒标签回填、随机森林模型加载/仲裁
- `train_from_demos.py`：从 `.dem` 离线训练静态 `learner.json`（主程序旧 demo 模型）
- `build_exe.ps1`：主程序 exe 的打包脚本
- `SELFCHECK.md`：自检清单、修复记录与仍需实机验证的步骤
- `frozen_v1.0.0/`：优化前冻结基线（含 SHA256）
- `requirements.txt`：依赖
- `start_cs2_director.bat`：无控制台启动脚本
