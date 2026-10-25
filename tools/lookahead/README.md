# 早源 GOTV Lookahead（影子模式）

用独立获取的 [cs2-headless-observer](https://github.com/Starfie1d1272/cs2-headless-observer)
录制**早于节目画面**的 GOTV 事件，测量真实领先时间，并评估“按节目时间预切”
本来能多拍到多少次击杀。

> 当前只做记录与评估，**不改变导播的任何镜头决策**。live 预切要等影子数据
> 证明领先稳定、覆盖增益足够后再单独实现。

## 为什么要影子模式

早源事件到达时，节目画面还在 N 秒之前。事件一到就切会让镜头提前 N 秒停在
目标身上（可能长达几十秒，观感很差）。正确做法是：

```text
节目时间 = 早源事件接收时间 + 领先时间
目标切镜时刻 = 节目时间 - 期望提前量（约 1-2 秒）
```

所以必须先量出领先时间，再谈调度；本目录负责量领先和算机会。

## 前置条件

- Windows x64，已登录 Steam，已安装 CS2；
- 赛事方提供**获授权的早源 GOTV** 数字 `IP:port` 与密码；
- 观察器发行包（推荐 `windows-x64`，内含 Python/Node）：
  https://github.com/Starfie1d1272/cs2-headless-observer/releases
- 来源 host 版本（作者当前验证为 `14192`，以赛事方为准）；
- CS2 的 `game/bin/win64/steam_api64.dll`。

观察器是 **AGPL-3.0** 第三方组件：不要复制进本仓库、不要打包进导播 exe。
解压到仓库外目录（例如 `D:\tools\cs2-headless-observer-1.0.0-windows-x64`），
校验 Release 的 `SHA256SUMS.txt`，仅通过公开的托管 JSONL 接口调用。

## 1. 录制一场

先启动游戏客户端，再启动观察器（同一 Steam 账号时，两者必须使用不同的
GOTV 端点；重启游戏前先停观察器）。然后运行：

```bat
set GOTV_PASSWORD=赛事方提供的密码

python tools\lookahead\observer_bridge.py ^
  --package "D:\tools\cs2-headless-observer-1.0.0-windows-x64" ^
  --steam-api "D:\SteamLibrary\steamapps\common\Counter-Strike Global Offensive\game\bin\win64\steam_api64.dll" ^
  --endpoint 10.0.0.5:27020 ^
  --host-version 14192 ^
  --out lookahead_runs\match1
```

首次先加 `--check` 只校验路径/参数，不启动观察器。

目录里会得到：

```text
lookahead_runs/match1/
  lookahead.jsonl        每行 {"recv": 接收墙钟, "record": 观察器原始记录}
  observer.stderr.log    观察器诊断日志
  bridge_report.json     记录数、序号、退出码、事件统计
  observer/              观察器自己的会话报告
```

要点：

- 密码只从 `GOTV_PASSWORD` 或交互提示进入观察器 stdin，不写入任何文件；
- `recv` 是**剪辑机本地接收时间**，评估靠它和导播 GSI 对齐；
- 正常停止：`Ctrl+C`，桥会给观察器 15 秒收尾；不要直接杀进程；
- 一场/一张图换一个新端点；同账号重复连同一 `IP:port` 会替换旧连接（作者实测
  `1000/shutdown`）。

## 2. 评估覆盖机会

导播同时正常运行，产出 `shot_events.csv` / `shot_timeline.csv`。赛后：

```bat
python tools\lookahead\evaluate_lookahead.py ^
  --events lookahead_runs\match1\lookahead.jsonl ^
  --out lookahead_runs\match1\eval.json
```

默认读取 `%APPDATA%\CS2AIDirector\` 下的三个 CSV。输出：

```text
eval.json                 汇总 + 每名死者明细
eval_deaths.csv           配对明细（CSV）
```

关键指标：

| 指标 | 含义 | 进入 live 的经验门槛 |
| --- | --- | --- |
| `lede_seconds.median` | 早源领先节目画面的秒数 | 稳定 >1.0s |
| `lede_seconds.positive_rate` | 正领先占比 | >90% |
| `coverage.killer_on_camera_rate` | 死亡时杀手本来就在拍的比例 | 作为基线 |
| `coverage.potential_precut` | 领先足够、杀手不在拍、可预切的次数 | 越多越值得做 |
| `unmatched_gsi_deaths` | 未配对的 GSI 死亡 | 0 或接近 0 |

评估器按回合把早源 death 与 GSI `shot_events.csv` 的 death 按顺序配对，
自动尝试回合号偏移 `-1/0/+1`；杀手槽位优先用 `decisions.csv` 的
`target_steamid ↔ target_slot` 映射，找不到时退回观察器 controller slot 并给出警告。

## 3. 判定是否进入 live

只有同时满足以下条件才考虑实现 live 预切：

- 领先中位数稳定、正领先 >90%，且不同回合波动不大；
- 影子评估显示 `potential_precut` 有实质数量（例如每场十几次）；
- 配对缺失/身份未知低，不是靠模糊配对“凑”出来的；
- 赛事规则允许早源只用于导播/观察，不向选手、教练、投注方泄露；
- 有 GSI 回退：观察器断线、延迟源、领先异常时立刻回到现有规则。

live 调度应采用“节目时间排程”，而不是事件到达即切：

- `player_death` 到达：若杀手不是当前 POV，在
  `recv + lead - desired_advance` 时刻预切杀手（`desired_advance` 约 1–2s）；
- 若当前 POV 就是杀手：保持，不因普通规则切走，确保击杀落地；
- `player_hurt` 先只记录，不直接触发；等影子数据证明其增益后再启用；
- 所有预切写入 `shot_timeline.csv` 的 `source=lookahead`，并遵守最短停留、
  切率上限与手动接管；任何异常回退 GSI 规则。

## 隐私、账号与合规

- **不要**把 `lookahead.jsonl`、观察器采集、GOTV 地址/密码上传或公开；其中含
  Steam64 与比赛数据；
- 观察器使用真实 Steam 账号与 App730 上下文，仓库没有 Valve 授权声明；
  账号/ToS 风险需自行向 Valve/赛事方确认，建议使用专用观战账号与专用机器；
- 观察器是 AGPL-3.0：不要修改后闭源分发；不要把其源码/二进制并入导播发行包；
  本目录只调用公开 CLI/JSONL 接口；
- 早源只能用于导播决策。禁止把未来事件显示到直播画面、HUD 或任何选手可见渠道。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `--check` 报找不到 entrypoint | 确认发行包完整；必要时 `--python/--driver/--native/--worker` 覆盖 |
| 握手/初始化超时 | 核对端点、密码、host 版本；确认来源允许观战 |
| 同账号连接被替换 | 换用不同 GOTV 端点；不要与游戏客户端共用同一端点 |
| `exit_code != 0` | 看 `observer.stderr.log` 与 `observer/` 会话报告 |
| 评估配对为 0 | 确认导播与观察器同一场；检查 `shot_events.csv` 是否有 death；查看时间窗 |
| 领先中位 ≤0 | 可能连的是延迟源，或两边不是同一时间线；不要进入 live |
