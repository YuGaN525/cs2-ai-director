# Yuzi_CS2_AIDirector

## 2026-11-08 BETA 1.13.0候选

已实现平台代理金额预算、Off/Platform/Personal、按账号DPAPI密钥及beta发行配套；独立候选全量测试与实际EXE自检通过。使用步骤与限制见[候选说明](docs/opt/BETA_CANDIDATE_2026-11-08.md)，版本交付以[统一进度](docs/opt/AI_DIRECTOR_PROGRESS_2026-11-08.md)为准。平台API按用户要求暂空，真实游戏、4h+24h、物理DPI/G4及公开上线未验收；以下旧日期段落为历史记录。


## 2026-11-08 下一步：仲裁双模式与公开测试

见[下一步计划入口](docs/opt/NEXT_BETA_PLAN_2026-11-08.md)。用户最新决定：程序登录后启用平台仲裁，网站管理员按金额预算分配额度，用尽停用；程序可填写自有API密钥，自有模式仍需有效产品资格且不扣平台预算。网站产品页只保留介绍、说明、下载；公开测试仍由管理员/CDK授权。

当前为公开测试准备，BETA-00～19仅完成规划，未实现新仲裁/预算或开启公测。保留本地签名模型、规则引擎、训练器、试运行与168小时执行门；移除旧面板时不能连带删掉这些控件。真实游戏、长稳、DPI及质量G4仍待验收。

## 2026-11-08 网页与程序进度

[统一进度总表](docs/opt/AI_DIRECTOR_PROGRESS_2026-11-08.md)为当前入口。程序仍是1.12.0联调候选；正式公钥及限定账号真实设备/模型接口链已通过，293项回归为前次证据。网站已部署，官网最后核验仍为1.11.0且无正式模型。游戏、物理DPI、4h+24h及G4未通过。ADW批量上传/自动解析/删原件仅完成规划，未实现。后文按日期保留历史说明，不能以旧包或HEAD冒充当前候选。

## 2026-11-07 OPT 优化候选 1.12.0（尚未正式发行）

已实现运行配置与界面偏好分离、后台任务与关闭保存、有界 OBS/时间线处理，以及共享黑金/浅色主题、字号、窗口尺寸记忆、滚动设置和默认简洁模式。导播、内置训练 GUI 与 CLI 继续使用同一 exe。

正常运镜使用 MulNX_CS2；HLAE 单列后续适配。设置支持只读核对 MulNX 地图运镜包快捷键；更新时用原运镜包恢复自定义绑定后，仍须进游戏确认。正式信任配置已由网站交接并接入独立候选构建；正式模型尚待审核发布，真实联调继续保留，详见 [DEP-WEB-01](docs/opt/DEP-WEB-01.md)。

完整 4h + 24h、真实 CS2/MulNX/OBS 和物理 DPI 验收尚未通过，不能声明 OPT-00～14 全部完成。详见 [实施与验收台账](docs/opt/OPT_IMPLEMENTATION.md) 和 [运镜适配说明](docs/opt/CAMERA_ADAPTERS.md)。下面各日期段落为对应历史交付记录，不代表这些旧包已自动包含本轮优化。

## 2026-11-07 CLI-02：统一 Demo live 采样（源码开发状态）

训练工作台、内置 Demo CLI 和 worker 共用 `demo_live_dataset.build_live_csv`，解析版本为 `demo-v2-live.1`。默认 live 阶段每 128 tick 采样，标签为未来 192 tick 内发生伤害或击杀；按 64tick 假设约为每 2 秒采样、预测未来 3 秒。缺少完整未来窗口的回合尾部不生成样本，近期伤害不跨 live 阶段起点。

```powershell
python demo_dataset.py --demos "D:/demo" --out "demo_decisions.csv" --horizon-ticks 192 --step-ticks 128
python cs2_ai_director.py --trainer --cli --demos "D:/demo" --demo-csv "demo_decisions.csv" --horizon-ticks 192 --step-ticks 128
```

新 CSV 每行带解析版本、采样方式、窗口、tickrate 假设和缺失字段；相邻 `.csv.manifest.json` 保存源 Demo/CSV SHA256 与统计。解析失败保留已有 CSV，同内容 Demo 不重复计权。旧 CSV 仍可直接训练；旧 `--tick-offset` 仅作为未来标签窗口的弃用别名，并提示语义变化。`train_from_demos.py` 是历史静态 `learner.json` 兼容工具，不属于正式决策模型训练入口。

此轮只修改程序仓库，网站和上传 v1 契约保持兼容；扩展逐行元数据目前保存在本地 CSV/清单，旧上传投影不会保留这些新增列。比赛的权威身份、多录制归组和 Demo/GSI 对齐留待 CLI-03/WEB-05；文件 hash 不代表真实比赛身份。默认调度仍为原策略。验收与后续边界见 [CLI-02 交付记录](docs/CLI-02_2026-11-07.md)。本节为源码状态，既有 1.11.0 exe 尚未重新打包。

## 2026-11-07 任务7～12本地交付与发布准备（最新）

导播/内置训练器统一1.11.0。任务7正式模型签名下载与官网发行、任务8范围分片上传/手工导入、任务9实际exe断网重启/自动补传、任务10质检/冻结快照/独立worker已本地验收。任务11审批/账号灰度/回滚与调度候选已实现，真实镜头收益G4尚未通过，保持原调度默认。任务12按用户“先完成发布准备”，不部署生产或开启灰度；主仓库97157ae8已上传并与本地提交/文件树一致；用户最新指令改为旧训练器仓库存档保留、无需删除；GitHub isArchived=true已核验，本地备份和原目录保留。详情见独立迁移JSON。

- 验收入口：主仓库 docs/TASK7-12_2026-11-07.md、TASK7-12_VALIDATION_2026-11-07.json；网站 doc/AI导播任务7-12本地验收与发布准备-2026-11-07.md。本节覆盖下方任务7仍待开发等历史状态。
- 248项Python、452项.NET、58项真实HTTP/worker/故障、40项浏览器/20张两尺寸两主题页面，两端类型/构建通过。实际QA exe首次下载正式模型、断网重启记录252行、联网自动补传confirmed、退出关执行通过；168小时策略不变。
- 018/019迁移、分权审核/发布、分离Ed25519密钥、运行中模型激活锁、撤销/更高序号回滚；director-upload-v1范围样本去重与不可变回执。director-delivery-v1契约含7份schema/OpenAPI，两仓逐字节一致。
- 用户D:\demo原件只读：12组比赛21983行固定split RF/ExtraTrees评估，最终AUC0.8016；另8组真实Demo经API流式入库/冻结/独立worker产候选。未获得同场GSI/节目时间线，AUC不能冒充镜头收益，候选不自动发布。
- 本地备份恢复31表/36对象及016～019重复迁移通过；生产克隆恢复/正式公钥注入/Linux容器实机仍待正式发布时验收。生产四开关默认false；现场原Compose整栈与其他业务开关保留。

## 2026-11-07 任务6本地交付（最新状态）

任务6产品资格、CDK/后台授权、网站设备登录、安全凭据和168小时服务端签发/续签已完成本地实现与验收。详见 `D:\AIdirector\docs\TASK6_2026-11-07.md`、网站 `doc/AI导播任务6本地验收-2026-11-07.md` 及同名验证JSON；接口为 `contracts/director-identity-v1`。本节覆盖下方早期“真实登录未接入/推荐任务6”的历史状态。

- 017增量迁移、独立产品资格域、CDK一次原码/原子兑换、手工单grant撤销、产品暂停、CAS/幂等/审计；新模块沿用实时可见+可修改RBAC，普通ADMIN不自动获权。
- 网站确认设备→客户端私钥证明轮询→短设备bearer/非滑动刷新轮换；重放撤family，改密/封号/注销/撤设备在线失效。DPAPI保护设备密钥、令牌及离线登录文件，无网站密码/管理员密钥进入exe。
- 服务端Ed25519签发账号/设备/站点/批准模型绑定凭据，最长168小时且不超过资格截止；联网成功校验才续签，重启/上传/仅联网不延长。临时断网保留有效凭据，明确撤销关闭执行；跨进程退出不会被迟到续签恢复。
- 主GUI正式自动动作接入统一账号门；导播/内置训练器/训练CLI使用同一账号与受管上传队列身份，真实设备bearer上传并确认已验收。未绑定旧记录不自动改归新账号；切镜路径不等待网络/凭据文件锁。
- Python221项（含16项账号专项）、.NET431项、真实HTTP/PG59项、迁移4项、浏览器16项/8张两尺寸两主题页面、两端类型/构建、exe在线/离线重启/退出及主程序打开内置训练器通过。隔离localhost合成网站JWT不代表生产密码登录或上线验收。
- 下一步任务7：正式模型注册/签名清单/首次联网下载、安全更新与官网入口。当前ApprovedModelHashes仅为服务端批准摘要占位，默认空；客户端可信公钥与独立模型缓存缺失时拒绝正式执行。本地候选不会自动成为正式模型。
- DirectorIdentity:Enabled、DirectorData:Enabled仍默认false；未启用生产开关，未提交/推送/部署/删除远端。旧训练器仓库按后续上传与迁移核验指令处理。以下记录保留为历史。

## 持久训练记录与兼容上传（2026-11-07）

主程序及内置训练器已共用受管SQLite记录/上传队列，导播成熟数据后台落盘；训练工作台和CLI对本次明确选择的数据入队。点击“训练数据状态”查看已保存、缓冲和补传状态。未登录时数据保存在本设备未绑定区，真实网站登录待后续任务6接入。

```powershell
python cs2_ai_director.py --data-queue --report queue-status.json --export local-records.csv
python -m unittest test_uploads
```

--data-queue 默认只诊断/导出本设备未绑定记录，保留原队列数据。正式账号队列按账号/站点/设备独立处理；不扫描用户旧目录。已持久化数据可跨进程恢复，RAM缓冲显示“写入中”，不冒充已保存。完整说明见 [任务4/5验收](docs/TASK45_2026-11-07.md)，协议及共享fixture见 contracts/director-data-v1。服务器仍未部署，当前不是正式网站账号自动上传已上线。


## 任务2、3本地能力（2026-11-07）

已实现冻结训练切分/防组泄漏、训练/验证/最终测试隔离、纯规则核心wheel与确定性回放，以及DPAPI设备文件、签名168小时凭据校验和可注入授权运行入口。说明见 [本地验收](docs/TASK23_2026-11-07.md)。

```powershell
python -m unittest test_cs2_ai_director test_ai_trainer test_trainer_integration test_task23
python -m director_core.replay docs/fixtures/rule-replay-v1.json --out build/replay.json
python cs2_ai_director.py --offline-check --config local-config.json --report status.json
python cs2_ai_director.py --trainer --cli --csv decisions.csv --split-plan metrics.split.json
```

离线诊断配置字段：session_root、server_origin、account_id、model_path、trusted_public_keys（key_id→base64公钥）。它用于本地适配测试，正式信任根随后续账号/发布流程接入。网站账号登录/签发/续签尚未实现，普通GUI仍使用现有本地兼容模式；自动上传也待任务4和服务器接口。已有模型JSON若缺特征版本/顺序声明，应经可信迁移后用于新授权缓存，不能只按特征数量放行。旧CSV至少需要8个可隔离组；缺可靠来源/时间时会诊断或拒绝切分。旧版README中随机拆行/任意AI切分的描述由本节替代。


> 2026-11-07 规划补充（尚未实现）：默认联网自动上传训练数据；已有模型、已登录并完成本设备绑定且创建有效登录文件后可离线使用，最长7×24小时（168小时），按服务端成功校验并续签时间计时。数据先在本地持久记录，恢复联网自动检测补传。详见 DEVELOPMENT_PLAN.md；本地设备文件/离线验签已实现，真实网站登录和自动上传仍待接入。

## 内置训练工作台（2026-11-06）

主界面“决策模型 → 打开训练工作台”直接使用内置训练器，无需另装训练器 exe。
完整构建使用 `build_exe.ps1`，同一个包包含 sklearn、pandas 和 demoparser2，体积会增大。
源码安装：`python -m pip install -r requirements.txt -r requirements-train.txt`。

```powershell
python cs2_ai_director.py --trainer
python cs2_ai_director.py --trainer --cli --csv decisions.csv
python cs2_ai_director.py --trainer --cli --demos "D:\demos"
python cs2_ai_director.py --trainer --cli --ai --no-ai --csv decisions.csv
```

exe 使用相同 `--trainer` 参数；主界面正常启动不导入训练依赖。
工作台在独立低优先级进程运行，关闭导播不终止正在训练的工作台；工作台运行训练/校准时需等待完成才能关闭。
默认输出仍为 `%APPDATA%\CS2AIDirector`，只有选择输入并开始训练才读取数据，不自动迁移或上传旧数据。
AI 顾问仅在用户启用时按既有逻辑发送聚合画像；离线测试用 `--no-ai`。
旧导播静态 Demo CLI 保留。合并来源、验证和限制见 [合并记录](docs/TRAINER_MERGE_2026-11-06.md)。
唯一维护仓库为 cs2-ai-director；旧训练器仓库待远端迁移核验后退役。
以下为原功能说明，旧“独立训练器”部署说明由本节替代。


**Yuzi Cup 专用 CS2 AI 专业导播** —— 只读取 CS2 GSI 数据的自动观战导播：

- 规则引擎（可选 DeepSeek 仲裁）自动切到“未来 1–3 秒最可能拿到击杀”的选手第一视角；
- `freezetime` 按上一回合胜方触发 CT 或 T 出生点运镜宏；
- 炸弹剩余 ≤ 8 秒按 A/B 包点触发对应 MulNX 包点运镜宏；
- 观察对象阵亡后自动切到离尸体最近的敌人（通常是击杀者），不会停在尸体视角；
- 即时学习已移除；保留离线 demo 静态权重，并新增决策 CSV 记录 + 随机森林离线仲裁模型；
- 专业导播：独立导播日志、事件动作规则、OBS WebSocket 场景/媒体控制；
- **v1.10 Phase 1**：镜头时间线（ShotTimeline）+ 六项专业指标 + 回合故事状态（阶段/经济/残局/下包/比分），不参与评分；
- **v1.10.2**：修复 GSI `observer_slot` 与观察者数字键错位（键 N→slot N-1 mod 10），日志/GUI 同时显示 slot 与键；
- **v1.10.3**：防碎切（最短停留 2s + 10 秒切率上限 3）+ 当前 POV 交火/拆包锁定不切；MulNX 增加管理员权限检测与提示；
- **v1.10.4**：适配 MulNX 的 200ms 连击缓冲——最后一次按键后 Alt 保持 300ms 再松开，修复“日志已发送但运镜不触发”；
- **v1.10.5**：移除 FFmpeg 回放、录制/击杀点/生成回放动作和玩家改名；只保留 OBS 场景/媒体事件动作；
- **v1.10.6**：品牌化为 `Yuzi_CS2_AIDirector` / `Yuzi_CS2_AIDecisionTrainer`，加入鱼籽杯图标、深色页头与卡片式界面；
- **v1.10.7**：训练器按回合/事件分组切分、模糊样本加权、top-1 评估；demo 标签对齐伤害窗口；仲裁增加概率边际；
- **v1.10.8**：勾选框改为原生 √；修复保存配置后事件规则不同步到 OBS 事件线程；增加“测试选中规则”和 OBS 媒体源/场景名校验；
- **v1.10.9**：修复比赛结束时 `phase_countdowns="over"` 覆盖 `map.phase="gameover"`，导致“比赛结束”事件不触发；增加 GSI 阶段切换日志；
- **v1.10.10**：GSI 不提供暂停状态，新增暂停热键/按钮，手动派发 `pause_started/pause_over`；
- **v1.10.11**：修复 CS2 在 over 阶段提前改 round 导致下一回合被立即结算、真实指标丢失；新增回合边界回归测试；
- **v1.10.12**：新增 Platt 在线概率校准（比赛结束自动拟合/验证/回滚保护），训练器增加“校准线上模型”和 top-1/模糊样本诊断；
- **v1.10.13**：校准只使用模型训练之后的线上样本，避免用训练 demo 自我校准；
- **v1.10.14**：新增回合叙事提示（freeze 比分/经济/出生位参考，回合结束胜方/比分），可选推送 OBS 文本源，不自动切镜；
- **v1.10.15**：运行时优化：候选评分按快照缓存、日志批量刷新、训练器校准批量推理/AUC O(n log n)；不改变评分与镜头结果；
- **v1.10.16**：新增一键导出/导入模型与配置 ZIP，导入前自动备份，密钥默认不导出；
- **v1.10.17**：会话指标按启动重置；校准阈值由验证集 F1 自动选择；
- **v1.10.18**：开局/控图阶段优先把镜头给手持或刚投掷手雷的选手；手持约 100ms 确认后预切，投掷前后保留 250–500ms 缓冲，场上偏热时自动缩短；默认不含诱饵弹；
- tkinter GUI：检测/选择 `cs2.exe`、一键安装 GSI 配置、录入快捷键与 API Key、持久化配置。

> 安全边界：程序**只**读取 GSI HTTP 推送。对游戏的动作只有“模拟 0–9 数字键”和“模拟 MulNX 快捷键（出生点 + A/B 包点）”，不读内存、不注入、不发送游戏控制台命令。

---

## 0. 直接运行 exe（推荐）

本仓库 Release 只提供 AI 导播主程序：

| 程序 | 用途 |
|---|---|
| `Yuzi_CS2_AIDirector.exe` | AI 导播主程序 v1.10.18，约 36 MB（Release v1.10.18） |

当前本地构建：36,527,430 字节（约 36.5 MB），SHA256 `22D1D7CA6412A1ED835DDEA2C4B906C52251744527D7CB3CC0E71A9D9261754E`。
v1.10.18 exe 已打包并通过启动验证（窗口标题 `Yuzi_CS2_AIDirector v1.10.18 · CS2 AI 专业导播`）。

- 不需要安装 Python；
- 双击即可，建议右键“以管理员身份运行”；
- 配置、日志、`decisions.csv`、`learner.json`、`shot_timeline.csv`、`round_metrics.csv` 仍在 `%APPDATA%\CS2AIDirector\`；
- 主程序内置 OBS WebSocket 依赖，但不打包 scikit-learn；它读取训练器生成的
  `decision_model.json.gz`；
- 未签名，Windows SmartScreen 可能提示，选择“更多信息 → 仍要运行”。

### 一键备份/恢复

控制条“导出备份…”/“导入备份…”会打包：

```text
config.json
decision_model.pkl
decision_model.json.gz
decision_model_metrics.json
decision_calibration.json
learner.json（如果存在）
```

- 导出时可选是否包含 DeepSeek API Key / OBS 密码；默认不包含；
- 导入前自动备份到 `%APPDATA%\CS2AIDirector\backup_import_时间戳`；
- 导入会校验 ZIP 路径/大小/JSON，并拒绝非法归档；
- 导入后模型立即重载，端口/快捷键/密钥等配置需重启程序生效。

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
2. **选择 MulNX 快捷键**：CT 出生点、T 出生点各 1 个，包点 A/B 各 1 个。每项用下拉选择 **ALT / SHIFT / CTRL** 之一 + **A–Z / 0–9** 之一，再选择“单次/双击”；不再支持自由输入。出生点按**上一回合胜方**选择 CT/T；包点按炸弹安放位置判定 A/B；未选择完整会被拒绝启动。
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
3. OBS 是可选依赖，未配置时 AI 导播、GSI、MulNX 运镜不受影响。

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

> 已知边界：GSI 不直接告诉程序 MulNX 当前是哪个机位，所以 `SPAWN/BOMB` 镜头按“已发送运镜 + 预计归还时间”记录；
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
| `utility_cam_enabled` | true | 开局/控图阶段优先镜头给手持/刚投掷道具的选手 |
| `utility_cam_pre_buffer` | 0.35 | 手持道具确认并预切的缓冲；运行时夹在 0.25–0.5s，场上偏热时缩短 |
| `utility_cam_post_hold` | 0.35 | 投掷后保持镜头的缓冲；运行时夹在 0.25–0.5s，场上偏热时缩短 |
| `utility_cam_fight_lookback` | 2.5 | 多久内出现伤害/高威胁候选算“场上偏热” |
| `utility_cam_min_interval` | 3.0 | 两次道具镜头的最小间隔（秒） |
| `utility_cam_max_per_round` | 4 | 每回合最多道具镜头次数；<=0 不限制 |
| `utility_cam_types` | 烟雾/闪光/燃烧/高爆 | 识别的手雷类型；默认不含诱饵弹 |
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
| `decision_model_min_prob` | 0.55 | 未校准模型推荐目标的最低概率 |
| `decision_calibration_enabled` | true | 是否启用线上 Platt 概率校准 |
| `decision_calibration_min_samples` | 500 | 自动校准所需的最少已标注样本 |
| `decision_model_calibrated_min_prob` | 0.16 | 已校准模型的最低概率（对应旧 0.55 所在尺度） |
| `decision_model_calibrated_prob_margin` | 0.02 | 已校准模型的概率边际 |
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
| `narrative_enabled` | true | freeze/回合结束叙事提示 |
| `obs_narrative_enabled` | false | 是否把叙事推送到 OBS 文本源 |
| `obs_narrative_source` | 空 | OBS 文本源名（text_gdiplus / text_ft2） |
| `auto_focus` | true | 按键前自动把 CS2 拉到前台 |
| `dry_run` | false | 演练模式 |

其余参数见 `DEFAULT_CONFIG`（文件顶部）。

**开局道具镜头（v1.10.18）**：程序用 `active_weapon` 识别“手持手雷”，手持约
100ms 确认后预切；用两次快照之间 `allplayers_weapons` 的手雷条目/弹药数减少识别
“刚投掷”。只在回合故事的 `opening`/`control` 阶段生效，仍受最短停留、10 秒切率、
交火锁定和人工接管约束。投掷前后使用 250–500ms 缓冲：场上安静时用 500ms，
有近期伤害或高威胁候选时缩短到 250ms。全局 `min_shot_duration`（默认 2s）和
`min_dwell_seconds`（默认 1s）仍然优先，所以实际镜头通常会长于缓冲时间；
希望更短的道具镜头可同时调低这两项。
默认识别烟雾弹、闪光弹、燃烧弹和高爆手雷，不含诱饵弹。若选手只是把道具丢给队友，
也可能被识别为一次投掷；正式使用前建议先用真实 GSI 跑一局确认日志与实际一致。

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

**MulNX/MulNX 运镜不触发或触发两次**
- **MulNX 不是 MulNX**：它用 `GetAsyncKeyState` 每 10ms 轮询，并把按键后 **200ms** 的连击计数放进缓冲区；检查连击时还要求 Alt 仍按住。v1.10.4 起默认在最后按键后保持 Alt 300ms（`hotkey_modifier_hold_after=0.30`）；旧版 50ms 就松 Alt，所以会出现“日志已发送但不触发”；
- 导播的“单次/双击”必须与 MulNX 运镜包/宏 YAML 里的 `comboClick` 完全一致：`comboClick: 1` 用单次，`comboClick: 2` 用双击。只改导播 GUI 不会改 MulNX 的宏；
- v1.10.3 启动时会打印“权限=管理员/普通用户”；如果 CS2/MulNX 由 MULNX 等启动器以管理员运行，本程序也必须**右键以管理员身份运行**，否则 Windows UIPI 会拦截模拟按键：日志显示“已发送”，游戏里却没有反应；
- 先在设置里把该项改成“单次”试一次，再改回“双击”；双击是“按住 Alt 连点两次”，若 MulNX 只认单次 alt+N，双击可能先触发再取消；
- 日志只会写“已发送”，无法确认 MulNX 是否接收；若没反应，把 `hotkey_press_hold` 调到 0.06–0.08，或把 `hotkey_double_interval` 调到 0.15–0.25；
- 快捷键现在是下拉选择：**ALT/SHIFT/CTRL 三选一 + A–Z/0–9 之一**，不再支持 `f6`、`ctrl+shift+f9` 这类自由输入；旧配置会显示“请选择”，需要重新选；
- 指南的交互格式是 `alt+按键`：选择 ALT + 对应字母/数字；
- 相机需要**长按 Alt 同时短按两次其它键**：对应项选“双击”。程序已实现为“按住 Alt → 点主键 45ms → 间隔 120ms → 再点一次 → 松开 Alt”，不是两次完整 alt+1；
- 如果一按就触发两次/镜头被取消：改回“单次”；
- 确认 MulNX/运镜工具以管理员运行，且没有忽略模拟输入。

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
5. CT 出生点、T 出生点、A 包点、B 包点四个快捷键与 MulNX 内绑定**完全一致**（下拉选择 ALT/SHIFT/CTRL + A–Z/0–9）。
6. 四个宏的“单次/双击”与实际 MulNX 行为一致；带修饰键的数字/字母不会暂停 AI，裸数字才会。
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
- `test_cs2_ai_director.py`：核心逻辑测试，共 129 项（不启动 GUI、不发真实按键）
- `director_pro.py`：独立日志、事件动作、OBS WebSocket 场景/媒体控制
- `director_story.py`：镜头时间线、六项专业指标、回合故事状态（Phase 1）
- `director_narrative.py`：freeze/回合结束叙事文案；可选 OBS 文本源
- `decision_model.py`：决策 CSV、3 秒标签回填、随机森林模型加载/仲裁
- `decision_calibration.py`：线上 Platt 概率校准、分组验证、自动写入/失效保护
- `backup_bundle.py`：模型/配置 ZIP 一键导出导入、校验、备份
- `train_from_demos.py`：从 `.dem` 离线训练静态 `learner.json`（主程序旧 demo 模型）
- `build_exe.ps1`：主程序 exe 的打包脚本
- `SELFCHECK.md`：自检清单、修复记录与仍需实机验证的步骤
- `DEVELOPMENT_PLAN.md`：开发计划、模块进度、线上指标与下一阶段优先级
- `frozen_v1.0.0/`：优化前冻结基线（含 SHA256）
- `requirements.txt`：依赖
- `start_cs2_director.bat`：无控制台启动脚本

## 1.13.0 BETA候选

新增关闭、平台额度、自有API三种仲裁模式；自有密钥按账号DPAPI加密，备份不导出密钥。登录与已批准模型的原执行门保持。使用步骤见[候选说明](docs/opt/BETA_CANDIDATE_2026-11-08.md)。平台API当前按用户要求留空；代码候选不代表真实游戏灰度或公测已开放。
