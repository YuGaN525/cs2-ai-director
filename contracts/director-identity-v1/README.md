# director-device-v1 / offline-lease-v1

2026-11-07，任务6本地实现契约。未部署；设备流为 JSON 与设备私钥证明扩展，不宣称是通用 OAuth 客户端端点。

## 路由与身份

除 start/token/refresh 成功返回原始 snake_case JSON，其余成功返回网站 `ApiResponse`（`success,data,message`）。错误沿用网站 JSON/code；轮询业务错误为 HTTP400 的 `{error}`。所有路径以 `/api` 开头。

| 方法、路径 | 身份 | 请求/结果 |
|---|---|---|
| POST `/director/device-auth/start` | 匿名，按IP限频 | `{publicKey,deviceName}`；返回 device_code、user_code、verification_uri、expires_in=600、interval=5 |
| POST `/director/device-auth/preview` | 网站JWT | `{userCode,approve:false}`；显示设备名称、12位公钥指纹、截止、是否已处理 |
| POST `/director/device-auth/approve` | 网站JWT | `{userCode,approve}`；同账号同决定可重试，其他决定/账号409 |
| POST `/director/device-auth/token` | 设备私钥证明 | `{deviceCode,proof}`；pending/slow_down/access_denied/expired_token；成功一次消费 |
| POST `/director/token/refresh` | 设备私钥证明 | `{refreshToken,proof}`；轮换凭据，重放有效旧刷新凭据撤销整个family并401 |
| GET `/director/me/entitlement` | 网站JWT或设备bearer | 当前 enabled/version/expiresAt/offlineSeconds |
| POST `/director/me/cdk/redeem` | 网站JWT | `{requestId,code}`；原子兑换、actor维度幂等 |
| GET `/director/me/devices` | 网站JWT或设备bearer | 仅当前账号的设备，无凭据 |
| DELETE `/director/me/devices/{id}` | 网站JWT | 本人设备撤销；其他账号404 |
| POST `/director/logout` | 设备bearer | 撤销本family；客户端立即写退出墓碑 |
| POST `/director/me/lease` | 设备bearer及私钥证明 | `{modelSha256,proof}`；返回签名envelope |
| GET `/admin/director/accounts?uid=…` | 实时director:license | 查询账号、产品状态、version及grant列表 |
| POST `/admin/director/accounts/{id}/grants` | 同上 | requestId、expectedVersion、UTC startsAt/nullable endsAt |
| POST `/admin/director/accounts/{id}/grants/{grant}/revoke` | 同上 | requestId、expectedVersion；仅撤销单条 |
| POST `/admin/director/accounts/{id}/state` | 同上 | requestId、expectedVersion、disabled |
| GET/POST `/admin/director/cdk-batches` | 同上 | POST requestId/label/count(1–500)/days(1–3650)/UTC expiresAt；原码只在首次成功响应返回 |
| POST `/admin/director/cdk-batches/{id}/state` | 同上 | requestId、expectedVersion、disabled |

资格是独立产品域，不赋予网站后台角色。模块须可见且可修改，或超级管理员；ADMIN不自动获得新模块权限。设备bearer仅有账号/设备标识，无网站角色，只在显式允许的导播端点生效。自动数据上传现要求当前产品资格；后台手工导入仍用原管理权限。

## 编码与凭据

设备公钥为Base64原始32字节Ed25519；proof为Base64原始64字节签名。签名字节为 UTF-8 的 `purpose + NUL + value`：

| purpose | value |
|---|---|
| yuzi-director-device-poll-v1 | device_code |
| yuzi-director-refresh-v1 | refresh_token |
| yuzi-director-lease-v1 | access_token + `:` + 小写64位modelSha256 |

短码40位随机熵、10分钟、IP及当前账号限频；客户端按返回间隔轮询，slow_down每次至少增加5秒。设备码为48字节随机；d1_访问令牌默认15分钟（1–30可配），dr1_刷新令牌默认30天（1–365可配），刷新轮换不滑动延长原family截止。DB只保存SHA256摘要，CDK128位随机、只保存摘要与末4位。改密、封号、注销中/已注销、撤设备使设备鉴权失败。

退出/换号及主程序与训练器的刷新/续签写入使用同一个Windows文件锁；私钥、令牌、离线凭据及时间高水位均DPAPI当前Windows用户保护。不保存网站密码。刷新响应丢失后不能安全恢复旧token，要求重新网站确认，不把重放当成功。

## 签名离线凭据

envelope为 `{payload,signature}`；payload见lease.schema.json。签名覆盖 `yuzi-director-offline-lease-v1`、NUL、按键名排序的紧凑UTF-8 JSON。字段是ASCII标识及Unix秒，时间保留毫秒精度。客户端固定可信公钥映射验签；公钥不得从本次登录响应获得。

`offline_expires_at = min(verified_at + 604800, 当前资格截止)`；服务端成功校验账号、设备、资格及批准模型后才签发。重启、Wi-Fi、API探活和上传成功不改变截止。到点停止正式自动动作并释放按键；网络失败保留尚有效凭据，服务端明确撤销立即关闭执行门。断网无法实时知晓远端撤销；DPAPI/签名无法防御控制本机且修改程序的攻击者。

默认无设备数量/并发产品上限；policy_version=1。当前CDK从兑换时起生效，各grant独立、并集判资格，不自动把时长叠加到既有截止；批次禁用只阻止新兑换。生产策略变更另定，不将测试策略冒充生产规则。

## 配置与默认关闭

服务端 DirectorIdentity:Enabled 默认false；ServerOrigin必须HTTPS源（仅loopback可HTTP）；SigningKeyId与SigningPrivateKeyPath指向服务器私有文件，内容为Base64原始32字节Ed25519 seed。缺密钥拒绝签发，不自动生成生产密钥。AccessMinutes/RefreshDays见上；ApprovedModelHashes为临时的服务端批准摘要列表，默认空。没有批准模型返回503 DIRECTOR_MODEL_REQUIRED；任务7才实现正式模型注册、签名清单、首次下载及更新，不把任意本地产物自动批准。

客户端按 identity-server.example.json 在exe旁配置 identity-server.json，仅含ServerOrigin及可信公钥（Base64）。空公钥映射不允许执行；私钥永不打包。已验证模型缓存 `%APPDATA%/CS2AIDirector/verified-model.json.gz` 与本地训练输出分离。默认后台每60秒校验/续签，本地每秒检查共享退出/凭据，切镜路径不访问网络/凭据文件。

测试公钥、合成用户和模型只用于localhost隔离PG。正式域名的公钥分发、生产密钥托管及模型发布待发布流程配置。本契约的shared fixture只含测试公钥与签名载荷，无token和私钥。
