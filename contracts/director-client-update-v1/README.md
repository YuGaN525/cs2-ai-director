# 应用更新签名契约 v1

2026-11-09实施补记：程序下载/安装器已实现并完成隔离实际exe事务验收；网站.NET签名和原黄金文件已跨端核对。正式公开信任位于`config/client-update-trust.public.json`，网站正式签名源尚未部署。具体证据与联合验收余项见`docs/opt/UPD_1.15.0_VALIDATION_2026-11-09.md`。本目录example.org测试信任仍只用于测试，不能装进正式包；下文提案阶段描述保留为历史。

完整网页侧要求：`docs/plan/自动更新网页侧开发要求-2026-11-09.md`。
现有账号、模型签名及五组契约保持原样。本目录不代表线上接口已实现。

- `SignedManifest.schema.json`：严格封套/字段形状；时间、序号、来源与签名等跨字段约束由 `client_update_manifest.verify` 验证。
- `test-public-trust.json`：仅用于 `https://example.org` 的一次性测试公钥，绝不能作为生产更新信任。
- `fixtures.json`：固定 `now` 的 14 个正反例；无真实文件可供安装，没有保存测试私钥。
- `payload.canonical.txt`：第一个样本的精确签名字节，无 BOM、无尾换行。中文、emoji、引号、反斜杠与 `<>&/` 用于 .NET/Python 转义一致性验证。
- `manifest.json`：上述四个样本文件的 SHA256。网站收到后逐字节核对；修改契约先协商并新增证据，不能只更新摘要掩盖差异。

执行：`python -m unittest test_client_update_manifest`。
生成签名输入只包含规范 payload JSON，无额外用途前缀；独立应用密钥、固定 schema/product、origin/channel/platform 共同验证用途。
正式公开配置、网站签名服务、OpenAPI 和撤回/下载实测由网页侧交付。客户端完整下载/退出替换尚未完成。
