# 导播与训练器合并记录

任务开始：2026-11-06；验收更新：2026-11-07（北京时间）。本地合并已实现，未提交、推送或发布。

## 交付

- 主仓库 `D:\AIdirector`，基线 ad3d326；迁移来源 `D:\cs2-ai-decision-trainer`，基线 5dde4dd。完整提交及 exe hash 见 `trainer-merge-validation.json`，原文件 hash 见 `trainer-migration-source.json`。
- 迁入 trainer_gui、train_decision_model、ai_trainer、demo_dataset 及训练器测试。共享导播、decision_model、calibration、story/pro/narrative 继续使用主仓库文件，未带入旧核心副本。
- train_from_demos 合并训练器的伤害标签/元数据能力，新增 collect_training_demo_rows 五项返回；旧 collect_demo_rows 四项接口及静态 Demo CLI 保留。空 Demo 返回六项的问题已修复，不代表 AID-02 其它评估问题完成。
- 主界面“专业导播 → 决策模型 → 打开训练工作台”；同 exe 的 --trainer 模式。训练子进程低优先级，主窗口重复点击不新开进程；关闭主窗口不会杀掉训练工作台，训练/校准中禁止关闭工作台。
- 构建脚本安装两份 requirements，按纳入 Git 候选的 spec 打包全部训练依赖；独立旧 exe 不再是运行依赖。源码主启动延迟导入训练入口。

## 本地验收

1. `python -m unittest test_cs2_ai_director test_ai_trainer`：140 项通过（129 项核心 + 11 项训练），不能写成 269 个独立测试。包含合成 CSV 的实际 RF/ExtraTrees 训练与 AI 故障回退。
2. `python -m unittest test_trainer_integration`：6 项通过；覆盖源码/冻结入口、重复打开、退出重开、空 Demo 新旧契约、工作台关闭保护。
3. `python cs2_ai_director.py --trainer --cli --help`：通过。
4. `python -m PyInstaller --noconfirm --clean --distpath dist/unified --workpath build/unified Yuzi_CS2_AIDirector.spec`：成功；构建日志 `build/unified-build.log`。生成 `dist/unified/Yuzi_CS2_AIDirector.exe`，旧 dist 产物未覆盖。
5. 打包 exe 使用 --trainer --cli 对 `build/merge-smoke/decisions.csv` 的252行合成数据训练，参数 --min-samples 100 --trees 40 --max-depth 4，输出 model.pkl、model.json.gz、metrics.json。进程退出，模型由 ModelArbiter 以 JSON 格式加载并完成概率计算。合成 AUC 不具有真实效果意义。
6. 实际 exe 主窗口 → 专业导播 → 打开训练工作台：成功打开同程序训练模式；检查 1062×972 主窗口和 942×952 工作台，按钮、输入、参数及日志区可见、无截断。使用隔离 APPDATA，未启动 GSI、OBS 或真实按键。
7. `git diff --check` 通过；原训练器目录未改动。未运行网站构建，网站服务代码保持原中断状态。

## 边界与后续

服务器依赖：本次本地合并无；API版本、鉴权、迁移：无新增。沿用本地 APPDATA 配置和模型目录。网络顾问仅保留既有显式训练功能，本轮测试未调用外部顾问或上传用户数据。

SRV-01/02 按用户要求暂缓。账号/CDK、首次正式模型下载、自动上传、云 worker、正式签名发布均未实现，本次不能标为平台上线。AID-02 的组泄漏/基线候选切分一致性、AID-03 纯核心抽取仍待做。真实 Demo 文件回归、长时间同时导播/训练负载、不同 DPI/小屏完整视觉覆盖未验证。

当前保留本地模型热加载兼容；未来生产版本必须把本地训练产物与正式签名模型分开，并按原计划通过资格执行门及正式发布审核。主/训练窗口版本仍为原模块版本，不作为新的正式发行号。

主仓库上传与旧远端删除待用户后续上传指令。先核验主仓库远端完整性并保存旧仓库历史/发行资产等，再删除旧仓库；本地旧目录保留。没有以“合并成功”替代远端迁移验收。

计划正文已更新 DEVELOPMENT_PLAN.md、HANDOFF.md、网站交接及计划04、review-notes 01～04；旧 docx 是历史导出，本轮以 Markdown 计划为准。
