# AI 导播诊断日志协议 v1

2026-11-11。用户确认：设置内手动上传，严重错误/崩溃自动上报，可关闭自动上报，断网或崩溃未上传下次启动补传；服务端保留30天，仅授权管理员查看。本协议只包含导播自身脱敏运行日志，不采集微信等应用、系统事件、聊天、凭据、配置目录或内存转储。

所有客户端接口使用既有 `Authorization: Bearer d1_…` / DirectorDevice 身份。服务器从已验证设备解析账号；不接受上传者自报UID。有效设备登录即可提交诊断，不因产品资格到期而无法求助；封禁、注销、密码变更或撤销设备仍拒绝。未登录时只保留本机有界队列，按原账号归属补传，不跨账号发送。默认每账号每UTC日5份新报告，幂等重试不占新配额。

`GET /api/director/diagnostics/capabilities` → `{success:true,code:"OK",message:"ok",data:{schemaVersion:1,enabled:true,protocol:"director-diagnostics-v1",maxLogBytes:131072,maxRequestBytes:1048576,retentionDays:30,maxReportsPerDay:5,kinds:["manual","error","crash"]}}`，外层以网站标准 ApiResponse 为准。

`POST /api/director/diagnostics`（application/json）：

```json
{"requestId":"c7e0ff70-51b4-4da5-9a78-0a34188df7c3","kind":"manual","appVersion":"1.16.3","occurredAt":"2026-11-11T02:00:00Z","summary":"手动提交运行故障","log":"[ERROR] input cancelled: foreground_changed","buildCommit":null,"osVersion":"Windows 11"}
```

必填前6项，拒绝未知字段；`requestId`是本机队列持久UUID，重试不改内容。`kind`仅manual/error/crash；appVersion为不带v的三段版本（可带SemVer预发行/构建后缀，最多48字符）；summary单行1～200字符；log非空且UTF8不超过128KiB；buildCommit可空或7～64位小写十六进制；osVersion可空、最多80字符，不能含机器名/用户目录。occurredAt须带UTC零偏移，在服务器当前时间前30天至后5分钟内。请求体最多1MiB（容纳JSON转义），不支持压缩、附件、文件路径或任意外链。

首次201，完全相同请求重试200：`data={id,status:"received",receivedAt,expiresAt}`，时间为UTC。以服务端receivedAt起算30天，不能由客户端时间延长。`GET /api/director/diagnostics/{id}` 仅返回当前账号的回执，不返回日志；支持同账号有效设备重试。已到期回执返回404。

错误：400 `DIRECTOR_DIAGNOSTIC_INVALID`、409 `DIRECTOR_DIAGNOSTIC_CONFLICT`（同账号requestId内容不同）、413 `DIRECTOR_DIAGNOSTIC_LIMIT`、429 `DIRECTOR_DIAGNOSTIC_QUOTA`（Retry-After）、503 `DIRECTOR_DIAGNOSTIC_DISABLED`，身份401/403沿既有接口。仅网络/5xx/429有界退避重试；400/409/413须人工查看，不能改ID绕过配额。重复自动故障本机按版本/异常签名去重；队列最多20份/4MiB、最长30天。关闭自动上报时停用自动采集及自动队列补传，用户仍可明确手动提交。

程序先脱敏且手动提交前可预览；自动上报仅使用本程序白名单诊断信息与必要异常片段，不把完整历史日志直接打包。服务器再次过滤凭据行、路径、地址、联系方式及原全局数字监听行。不要记录第三方窗口标题、其他应用数字、账号令牌或密钥。禁止把上传原文或请求体写入HTTP/异常日志。

后台通过 `module:director-diagnostics:view/edit`（操作映射 `director:diagnostics`）独立授权，沿既有可见/可修改规则。列表只返回元信息；详情按需读取并记录访问审计。30天到期或账号注销后删除正文；读取时立即排除到期数据，后台定时物理清理。恢复历史数据库后须先执行到期/注销清理再开放诊断接口，不能把灾备留存当在线可读期限。
