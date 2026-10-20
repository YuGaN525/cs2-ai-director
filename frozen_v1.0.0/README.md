# CS2 AI 导播（CS2 AI Director）

一个只读取 CS2 GSI 数据的自动观战导播：

- 规则引擎（可选 DeepSeek 仲裁）自动切到“未来 1–3 秒最可能拿到击杀”的选手第一视角；
- `freezetime` 触发一次 HLAE 出生点运镜宏；
- 炸弹剩余 ≤ 8 秒触发一次 HLAE 包点运镜宏；
- tkinter GUI：检测/选择 `cs2.exe`、一键安装 GSI 配置、录入快捷键与 API Key、持久化配置。

> 安全边界：程序**只**读取 GSI HTTP 推送。对游戏的动作只有“模拟 0–9 数字键”和“模拟两个 HLAE 快捷键”，不读内存、不注入、不发送游戏控制台命令。

---

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
2. **填写两个 HLAE 快捷键**：必须和 HLAE 里绑定的完全一致（如 `f6`、`ctrl+shift+f7`）。留空或填单个数字键会被拒绝启动。
3. **一键安装 GSI**：程序从 `cs2.exe` 向上找到 `game/csgo/cfg`，写入
   `gamestate_integration_ai_director.cfg`。已存在同名文件会弹窗确认，并自动备份为 `.bak_时间戳`。
4. **完全退出并重启 CS2**（不是退回主菜单）。
5. **可选**：填写 DeepSeek API Key，勾选“启用 AI 仲裁”。
6. 先勾选“演练模式（不实际发送按键）”启动，观察日志/状态是否正常，再取消演练正式使用。

GSI 配置端口必须与 GUI 里“GSI 端口”一致（默认 3000）。改端口后要重新“一键安装 GSI”并重启游戏。

---

## 3. 使用

1. 进入 CS2 观战（GOTV / demo / 死亡后观战），确认数字键 0–9 能手动切第一视角。
2. 回到导播程序点“启动导播”，再切回游戏。
3. 状态区会显示：GSI 连接状态、阶段、当前观察 slot、AI 目标、手动接管倒计时、运镜触发次数。
4. 你随时按 0–9 手动接管：AI 自动暂停 10 秒（可配置），期间不会抢你的视角。
5. 点“停止”结束；配置会自动保存到
   `%APPDATA%\CS2AIDirector\config.json`，日志在 `%APPDATA%\CS2AIDirector\director.log`。

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

**AI 仲裁**：仅当规则前两名分差 ≤ `ai_ambiguity_margin`（默认 0.08）时才请求 `deepseek-chat`，
最多 12 次/分钟、超时 2.5 秒；返回空 content、超时、网络错误、返回候选外 slot 时全部回退规则引擎。
AI 建议仍要通过阈值/迟滞/冷却检查，不能绕过安全约束。

**运镜去抖**：出生点运镜按 `map + round` 去重，包点运镜按回合去重；发送失败不标记完成，1 秒后重试。

---

## 5. 关键参数（config.json）

| 键 | 默认 | 说明 |
|---|---|---|
| `gsi_port` | 3000 | GSI 监听端口，必须与 cfg 里 `uri` 一致 |
| `hlae_round_key` / `hlae_bomb_key` | 空 | 两个 HLAE 宏快捷键，必须填 |
| `manual_pause_seconds` | 10.0 | 手动按 0–9 后 AI 暂停秒数 |
| `switch_min_score` | 0.30 | 切换阈值，越低越容易切 |
| `switch_margin` | 0.10 | 新目标需领先当前目标的分数 |
| `min_dwell_seconds` | 1.0 | 两次切换最小间隔 |
| `repeat_guard_seconds` | 4.0 | 防止 GSI 延迟导致重复按同一键 |
| `bomb_cam_countdown` | 8.0 | 炸弹剩余多少秒触发包点运镜 |
| `bomb_cam_grace` | 1.5 | 包点运镜后 AI 让位秒数，设 0 可让 AI 立刻接管 |
| `ai_enabled` / `deepseek_api_key` | 开 / 空 | AI 仲裁开关与 Key（明文保存） |
| `ai_min_interval` / `ai_max_per_minute` | 3.0 / 12 | 成本与频率限制 |
| `auto_focus` | true | 按键前自动把 CS2 拉到前台 |
| `dry_run` | false | 演练模式 |

其余参数见 `DEFAULT_CONFIG`（文件顶部）。

---

## 6. 故障排查

**GSI 一直“等待数据”**
- 确认 cfg 在 `...\game\csgo\cfg\`、游戏已完全重启、端口一致；
- 用浏览器/命令行 `curl` 无法直接验证（GSI 只推送），看日志有没有 POST；
- 部分环境需要在 CS2 启动项加 `-gamestateintegration`（通过 MLAE 启动时加在 MLAE/CS2 的快捷方式上）；
- 只有观战/有 `allplayers` 数据时才能自动导播；防火墙需允许本机 127.0.0.1 回环。

**切视角没反应**
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

**端口被占用**
- 改端口 → 重新“一键安装 GSI” → 重启 CS2。

---

## 7. 需要你手动确认的事项

1. `cs2.exe` 路径是否正确（自动检测可能找到多个 Steam 库）。
2. GSI cfg 已安装并**完全重启** CS2；端口与 GUI 一致。
3. 两个 HLAE 快捷键与 HLAE 内绑定**完全一致**，且不是单个数字键。
4. 游戏内 0–9 数字键的观察者切换可用；建议先用 demo/离线 bot 局试。
5. 程序与游戏的前台/管理员权限关系（游戏若以管理员启动，本程序也要管理员）。
6. 是否接受 DeepSeek API 费用；Key 以明文保存在本机 JSON。
7. `bomb_cam_grace=1.5` 是否合适：包点运镜触发后 AI 会让位 1.5 秒；想 AI 绝对优先就设为 0。
8. 自动化输入是否符合你所在平台/比赛规则（本程序不碰内存与进程，但仍是模拟按键）。
9. 若显示器缩放不是 100%，检查窗口是否完整显示（GUI 采用“设置/日志”分页以适配较小高度）。

---

## 8. 文件

- `cs2_ai_director.py`：完整程序（单文件）
- `test_cs2_ai_director.py`：核心逻辑测试（不启动 GUI、不发真实按键）
- `requirements.txt`：依赖
- `start_cs2_director.bat`：无控制台启动脚本
