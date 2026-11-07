# DEP-WEB-01 程序侧交接接入

> 2026-11-08 联调补充：真实设备确认、限定 UID100009 的临时模型下载/续传/激活、真实许可续签、灰度撤下与退出已完成；修复服务器轻微领先本机时新凭据尚未生效的问题。293 项程序回归通过。临时灰度已移除、测试登录已退出，官网仍为 1.11.0，正式模型仍为空。完整结果见 `DEP-WEB-01_LIVE_2026-11-08.md` 与 `DEP-WEB-01_LIVE_VALIDATION.json`；下文保留初次交接时的历史状态。游戏实测按用户要求留待后续。

交接包：`director-website-handoff-20261107-234014`。用户确认本轮仅负责程序侧。

## 范围与结果

公钥文件依赖已具备；程序按原签名信任机制接入，不更改网站、服务开关、账号资格、模型审核或发布。构建产物仍为 1.12.0 候选，不能等同正式发行或 OPT 全项通过。

- 校验 SHA256SUMS 的 34 个文件；29 个契约文件与程序仓库逐字节一致（其中原契约 manifest 管理的项目为26项，计数范围不同）。
- `identity-server.json` 仅含 HTTPS 源、账号公钥与模型公钥。文件 SHA256：`6030d327b148141ca2e1286cf9c1e01eaaa89c210cbcc40300ab13f9c2e2ecb2`。密钥 ID 分别为 `identity-web-20261107`、`model-web-20261107`。
- 使用既有 `DIRECTOR_PUBLIC_TRUST_CONFIG` 构建注入通道，输出到独立目录 `dist/dep-web-1.12`，保留原候选和官网 1.11.0。
- 账号装配和 `--check-report` 共用同一公钥配置定位函数。自检新增 `public_trust`，包含配置状态、源、规范化配置摘要和公钥 ID，不含账号凭据或机器路径。
- 冻结 exe 优先读取 exe 旁显式配置，否则读取包内固定配置；显式配置无效时保持拒绝，不偷偷退回另一个信任源。没有配置的开发源码仍按原行为拒绝正式执行。
- 本地源码未放置生产 `identity-server.json`；构建注入不会把交接包中的账号状态文件或其他机器资料一起打包。公钥来自用户指定交接，而非登录响应。

## 验收入口

```powershell
python tools/verify_website_handoff.py <交接目录> --out build/handoff-check.json
$env:DIRECTOR_PUBLIC_TRUST_CONFIG = '<交接目录>/identity-server.json'
python -m PyInstaller --noconfirm --distpath dist/dep-web-1.12 --workpath build/dep-web-1.12 Yuzi_CS2_AIDirector.spec
.\dist\dep-web-1.12\Yuzi_CS2_AIDirector.exe --check-report build/dep-web-check.json
```

`configuration_sha256` 是解析后固定字段排序、紧凑 JSON 的 SHA256，用于比较打包前后语义；与原文件摘要不同是正常的，原文件完整性另行核对。自检的 `formal_model_approval_verified=false` 表示这个本地配置检查没有验证服务器模型批准，不代表查询过生产模型列表。

精确构建 hash、全量测试、包内公钥校验和隔离账号执行限制见 `DEP-WEB-01_VALIDATION.json`。测试使用独立 APPDATA，不复用用户设备登录，不输出令牌或设备标识。

## 未完成的真实联调

交接快照记录正式发布模型数量为 0；本轮不将文件里的历史状态当作新的生产查询，也不把 QA/未审核候选转为正式模型。

1. 网站侧审核合格模型并交付模型 ID、artifact SHA、发布 sequence、评估和回退依据；仍从鉴权 API 获取签名 manifest。
2. UID100009 本人通过正常网站登录和设备码确认完成绑定；程序随后验收首次下载、续传、签名/特征/hash/向量检查和停止后激活。
3. 验证真实许可续签、离线有效/到期、撤销、账号切换、回执重试及本地记录原归属。受控时钟单元测试不能替代这一步。
4. 当前 Windows 电脑继续 CS2/MulNX/OBS 实机、4h + 24h、物理 DPI 及界面性能验收，完成后才能推进正式发行。

DEP-WEB-01 的文件交接与程序构建接入可单独关闭；完整账号/模型/游戏联调尚未关闭。网站 WEB-06 新协议仍为后续专项，本轮保持 v1。
