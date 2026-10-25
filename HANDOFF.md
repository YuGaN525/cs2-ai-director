# CS2 AI 导播 — 开发交接日志

最后更新：2026-10-24  
当前主程序源码版本：**v1.10.18（Yuzi_CS2_AIDirector；开局道具镜头）**  
当前训练器版本：**v1.2.0（Yuzi_CS2_AIDecisionTrainer；AI 辅助训练）**  
专业导播新目标：见 `PRO_DIRECTOR_HANDOFF.md`

---

## 1. 项目定位

CS2 观战/转播导播工具，核心目标：

1. 只读取 CS2 GSI 数据，自动把观察者 POV 切到“未来 1–3 秒最可能造成击杀”的选手；
2. 回合开始触发 HLAE 出生点运镜（按上一回合胜方选 CT/T）；
3. 炸弹倒计时触发 A/B 包点运镜；
4. 观察对象阵亡后自动切到离尸体最近的敌人；
5. 专业导播：独立日志、事件动作、OBS WebSocket 场景/媒体控制；
6. 决策日志 + 离线随机森林仲裁模型；
7. 独立训练器支持 `decisions.csv` 和 `.dem`；
8. v1.10 Phase 1：镜头时间线、六项专业指标、回合故事状态（只观测，不参与评分）。

硬边界：

- 只读 GSI HTTP 推送；
- 不读游戏内存、不注入、不发送游戏控制台命令；
- 对游戏的动作只有模拟 0–9 观察者数字键，以及用户录入的 HLAE 宏快捷键；
- 不直接与 HLAE 通信。

---

## 2. 仓库与产物

### 导播主仓库（私密）

https://github.com/YuGaN525/cs2-ai-director

- 当前源码版本：`v1.10.18`（开局道具镜头；已打包发布）
- Phase 1 源码提交：`b78bbd3 Add Phase 1 shot timeline, pro metrics, and round story`
- v1.10.1 映射方向错误（已废弃）：`02c770e`
- v1.10.2 映射修正提交：`44640a3 Correct observer_slot keyboard mapping direction`
- v1.10.3 稳定性提交：`f2563e1 Reduce fragmented cuts and add fight lock`
- v1.10.4 MulNX 时序提交：`7f6caf6 Hold Alt for MulNX combo buffer`
- v1.10.5 功能精简提交：`0156527 Remove FFmpeg replay and player rename`
- v1.10.6 品牌化提交：`ebcb74c Brand as Yuzi_CS2_AIDirector`
- v1.10.7 训练/仲裁优化：`a49ddc3 Align trainer with director arbitration`
- v1.10.8 OBS/UI 修复：`e8b9199 Fix checkbox checkmark and OBS event sync`
- v1.10.9 GSI 阶段修复：`8ecf010 Fix GSI gameover phase priority`
- v1.10.10 暂停兜底：`a948704 Add manual pause fallback`
- v1.10.11 回合边界修复：`ee05a1d Fix round boundary during over`
- v1.10.12 在线校准：`022772c Add online probability calibration`
- v1.10.13 校准窗口修复：`4ff7177 Calibrate only on post-model samples`
- v1.10.14 回合叙事：`f8a3860 Add round narrative hint`
- v1.10.15 性能优化：`ee4c5b5 Optimize runtime hot paths`
- v1.10.16 模型/配置备份：`80f86ba Add model/config bundle export/import`
- v1.10.17 会话/阈值：`87c3ec3 Fix session metrics and calibration threshold`
- v1.10.18 开局道具镜头：`8631949 Add utility camera for opening and control phase`
- Release：`v1.10.18`
- 资产：`Yuzi_CS2_AIDirector.exe`
- 大小：36,527,430 字节（约 36.5 MB）
- SHA256：`22D1D7CA6412A1ED835DDEA2C4B906C52251744527D7CB3CC0E71A9D9261754E`
- Release 页面：https://github.com/YuGaN525/cs2-ai-director/releases/tag/v1.10.18

> v1.10.18 已用 PyInstaller 6.22.3 / Python 3.14 打包，并通过启动验证（窗口标题 `Yuzi_CS2_AIDirector v1.10.18 · CS2 AI 专业导播`）；
> exe 已作为 v1.10.18 Release 资产上传。v1.10.1 映射方向错误，请勿使用。

### 训练器仓库（私密）

https://github.com/YuGaN525/cs2-ai-decision-trainer

- 最新提交：`5dde4dd Clarify AI search iteration budget`（v1.2.0 功能提交 `2f5f1f5`）
- 共享核心：已同步 v1.10.18（`cs2_ai_director.py` / `director_pro.py` / 测试）
- Release：`v1.2.0`
- 资产：`Yuzi_CS2_AIDecisionTrainer.exe`
- 大小：183,799,476 字节（约 175 MB）
- SHA256：`67D39F87C56FF06D8EB9FE333308EE6FF8770126F69D55746AA035DE5D34BDE7`
- Release 页面：https://github.com/YuGaN525/cs2-ai-decision-trainer/releases/tag/v1.2.0

> v1.2.0 新增 DeepSeek 训练顾问 + 本地验证：只发送聚合统计；AI 候选必须超过基线且体积可控才采用；
> 无 Key/失败自动回退本地扩展搜索。训练器 exe 已通过启动与 `--cli --ai` 完整链路验证。

### 本地产物

```text
dist\Yuzi_CS2_AIDirector.exe
dist\Yuzi_CS2_AIDecisionTrainer.exe
```

训练器和主程序建议放在同一目录，主程序的“打开训练器…”按钮按同目录 exe 查找。

---

## 3. 主仓库文件

```text
cs2_ai_director.py        # 单文件主程序（GUI/GSI/评分/切换/HLAE）
director_story.py         # 镜头时间线/六项专业指标/回合故事状态（v1.10）
director_pro.py           # 独立日志/事件动作/OBS 场景/媒体控制
decision_model.py         # 决策 CSV、3 秒标签、纯 JSON 森林加载/仲裁
train_from_demos.py       # 旧静态 demo 模型（learner.json）
test_cs2_ai_director.py   # 129 项测试
requirements.txt          # 主程序依赖（含 obsws-python）
requirements-train.txt    # demo 静态模型/训练依赖
build_exe.ps1             # 主程序 PyInstaller 打包
tools/lookahead/          # 早源 GOTV 影子录制/评估（外置观察器，不进 exe）
start_cs2_director.bat
README.md
SELFCHECK.md
frozen_v1.0.0/            # 旧版基线
```

主仓库已移除训练器 GUI/CLI/demo CSV 相关文件，避免两套源码重复；训练器仓库保留共享核心副本。

## 4. 训练器仓库文件

```text
trainer_gui.py            # 独立训练器 GUI + --cli
train_decision_model.py   # RF/ExtraTrees 训练、CV、JSON 导出
ai_trainer.py             # AI 辅助训练：数据画像、DeepSeek 顾问、基线/候选验证与报告
test_ai_trainer.py        # AI 辅助训练测试（11 项）
demo_dataset.py           # .dem -> demo_decisions.csv
decision_model.py         # 共享模型/CSV 定义
train_from_demos.py
cs2_ai_director.py        # 共享评分/快照核心（demo 解析需要）
director_story.py         # v1.10 同步的镜头/指标/故事观测模块（训练不使用）
director_pro.py           # 主程序依赖
test_cs2_ai_director.py
requirements.txt
requirements-train.txt
build_trainer.ps1
README.md
```

> 注意：训练器为了复用 demo 评分代码，内含主程序核心副本；修改评分特征时必须同步两个仓库，否则训练/推理特征漂移。

---

## 5. 当前功能清单

### 5.1 AI POV 切换

- GSI `allplayers` + `allplayers_position`；
- 评分：武器射程包络、双向瞄准、胜率、血量/护甲、残血、最近掉血、拆包、准备度；
- 决策：`switch_min_score=0.30`、`switch_margin=0.10`、`min_dwell=1.0s`、`repeat_guard=4.0s`、`emergency=0.72`；
- 硬间隔：`hard_min_switch_interval=0.2s`；
- **v1.10.2 映射修正**：CS2 实测“按数字键 N → GSI slot (N-1) mod 10”，因此发键用
  `slot_to_key(slot)=(slot+1) mod 10`（slot 0→1、1→2、3→4、4→5、9→0）；人工接管反向用
  `key_to_slot(key)=(key-1) mod 10`（键1→slot0、键0→slot9）。日志/状态同时显示 `slot=N（键X）`；
- **v1.10.3 防碎切**：`min_shot_duration=2.0s`、`max_cuts_per_10s=3`；
- **v1.10.3 交火锁定**：当前 POV 在射程内与敌人交火（近期掉血/双向瞄准）或正在拆包时保持不切；
  当前观察者阵亡的死亡回切不受此限制；
- 观察对象阵亡回切：优先最近敌人，支持 `observer_slot` 被清空后使用最后观察记录；
- 不再使用“是否观战视角”阻断切换（v1.6 移除）；
- 跨楼层/超远距离不视为可交火：`max_vertical_diff=300`，Nuke 放宽到 900；
- 包点 POV 优先：有可交火目标才跳过包点运镜。

### 5.2 HLAE 运镜

- 四个快捷键：CT 出生点、T 出生点、A 包点、B 包点；
- GUI 只允许下拉选择 `ALT/SHIFT/CTRL + A–Z/0–9`；
- 双击时序：Alt 按下 → 80ms → 主键 30ms → 50ms → 主键 30ms → 50ms → Alt 松开；
- `hotkey_double_interval=0.08` 表示两次主键按下的间隔；
- 手动裸 0–9 暂停 AI 10 秒；
- 日志只写“已发送”，不谎称 HLAE 已接收；
- **v1.10.3 权限诊断**：启动时输出“权限=管理员/普通用户”；非管理员会警告
  “CS2/HLAE 若以管理员运行，UIPI 会拦截模拟按键，日志已发送但游戏无反应”；
- 若 HLAE 只认单次 alt+N，双击可能先触发再取消；日志提示改用“单次”验证；
- **v1.10.4 MulNX 适配**：MulNX 的 `InputSystem` 每 10ms 轮询 `GetAsyncKeyState`，按键后
  200ms 才把连击计数放入 `BufferComboClick`，并在消费时检查 Alt 仍按住。旧版最后按键后
  50ms 就松 Alt，所以日志“已发送”但 MulNX 不触发。现在 `hotkey_modifier_hold_after=0.30`，
  最后按键后保持 Alt 300ms；导播单次/双击必须与 MulNX 宏的 `comboClick` 一致。

### 5.3 专业导播

`director_pro.py`：

- 独立日志：`GSI/OBS/EventAction/App`，GUI 独立“导播日志”页，可过滤类别/级别/时间/关键字；
- 事件动作：13 类 GSI 事件，3 种动作（播放媒体/停止媒体/切换场景）；
- 规则支持多动作、启用/禁用、JSON 预设导入导出；
- OBS WebSocket：连接、场景创建/切换、媒体源播放/停止/重播；
- v1.10.5 起已移除 FFmpeg 回放、录制/击杀点/生成回放动作和玩家改名。

### 5.4 决策日志与模型

主程序每次切换（以及每 1 秒一次的保持）写：

```text
%APPDATA%\CS2AIDirector\decisions.csv
```

字段：

```text
timestamp, iso_time, map, round, action, target_slot, target_name,
target_steamid, current_slot, reason, rule_score, candidate_count,
distance, target_hp, enemy_hp,
aim_self, aim_enemy, win, enemy_low_hp, self_low_hp,
proximity, enemy_damaged, hp_advantage,
label, label_damage, label_kill
```

标签：

- 记录时保存目标 `round_totaldmg` / `round_kills` 基线；
- 3 秒后差值 >0 → label=1；
- 回合切换 → 丢弃待标注样本。

模型：

- 训练器在 **RandomForest** 与 **ExtraTrees** 之间做 3 折 CV + 8 组随机搜索；
- 指标接近时优先小模型：AUC 差 ≤0.005、top-1 差 ≤1pp 时比较节点数；
- 当前线上紧凑模型：RandomForest，pkl 9.2MB / JSON 2.05MB，AUC 0.7074，top-1 59.1%；
- 重型 ExtraTrees 备份已保留，JSON 36.3MB，AUC 0.7114，top-1 59.9%；
- 导出：

```text
decision_model.pkl
decision_model.json.gz
decision_model_metrics.json
```

- 主程序只加载 `decision_model.json.gz`，不依赖 sklearn；
- 每 5 秒检查 mtime 自动重载；
- 只在规则前两名分差 ≤ `decision_model_margin=0.08` 时仲裁；
- 模型推荐目标的规则分不得低于第一名 × `decision_model_min_factor=0.9`；
- **v1.10.7 概率边际**：模型目标概率需 ≥ `decision_model_min_prob=0.55`，且比当前目标高
  `decision_model_prob_margin=0.05`，避免 50.1% vs 49.9% 的噪声切换；
- 仍走阈值/迟滞/硬间隔。

### 5.5 训练器

- GUI：CSV 或 `.dem` 文件/文件夹；
- `.dem`：击杀前 160 tick（64tick≈2.5s）；v1.10.7 起标签为“回看窗口内造成过伤害/击杀”的候选=1，
  与在线 `decisions.csv` 的 `label` 对齐，而不是只标击杀者；
- 训练按 `group`（回合或 demo 击杀事件）切分，避免同回合泄漏；
- `is_ambiguous=1` 的规则前两名接近样本权重更高；`switch` 样本略加权；
- 评估同时输出 AUC 和 top-1（候选组内概率最高者是否命中）；
- 输出 `demo_decisions.csv` 后训练；
- CLI：

```bat
Yuzi_CS2_AIDecisionTrainer.exe --cli --csv decisions.csv --min-samples 1000
Yuzi_CS2_AIDecisionTrainer.exe --cli --demos "D:\demos" --min-samples 1000
```

- **数据来源不要混淆**：`.dem` 是训练器自己构造候选，不等于导播当时的实际选择；
  要学导播实际行为，用运行时写出的 `decisions.csv`；
- 2026-10-22 用五张导播实际对局的 `decisions.csv` 重训：
  7,131 行 / 86 group / 647 正例 / 2,707 模糊样本；
  RandomForest CV AUC 0.9169、验证 AUC 0.8054；每行只有一个最终目标，top-1 不可用；
- 内置 scikit-learn、joblib、demoparser2、pandas；
- PyInstaller onefile 下 `n_jobs=1`，避免 joblib 多进程卡死。

### 5.6 旧静态 demo 模型

- 主程序“从 demo 训练…”仍然调用 `train_from_demos.py`，训练 `learner.json`；
- 这是旧的静态逻辑回归权重，**不再在线更新**；
- `DemoModel` 只加载权重，按 ±12.5% 调整候选分；
- 与新的随机森林仲裁可能同时生效，这是已知交互点。

---

### 5.7 专业导播 Phase 1（v1.10）

文件：`director_story.py`

- `ShotTimeline`：按实际 `observer_slot` + 导演意图记录每个镜头
  （类型/目标/开始/结束/原因/来源/人工接管/故事快照）；
- 镜头类型：`POV / BOMB / SPAWN / OVERVIEW`；当前实际使用
  `POV`、`BOMB`、`SPAWN`，`OVERVIEW` 已预留；
- 六项指标：
  - `kill_coverage`：击杀前 2.5s 镜头在击杀者；
  - `pre_fight_coverage`：每段交火首次伤害前 2.5s 镜头在当事人；
  - `corpse_time`：镜头停在尸体上的秒数；
  - `cut_rate`：每 live 分钟切换次数；
  - `shot_duration`：POV 平均/中位停留；
  - `manual_takeover`：人工裸 0–9 接管次数（另记 GSI 确认次数）；
- `StoryTracker`：阶段/经济/残局/下包/比分/赛点；
- 输出：
  - `shot_timeline.csv`
  - `shot_events.csv`
  - `round_metrics.csv`
  - `director_metrics.json`
- GUI：“运行状态”和“专业导播”页显示故事/指标；可打开数据目录；
- **不参与** `score_candidates` / `choose_slot` / 模型仲裁；评分特征不变。

### 5.8 开局控图道具镜头（v1.10.18）

- 目标：`opening`/`control` 阶段，无交火时优先把 POV 切给“手持道具”的选手，
  并在投掷前后保留约 250–500ms 缓冲；
- 手持检测：`Player.active_weapon` 是允许的手雷类型，约 100ms 确认后预切；
- 投掷检测：GSI 无投掷事件，用连续快照的 `allplayers_weapons` 手雷条目数/`ammo_reserve` 减少推断；
- 缓冲：`utility_cam_pre_buffer` / `utility_cam_post_hold` 默认 0.35s，运行时夹在 0.25–0.5s；
  场上近 2.5s 有伤害或高威胁候选时缩短到 0.25s，安静时放宽到 0.5s；
- 默认类型：烟雾弹、闪光弹、燃烧弹、高爆手雷；默认不含诱饵弹（`utility_cam_types` 可改）；
- 安全约束：不绕过交火锁定、最短停留（默认 2s）、`min_dwell_seconds`（默认 1s）、10 秒切率、硬间隔、重复按键保护；
  另有 `utility_cam_min_interval`（默认 3s）和 `utility_cam_max_per_round`（默认 4）限制；
- 独立来源 `utility` 写入镜头时间线；不写入 `decisions.csv`（避免污染行为克隆样本）；
- 已知风险：丢道具给队友也会被识别为“投掷”；同一次手持只触发一次，需实机验证。

### 5.9 早源 GOTV Lookahead（影子工具，未进 exe）

- 背景：赛事方可提供早于节目画面的 GOTV。它能把击杀覆盖从“预测”变成“预知”，
  但事件到达时节目画面还在 N 秒前，必须按节目时间排程，不能到达即切。
- `tools/lookahead/observer_bridge.py`：托管启动第三方 `cs2-headless-observer`，
  记录 JSONL（含本地接收时间）、stderr 与会话报告；密码只走环境变量/交互 stdin，不落盘。
- `tools/lookahead/evaluate_lookahead.py`：把早源 death 与 `shot_events.csv` /
  `shot_timeline.csv` / `decisions.csv` 对齐，输出真实领先（中位/p10/p90/正领先占比）
  与“杀手不在拍、可预切”的机会明细。
- 当前只做影子记录/评估，**不改变导播镜头**；4 项合成测试通过。
- 进入 live 的门槛：领先稳定、正领先 >90%、可预切机会足够、配对完整、赛事规则允许、
  GSI 回退可用；live 需按 `recv + lead - 1~2s` 排程，并写入 `source=lookahead`。
- 合规：观察器 AGPL-3.0，外置不打包；账号/ToS 风险需赛事方确认；禁止把未来事件
  显示到直播/HUD。运行手册见 `tools/lookahead/README.md`。

## 6. 当前配置与路径

配置：

```text
%APPDATA%\CS2AIDirector\config.json
```

日志：

```text
%APPDATA%\CS2AIDirector\director.log
```

模型/样本：

```text
%APPDATA%\CS2AIDirector\decisions.csv
%APPDATA%\CS2AIDirector\learner.json
%APPDATA%\CS2AIDirector\decision_model.json.gz
%APPDATA%\CS2AIDirector\decision_model_metrics.json
%APPDATA%\CS2AIDirector\shot_timeline.csv
%APPDATA%\CS2AIDirector\shot_events.csv
%APPDATA%\CS2AIDirector\round_metrics.csv
%APPDATA%\CS2AIDirector\director_metrics.json
```

本机当前状态（交接时）：

- CS2：`D:\SteamLibrary\steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe`
- GSI 端口：`3000`
- 已安装的 GSI 配置是 KeyValues 格式；端口冲突已消除（旧 yuzi 配置已不在）；
- 快捷键：CT `alt+N`，T `alt+M`，A `alt+1`，B `alt+6`；
- DeepSeek API Key 已配置（不要在文档/截图暴露）；
- `decisions.csv` 目前基本只有表头，尚未积累 1000 条；
- 尚未训练出正式 `decision_model.json.gz`。

关键配置项：

```text
gsi_port
switch_min_score / switch_margin / min_dwell_seconds
min_shot_duration / max_cuts_per_10s / fight_lock_enabled / fight_lock_aim
hard_min_switch_interval / repeat_guard_seconds / emergency_score
bomb_cam_policy / bomb_cam_grace
hotkey_double_interval / hotkey_press_hold
hotkey_modifier_lead / hotkey_modifier_release_delay / hotkey_modifier_hold_after
max_vertical_diff
demo_model_enabled / demo_model_min_samples / demo_model_blend
decision_log_enabled / decision_log_interval / decision_label_window
decision_model_enabled / decision_model_margin / decision_model_min_factor
decision_model_prob_margin / decision_model_min_prob
shot_timeline_enabled / shot_timeline_dir
metric_kill_lookback / metric_prefight_lookback / metric_engagement_gap / metric_min_live_seconds
story_opening_seconds / story_engage_recent_seconds
economy_full_avg / economy_force_avg / economy_equip_full / economy_equip_force
match_point_score
obs_host / obs_port / obs_password / obs_scene...
event_actions_enabled / event_rules
```

---

## 7. 版本里程碑

- v1.0：GSI + 规则评分 + HLAE 出生点/包点 + GUI；
- v1.1：A/B 包点分离、单/双击；
- v1.3：阵亡回切、观察者保护、去抖；
- v1.4：CT/T 出生点分离；
- v1.5：GSI cfg 改 Valve KeyValues（JSON 会被 CS2 忽略）；
- v1.6：移除观战判定、精确 80/30/50/30/50ms、楼层/远距离无交火、200ms 硬间隔；
- v1.7：在线学习 + demo 静态模型（在线部分后来移除）；
- v1.8：整合专业导播（独立日志/事件动作/OBS/FFmpeg/改名）；
- v1.9：移除即时学习、决策 CSV/标签、随机森林、纯 JSON 加载、独立训练器、`.dem` 导入、RF/ExtraTrees CV 优化；
- v1.9.1：训练器独立私密仓库、主程序清理训练器源码；
- v1.10（Phase 1）：新增 `director_story.py`：镜头时间线、六项专业指标、回合故事状态；评分/模型/特征不变；
- v1.10.1：尝试修复 slot/按键错位，但方向写反（键 N→slot N+1），已废弃；
- v1.10.2：更正为键 N→slot (N-1) mod 10（键1→slot0、键0→slot9），日志/GUI 同时显示 slot 与键；
- v1.10.3：防碎切（最短停留 2s、10 秒最多 3 次切换）、当前 POV 交火/拆包锁定、HLAE 管理员权限诊断；
- v1.10.4：适配 MulNX 的 200ms 连击缓冲，Alt 在最后按键后保持 300ms，修复“已发送不触发”；
- v1.10.5：移除 FFmpeg 回放、录制/击杀点/生成回放动作和玩家改名；保留 OBS WebSocket 与事件动作；
- v1.10.6：品牌化为 Yuzi_CS2_AIDirector / Yuzi_CS2_AIDecisionTrainer，鱼籽杯图标、深色页头、卡片式 UI；
- v1.10.7：训练器分组切分、模糊样本加权、top-1 评估、demo 伤害窗口标签；仲裁增加概率边际；
- v1.10.8：勾选框改为原生 √；修复事件规则/OBS 配置同步；新增测试规则与 OBS 源/场景校验；
- v1.10.9：修复 map.phase=gameover 被 countdowns.phase=over 覆盖；增加 GSI 阶段切换日志；
- v1.10.10：GSI 无暂停状态，新增暂停热键（默认 ctrl+alt+p）与开始/结束按钮，手动派发暂停事件；
- v1.10.11：修复 over 阶段提前改 round 导致下一回合被立即结算、live=0 空行；新增回归测试；
- v1.10.12：新增 Platt 在线概率校准；训练器增加校准按钮、top-1/模糊样本诊断；
- v1.10.13：校准只使用模型训练后的线上样本；用 5 张 demo 重训（2955 行，top1 67.3%）；
- v1.10.14：新增回合叙事提示（freeze/回合结束），可选推送 OBS 文本源，不自动切镜；
- v1.10.15：候选评分快照缓存、GUI 日志批量刷新、训练器校准批量推理/AUC O(n log n)；不改变决策结果；
- v1.10.16：新增模型/配置 ZIP 导出导入，导入前自动备份，密钥默认脱敏；
- v1.10.17：会话指标按启动重置；校准阈值按验证 F1 自动选择（当前 0.20）；
- v1.10.18：开局/控图阶段道具镜头：`active_weapon` 为允许手雷时约 100ms 确认后预切；`allplayers_weapons` 库存减少推断已投掷，投掷前后各留 250–500ms 缓冲（近 2.5s 有交火则缩到 0.25s，安静则放宽到 0.5s）；仅 `opening/control` 生效，默认排除诱饵弹。

---

## 8. 已验证 / 未验证

已验证：

```text
主仓库 129 项测试通过（v1.10.18；含道具镜头/备份/叙事/校准/回合边界测试）
早源 lookahead 影子链路 4 项测试通过；已用真实观察器包校验 manifest/entrypoint（未启动 GOTV）
训练器仓库 140 项测试通过（129 共享核心 + 11 AI 辅助）
Python 编译通过（cs2_ai_director.py / director_story.py）
核心 ruff F/E9 通过
模拟 GSI 序列验证镜头/指标/故事落盘
主程序 exe 启动成功（Yuzi_CS2_AIDirector v1.10.18，窗口标题已核对）；训练器 exe v1.2.0 已发布并通过 `--cli --ai` 验证
AI 辅助训练真实数据验证：11,170 行；AI 候选 AUC 0.7722 < 基线 0.7763，守卫保留基线
训练器 CSV 训练 exit=0
训练器 .dem 训练 exit=0（544 行 / 105 击杀事件）
导播加载 ExtraTrees JSON 成功
模型文件覆盖后自动重载成功
```

未验证：

- 真实 OBS WebSocket 连接与场景/媒体源；
- 正式比赛/多 demo 模型效果；
- 长时间运行稳定性；
- 非 100% 显示器缩放下的最终布局；
- exe 数字签名/杀软误报；
- v1.10 时间线在真实 GSI/HL AE 运镜下的准确性（目前只有模拟 GSI 序列测试）；
- `pre_fight_coverage` 与真实导播人工标注的一致性；
- `shot_duration` / `corpse_time` 在真实 HLAE 包点/出生点镜头下的口径。

---

## 9. 已知问题/技术债

1. **OBS 未实机联调**：本机 OBS 未开 WebSocket；FFmpeg 回放已移除。
2. **模型样本少**：单 demo 约 540 行，AUC 约 0.61，不能代表真实效果。
3. **共享核心副本**：训练器仓库包含 `cs2_ai_director.py` 等副本，改评分必须同步。
4. **双模型叠加**：`DemoModel` 与随机森林都会调整排序，可能互相影响。
5. **决策样本正例统计**：重启后 `DecisionLogger` 只从 CSV 行数恢复 `rows`，`positive` 初始为 0，状态栏正例数会偏低。
6. **纯 JSON 只支持树森林**：不支持 XGBoost/LightGBM 原生格式。
7. **无回合上下文特征**：当前 8 个候选特征没有 alive 数、炸弹、时间、比分。
8. **demo 标签不是逐帧模拟导播**：是击杀前候选的 killer 标签，存在分布偏差。
9. **训练器体积大**：约 179 MB，内置 sklearn/pandas/demoparser2。
10. **未签名**：SmartScreen 提示；建议管理员运行主程序。
11. **OBS/DeepSeek 密码/Key 明文**保存在 config.json。
12. **EventAction 规则无内置预设库**：只有 JSON 导入导出。
13. **ProLog 只在内存**，没有独立日志文件。
14. **主程序未打包 sklearn**，旧 `.pkl` 模型在 exe 内不能加载，必须用 `.json.gz`。
15. **ruff 完整规则**仍有大量风格/宽泛异常提示，仅保证 F/E9 核心检查。
16. **主程序 `_open_trainer`** 要求 `Yuzi_CS2_AIDecisionTrainer.exe` 与主程序同目录；否则手动运行。
17. **v1.10 时间线是启发式观测**：GSI 不提供 HLAE 当前机位，`SPAWN/BOMB` 按“已发送 + 预计归还”记录；`OVERVIEW` 仅预留，尚未接入实际机位。
18. **经济分档是近似**：freezetime 用金钱（可能买到一半）、live 用武器+护甲平均档位；不读装备价值。
19. **残局/阶段判定依赖 GSI 字段**：若 `allplayers` 缺失、`observer_slot` 延迟，镜头目标会以 GSI 实际值为准，指标可能少算。
20. **回合切换/中途停止会产生 partial 行**：`round_metrics.csv` 用 `completed=1/0` 区分；累计指标会包含 partial 行。
21. **训练器仓库已同步 v1.10.17 共享核心**；训练器 exe 已重建为 v1.1.7；5 图模型 top1 67.3%。
22. **observer_slot 映射依赖实测**：v1.10.2 按完整表修正为“键 N→slot (N-1) mod 10”；v1.10.3/v1.10.4 未改映射；若 CS2 更新后映射变化，需重新实测。
23. **道具镜头依赖 GSI 武器字段**：手持依赖 `active_weapon`，投掷依赖手雷库存减少；若 `allplayers_weapons` 不逐项列出多颗同类手雷、或包内缺少武器字段，可能漏检；丢给队友会被当成投掷。全局 `min_shot_duration` 仍优先，实际镜头可能长于 0.5s 缓冲。需实机确认日志与实际投掷一致。

---

## 10. 构建命令

主程序：

```bat
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

训练器（训练器仓库）：

```bat
powershell -ExecutionPolicy Bypass -File .\build_trainer.ps1
```

测试：

```bat
python -m unittest test_cs2_ai_director
```

模型训练：

```bat
python -m pip install -r requirements-train.txt
python train_decision_model.py --min-samples 1000
```

`.dem`：

```bat
python demo_dataset.py --demos "D:\demos" --out demo_decisions.csv
```

---

## 10.1 共享核心同步清单（v1.10）

训练器仓库 `YuGaN525/cs2-ai-decision-trainer` 已同步 v1.10.17 共享核心，并发布 v1.1.7 F1 阈值/会话同步 exe。
评分核心没有改，但主程序新增了 `import director_story`，因此**复制新的 `cs2_ai_director.py` 时必须同时复制
`director_story.py`**，否则训练器的 demo 解析会 ImportError。

| 文件 | 主仓库状态 | 训练器仓库动作 |
|---|---|---|
| `cs2_ai_director.py` | v1.10 引擎集成 + 新增配置默认值 | 已复制；评分函数未变 |
| `director_story.py` | 新增 | 已复制 |
| `director_pro.py` | 未改 | 保持旧版 |
| `decision_model.py` | 未改 | 保持 |
| `test_cs2_ai_director.py` | 新增 6 项测试 | 已复制 |

不需要重新训练模型：`DECISION_FEATURES`、`score_candidates`、`choose_slot` 都没改。
训练器仓库已验证：

```text
python -m unittest test_cs2_ai_director
Ran 93 tests ... OK
ruff F/E9: All checks passed!
```

**指标不作为训练特征**：`kill_coverage` 等是回合/镜头级评估指标，存在训练/推理时点不一致与
“镜头影响指标、指标再影响镜头”的循环；当前 8 特征 `decision_model.json.gz` 格式保持不变。
后续做 Phase 2 镜头候选/偏好学习时，另建镜头级数据集与模型。

---

## 11. 推荐下一步

优先级从高到低：

1. **Phase 2 镜头调度器**：把 `StoryState` + `ShotTimeline` 接入 `ShotCandidate/ShotScheduler`，先做“不切”候选与最短停留，不改模型；
2. **实机验证 Phase 1**：用真实 GSI + HLAE 跑 1–2 场，校准 `corpse_time` / `shot_duration` / `pre_fight_coverage` 口径；
3. **实机联调 OBS**：连接 OBS WebSocket，验证场景切换与媒体源播放/停止；
5. **积累 1000+ 真实决策样本**，训练并观察 AUC 与线上误切；
6. **多 demo 训练**：至少 5–10 场完整 demo，按比赛留出验证，避免同场泄漏；
7. **实现回合上下文特征**：alive 数、炸弹、时间、比分、经济；
8. **模型校准/阈值**：可靠性曲线、按概率分桶评估；
9. **修复 DecisionLogger positive 恢复**；
10. **给训练器加模型对比 UI**（RF/ExtraTrees 的 CV/测试 AUC 表格）；
11. **exe 签名/安装包**；
12. **清理历史 SELFCHECK 段落**，保持文档与当前版本一致。

---

## 12. 新对话关键词

### 中文

```text
CS2 AI 导播
CS2 GSI
观察者 POV 自动切换
HLAE 出生点/包点运镜
事件动作
OBS WebSocket
独立导播日志
决策日志
3 秒标签
随机森林
ExtraTrees
交叉验证
随机搜索
纯 JSON 森林
模型自动重载
demoparser2
.dem 训练
独立训练器
PyInstaller onefile
镜头时间线
镜头类型
击杀覆盖
交火前覆盖
尸体时间
切换频率
镜头停留
人工接管
回合故事
经济局
强起
残局
赛点
```

### 英文

```text
CS2 GSI director
observer POV switching
HLAE camera macros
event actions
OBS WebSocket
separate director logs
decision logging
3-second outcome label
RandomForest
ExtraTrees
RandomizedSearchCV
pure JSON forest
model hot reload
demoparser2
demo training
standalone trainer
PyInstaller onefile
ShotTimeline
ShotType
kill coverage
pre-fight coverage
corpse time
cut rate
shot duration
manual takeover
round story
economy
force buy
clutch
match point
```

### 文件/仓库关键词

```text
cs2_ai_director.py
director_story.py
director_pro.py
decision_model.py
shot_timeline.csv
round_metrics.csv
train_from_demos.py
trainer_gui.py
train_decision_model.py
demo_dataset.py
Yuzi_CS2_AIDirector.exe
Yuzi_CS2_AIDecisionTrainer.exe
YuGaN525/cs2-ai-director
YuGaN525/cs2-ai-decision-trainer
```

---

## 13. 新对话开场提示（可直接复制）

```text
继续开发 CS2 AI 导播项目。请先阅读仓库根目录的 HANDOFF.md 和 README.md。

当前状态：
- 主程序源码 v1.10.18（Yuzi_CS2_AIDirector），训练器 v1.2.0；
- 主仓库 YuGaN525/cs2-ai-director，训练器仓库 YuGaN525/cs2-ai-decision-trainer，均为私密；
- 主程序 exe：dist/Yuzi_CS2_AIDirector.exe（v1.10.18，已重新打包）；
- 训练器 exe：dist/Yuzi_CS2_AIDecisionTrainer.exe（v1.2.0，含 AI 辅助训练）；
- 140 项测试通过；GSI 阶段/OBS 事件链路已修复；暂停用热键/按钮兜底；在线校准/回合叙事/模型配置备份/开局道具镜头已启用；
- 模型使用 RF/ExtraTrees 交叉验证选择，导出纯 JSON，导播自动重载；
- 训练器 v1.2.0 新增 AI 辅助训练：DeepSeek 聚合画像诊断 + 本地基线/候选验证，报告写入 ai_training_report.json；
- decisions.csv 约 11,170 行有效样本；训练器仓库已同步 v1.10.18 共享核心。

硬性约束：
- 只读 GSI，不读内存/不注入/不发控制台命令；
- 只模拟 0-9 观察者键和 HLAE 快捷键；
- 不使用深度学习；
- 训练器与主程序共享评分核心，修改时两个仓库要同步。

下一步优先（见 DEVELOPMENT_PLAN.md）：
1. 用 v1.10.18 实机验证道具镜头，检查 shot_timeline.csv 的 source=utility 与实际投掷是否一致；
2. P0-2 候选级日志 decision_candidates.csv，记录全部候选而非最终目标；
3. P0-3 击杀未命中四类归因；P0-1 建立新会话 baseline；
4. 新模型校准与规则/模型 A/B，达标后再扩大模型接管；
5. 早源 GOTV：按 tools/lookahead/README.md 跑一场影子录制 + 评估，领先稳定再谈 live 预切。

请先检查当前代码和测试，再给出最小改动方案。
```

---

## 14. 安全/隐私提醒

- `config.json` 里的 DeepSeek Key、OBS 密码是明文；
- `decisions.csv` 含选手 SteamID/昵称，属于比赛数据；
- 两个 GitHub 仓库都是私密；
- 不要把 `config.json`、`learner.json`、`decision_model*.json.gz`、`.dem` 提交到公开仓库；
- 当前 `.gitignore` 已排除这些运行产物。
