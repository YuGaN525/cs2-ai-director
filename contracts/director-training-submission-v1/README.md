# 训练结果提交 v1（2026-11-09 冻结）

仅内部训练器；服务器公开下载只提供导播程序。三条接口均为 HTTPS、`Authorization: Bearer d1_...`，同时校验当前有效产品资格及数据库 `director:train`（超级管理员亦可）。普通导播资格没有训练提交权限，网站 JWT 不替代设备凭据。返回 `Cache-Control: no-store`。

`GET /api/director/training/submissions/capabilities`

```json
{"success":true,"code":"OK","message":"ok","data":{"schema":"director-training-submission-v1","enabled":true,"modelFormat":"forest-json-v1","featureVersion":"pov-v1","maxFileBytes":20971520,"maxMetadataBytes":65536,"maxRequestBytes":23068672,"candidateOnly":true,"idempotencyScope":"account-device-requestId"}}
```

`POST /api/director/training/submissions`：multipart 三个字段 `requestId`（非空 UUID）、`metadata`（UTF-8 JSON）、`file`（gzip forest-json-v1 模型）。metadata 与现有模型登记一致：

```json
{"name":"本地训练结果","evaluation":{"suiteVersion":"suite-v1","splitHash":"64位小写SHA256","testRows":100,"positiveRows":20,"balancedAccuracy":0.8,"notes":"来源及评估说明"},"vectors":[{"features":[0,0,0,0,0,0,0,0],"probability":0.5}],"artifactSha256":"实际gzip文件64位小写SHA256"}
```

上例 hash 仅是说明，不能用于上传；模型和 vectors 须通过现有模型结构/预测一致性检查。metadata 不接受重复或额外字段。字段顺序/JSON空白无关；服务端解析为固定 DTO 顺序再序列化，所有字段均参与幂等指纹（含 notes、vectors 次序及实际文件 hash）；数值按 .NET double 值规范化。客户端不用计算指纹，只需重用同一 requestId 与内容。

首次成功与同内容重试均 HTTP 200；成功 data 回执形状固定：

```json
{"requestId":"11111111-1111-4111-8111-111111111111","modelId":"22222222-2222-4222-8222-222222222222","artifactSha256":"64位实际小写SHA256","artifactBytes":1234,"status":"candidate","existingModel":false,"submittedAt":"2026-11-09T00:00:00Z"}
```

`GET /api/director/training/submissions/{requestId}` 返回同一不可变回执（同样包装 success/code/message/data），不返回后续发布状态。网络结果未知先查询；404 才可重试原 POST。相同文件已存在时 `existingModel=true`，status 为登记时已有状态，不改变原审核/发布状态。新模型始终 candidate；提交不会审核、发布或扩大账号灰度。

错误采用 `{"success":false,"code":"...","message":"说明","data":null}`：404 `DIRECTOR_SUBMISSION_NOT_FOUND`（当前账号设备没有该回执）；409 `DIRECTOR_SUBMISSION_CONFLICT`（同一账号设备 requestId 对应不同内容）；403 `FORBIDDEN`（训练权限不足）或 `DIRECTOR_NO_ENTITLEMENT`；401 过期/无效凭据；400 `BAD_REQUEST`；422 `DIRECTOR_MODEL_INVALID`；429 `DIRECTOR_MODEL_QUOTA`；503 `DIRECTOR_TRAINING_DISABLED`。网关体积超限可返回 413 非 JSON，客户端不可假定所有失败都有 JSON。

客户端持久 requestId、站点、账号、设备、文件摘要及 metadata；换账号/设备不复用提交上下文。成功后保留回执，用户显式选择结果才上传，不自动上传私有数据、模型之外的路径或供应商密钥。
