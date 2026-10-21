# CS2 AI 导播 — 开发交接日志

最后更新：2026-10-21  
当前主程序版本：**v1.9.0**  
当前训练器版本：**v1.1.0**

---

## 1. 项目定位

CS2 观战/转播导播工具，核心目标：

1. 只读取 CS2 GSI 数据，自动把观察者 POV 切到“未来 1–3 秒最可能造成击杀”的选手；
2. 回合开始触发 HLAE 出生点运镜（按上一回合胜方选 CT/T）；
3. 炸弹倒计时触发 A/B 包点运镜；
4. 观察对象阵亡后自动切到离尸体最近的敌人；
5. 专业导播：独立日志、事件动作、OBS WebSocket、FFmpeg 击杀回放、玩家改名；
6. 决策日志 + 离线随机森林仲裁模型；
7. 独立训练器支持 `decisions.csv` 和 `.dem`。

硬边界：

- 只读 GSI HTTP 推送；
- 不读游戏内存、不注入、不发送游戏控制台命令；
- 对游戏的动作只有模拟 0–9 观察者数字键，以及用户录入的 HLAE 宏快捷键；
- 不直接与 HLAE 通信。

---

## 2. 仓库与产物

### 导播主仓库（私密）

https://github.com/YuGaN525/cs2-ai-director

- 最新提交：`6d34178 Harden exe build script`
- Release：`v1.9.0`
- 资产：`CS2AIDirector.exe`
- 大小：34,991,208 字节（约 35 MB）
- SHA256：`7D19FF279A0198109D0FEDE191882F8348A528F46CB47A77087BAAB1B4B341A8`

### 训练器仓库（私密）

https://github.com/YuGaN525/cs2-ai-decision-trainer

- 最新提交：`cc2df51 Optimize trainer with RF/ExtraTrees CV search; fix onefile multiprocessing hang`
- Release：`v1.1.0`
- 资产：`CS2AIDecisionTrainer.exe`
- 大小：178,826,787 字节（约 179 MB）
- SHA256：`A8FDA480D229FE0C21621C72D14BF16769CF0B8BF4D52411538009C739D8C539`

### 本地产物

```text
dist\CS2AIDirector.exe
dist\CS2AIDecisionTrainer.exe
```

训练器和主程序建议放在同一目录，主程序的“打开训练器…”按钮按同目录 exe 查找。

---

## 3. 主仓库文件

```text
cs2_ai_director.py        # 单文件主程序（GUI/GSI/评分/切换/HLAE）
director_pro.py           # 独立日志/事件动作/OBS/FFmpeg 回放/玩家改名
decision_model.py         # 决策 CSV、3 秒标签、纯 JSON 森林加载/仲裁
train_from_demos.py       # 旧静态 demo 模型（learner.json）
test_cs2_ai_director.py   # 80 项测试
requirements.txt          # 主程序依赖（含 obsws-python）
requirements-train.txt    # demo 静态模型/训练依赖
build_exe.ps1             # 主程序 PyInstaller 打包
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
demo_dataset.py           # .dem -> demo_decisions.csv
decision_model.py         # 共享模型/CSV 定义
train_from_demos.py
cs2_ai_director.py        # 共享评分/快照核心（demo 解析需要）
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
- 日志只写“已发送”，不谎称 HLAE 已接收。

### 5.3 专业导播

`director_pro.py`：

- 独立日志：`GSI/OBS/FFmpeg/Replay/EventAction/Player/App`，GUI 独立“导播日志”页，可过滤类别/级别/时间/关键字；
- 事件动作：13 类 GSI 事件，6 种动作（播放媒体/停止媒体/切换场景/开始录制/记录击杀点/生成回放）；
- 规则支持多动作、启用/禁用、JSON 预设导入导出；
- OBS WebSocket：连接、场景创建/切换、媒体源创建/更新、播放/停止/重播、录制、媒体结束轮询；
- FFmpeg：首杀 `-4s/5s`，后续 `-1s/2s`，片段合并、重编码、concat；
- 玩家改名：赛事 API → `mirv_replace_name byXuid add` → 剪贴板。

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
- 按 CV AUC 选最终模型；
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
- 仍走阈值/迟滞/硬间隔。

### 5.5 训练器

- GUI：CSV 或 `.dem` 文件/文件夹；
- `.dem`：击杀前 160 tick（64tick≈2.5s），击杀者=1，其他候选=0；
- 输出 `demo_decisions.csv` 后训练；
- CLI：

```bat
CS2AIDecisionTrainer.exe --cli --csv decisions.csv --min-samples 1000
CS2AIDecisionTrainer.exe --cli --demos "D:\demos" --min-samples 1000
```

- 内置 scikit-learn、joblib、demoparser2、pandas；
- PyInstaller onefile 下 `n_jobs=1`，避免 joblib 多进程卡死。

### 5.6 旧静态 demo 模型

- 主程序“从 demo 训练…”仍然调用 `train_from_demos.py`，训练 `learner.json`；
- 这是旧的静态逻辑回归权重，**不再在线更新**；
- `DemoModel` 只加载权重，按 ±12.5% 调整候选分；
- 与新的随机森林仲裁可能同时生效，这是已知交互点。

---

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
hard_min_switch_interval / repeat_guard_seconds / emergency_score
bomb_cam_policy / bomb_cam_grace
hotkey_double_interval / hotkey_press_hold
hotkey_modifier_lead / hotkey_modifier_release_delay
max_vertical_diff
demo_model_enabled / demo_model_min_samples / demo_model_blend
decision_log_enabled / decision_log_interval / decision_label_window
decision_model_enabled / decision_model_margin / decision_model_min_factor
obs_host / obs_port / obs_password / obs_scene...
ffmpeg_path / replay_output_dir
event_actions_enabled / event_rules
player_api_base_url
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
- v1.9.1：训练器独立私密仓库、主程序清理训练器源码。

---

## 8. 已验证 / 未验证

已验证：

```text
主仓库 80 项测试通过
训练器仓库 80 项测试通过
Python 编译通过
核心 ruff F/E9 通过
两个 exe 启动成功
训练器 CSV 训练 exit=0
训练器 .dem 训练 exit=0（544 行 / 105 击杀事件）
导播加载 ExtraTrees JSON 成功
模型文件覆盖后自动重载成功
```

未验证：

- 真实 OBS WebSocket 连接与场景/媒体源；
- FFmpeg 实际剪辑与 concat；
- 正式比赛/多 demo 模型效果；
- 长时间运行稳定性；
- 非 100% 显示器缩放下的最终布局；
- exe 数字签名/杀软误报。

---

## 9. 已知问题/技术债

1. **OBS/FFmpeg 未实机联调**：本机 OBS 未开 WebSocket，FFmpeg 未安装。
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
16. **主程序 `_open_trainer`** 要求 `CS2AIDecisionTrainer.exe` 与主程序同目录；否则手动运行。

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

## 11. 推荐下一步

优先级从高到低：

1. **实机联调 OBS/FFmpeg**：连接 OBS WebSocket，验证场景/媒体源/录制/回放/恢复录制；
2. **积累 1000+ 真实决策样本**，训练并观察 AUC 与线上误切；
3. **多 demo 训练**：至少 5–10 场完整 demo，按比赛留出验证，避免同场泄漏；
4. **同步两个仓库的共享核心**，最好抽成独立包，消除副本；
5. **实现回合上下文特征**：alive 数、炸弹、时间、比分、经济；
6. **模型校准/阈值**：可靠性曲线、按概率分桶评估；
7. **修复 DecisionLogger positive 恢复**；
8. **给训练器加模型对比 UI**（RF/ExtraTrees 的 CV/测试 AUC 表格）；
9. **exe 签名/安装包**；
10. **清理历史 SELFCHECK 段落**，保持文档与当前版本一致。

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
FFmpeg 击杀回放
独立导播日志
玩家改名
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
```

### 英文

```text
CS2 GSI director
observer POV switching
HLAE camera macros
event actions
OBS WebSocket
FFmpeg kill replay
separate director logs
player rename
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
```

### 文件/仓库关键词

```text
cs2_ai_director.py
director_pro.py
decision_model.py
train_from_demos.py
trainer_gui.py
train_decision_model.py
demo_dataset.py
CS2AIDirector.exe
CS2AIDecisionTrainer.exe
YuGaN525/cs2-ai-director
YuGaN525/cs2-ai-decision-trainer
```

---

## 13. 新对话开场提示（可直接复制）

```text
继续开发 CS2 AI 导播项目。请先阅读仓库根目录的 HANDOFF.md 和 README.md。

当前状态：
- 主程序 v1.9.0，训练器 v1.1.0；
- 主仓库 YuGaN525/cs2-ai-director，训练器仓库 YuGaN525/cs2-ai-decision-trainer，均为私密；
- 主程序 exe：dist/CS2AIDirector.exe；
- 训练器 exe：dist/CS2AIDecisionTrainer.exe；
- 80 项测试通过；OBS/FFmpeg 尚未实机联调；
- 模型使用 RF/ExtraTrees 交叉验证选择，导出纯 JSON，导播自动重载；
- 决策样本 decisions.csv 尚未达到 1000 条。

硬性约束：
- 只读 GSI，不读内存/不注入/不发控制台命令；
- 只模拟 0-9 观察者键和 HLAE 快捷键；
- 不使用深度学习；
- 训练器与主程序共享评分核心，修改时两个仓库要同步。

下一步优先：
1. 实机联调 OBS/FFmpeg；
2. 用多场 demo 训练并验证 AUC；
3. 增加回合上下文特征；
4. 消除两仓库共享核心副本。

请先检查当前代码和测试，再给出最小改动方案。
```

---

## 14. 安全/隐私提醒

- `config.json` 里的 DeepSeek Key、OBS 密码是明文；
- `decisions.csv` 含选手 SteamID/昵称，属于比赛数据；
- 两个 GitHub 仓库都是私密；
- 不要把 `config.json`、`learner.json`、`decision_model*.json.gz`、`.dem` 提交到公开仓库；
- 当前 `.gitignore` 已排除这些运行产物。
