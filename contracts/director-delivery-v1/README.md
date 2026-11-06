# 导播交付与训练契约 v1（2026-11-07）

实现入口：`DirectorDeliveryController`、`DirectorTrainingController`；数据模型以同名DTO为准。许可与设备身份继续使用 `director-identity-v1`，旧CSV入口继续使用 `director-data-compat-v1`。新增协议不把旧capabilities的parts标成已支持。

## 身份与配置

设备接口只接受设备bearer；后台接口接受网站JWT并实时检查数据库中的模块可见和可修改权限。`director:model-review`、`director:model-publish`、`director:train`互不隐含。普通ADMIN不自动获得这些模块。四个功能开关默认关闭，配置和生产准备见网站 `deploy/DIRECTOR-RELEASE.md`。

| 接口 | 身份 / 行为 |
| --- | --- |
| GET `/api/director/product`、`/api/director/client-releases` | 公开；只列已启用客户端，客户端和模型版本独立 |
| GET `/api/director/client-releases/{id}/file` | 公开已启用exe；SHA256、ETag、Range；私有目录不静态公开 |
| GET `/api/director/models/manifest` | 设备且资格有效；签名封套，no-store |
| POST `/api/director/models/{id}/ticket` | 设备且资格有效；5分钟账号/设备/模型绑定票据 |
| GET `/api/director/models/{id}/file` | 设备+`X-Director-Download-Ticket`，支持Range/If-Range |
| GET/POST `/api/admin/director-models` | 审核角色登记gzip模型和submission元数据；发布角色可读 |
| POST `.../{id}/review` | 审核权限；expectedVersion |
| POST `.../{id}/publish`、`.../{id}/revoke` | 发布权限；requestId、expectedSequence、reason |
| GET/POST `.../clients`、POST `.../clients/{id}/state` | 发布权限；上传PE、不可变版本、expectedRevision |
| GET `.../rollout-account?uid=U…`、GET `.../rollouts`、POST `.../rollouts/{account}` | 发布权限；仅返回灰度所需账号ID；CAS+幂等原因；stable指针不变 |
| GET `.../applications` | 最近成功模型许可校验时间；超过2分钟显示offline-or-unknown，不能推断离线已回滚 |
| POST `.../artifacts/reconcile` | 发布权限；默认dryRun，仅24小时以上无引用models/clients/parts及其临时文件；Demo及.staging保留待人工核查 |

## 签名与激活

封套 `{payload,signature}`，Ed25519签名Base64。签名输入为ASCII用途前缀 `yuzi-director-model-manifest-v1\0` 加递归按Ordinal排序、无空格ASCII JSON。签名密钥与许可签名分离；key_id在payload。预测向量数值用往返精度字符串表示，避免C#/Python浮点JSON差异。实际payload包含manifest_schema、product、channel、server_origin、account_id、current_scope、release_sequence、issued_at、metadata_expires_at、revoked_models与current。

current包含model_id、artifact_sha256/bytes、model_format=forest-json-v1、feature_version=pov-v1、feature_manifest_sha256、features、min_client_version=1.11.0、core_major=0、max_unpacked_bytes、test_vectors、published_at。签名元数据24小时过期，离线执行仍独立取决于已验证模型和最长168小时设备许可。客户端按账号保存单调sequence及撤销集合；回滚必须新增更高sequence。运行时持有激活锁，停止后才切换；撤销立即关执行门。候选、pickle和本地校准不进入正式执行。

## director-upload-v1

能力由旧capabilities中的独立 `scopedUploads` 对象宣告。POST `/api/director/data/uploads` 冻结batchId/source/uploadSha256/uploadBytes/partCount/samples；GET `.../{batchId}`恢复状态；PUT `.../{batchId}/parts/{index}`通过`X-Part-Sha256`提交至多262144字节；POST `.../{batchId}/complete`确认；DELETE `.../{batchId}`取消。

samples包含recordId、sourceId、sequence、rowSha256。recordId/sourceId是稳定SHA256身份，不含本地路径。行hash为固定列顺序CSV单行UTF-8（含LF）的SHA256；整个上传为gzip内容hash。客户端冻结后不得重新生成身份或更改内容；相同批次请求内容变化409，相同part位置不同内容409，未完成或行/样本不一致422。配额预留、限流429，丢失/损坏对象503，不返回虚假成功回执。

样本去重范围为账号+设备下的recordId及sourceId/sequence；重叠批次只接收新样本。不能据此宣称整内容全球exactly-once。同账号其他设备与其他账号不可查询该批次。服务端持久确认后客户端才标confirmed；回执重试返回同一原回执。人工CSV导入支持owner+requestId幂等，旧客户端仍有兼容入口。CSV仍仅记录已选视角，不冒充全候选数据。

## 云训练与数据集

后台前缀 `/api/admin/director-training`：GET sources；POST demos(multipart，CS2 PBDEMS2头，最多512MiB)；POST sources/{csv|demo}/{id}/review(expectedVersion/state/matchHash/note)；GET/POST snapshots(name/batchIds/demoIds)；GET/POST jobs(requestId/snapshotId/parameters/baselineModelId)；POST jobs/{id}/cancel或retry。

源默认隔离；ready必须提供稳定比赛hash。同场不同录制或CSV必须审核绑定相同比赛hash，防止跨账号泄漏。快照至少8个独立比赛组，固定输入hash、解析版本和split策略；源删除是墓碑，不能新增依赖训练。Demo解析是定时live窗口，不以未来死亡选择采样时刻，丢弃跨回合/删失窗口，缺失GSI字段显式标记。原始Demo不会因审核自动证明线上特征对齐。

内部前缀 `/api/internal/director-training`：POST claim(workerId)；POST jobs/{id}/heartbeat(fence/leaseToken/progress)；GET jobs/{id}/inputs/{kind}/{source}?fence=N（`X-Director-Job-Lease`）；POST jobs/{id}/complete(multipart file+metadata)。所有内部请求必须有独立`X-Director-Worker-Key`，至少32字符；公网双入口应404。leaseToken不放URL。90秒租约、fence防旧worker回写，取消撤销租约并杀子进程。固定参数/内存/时间/线程预算，禁止脚本与任意命令。报告和模型再次结构/向量/hash校验，仅产出candidate，无发布权限。

HTTP统一ApiResponse封套；401无身份、403无权限、404不存在或范围隔离、409版本/幂等冲突、413大小限制、422格式/hash无效、429配额/限流、503功能关闭/存储不可用。保留原始数据与回执，不配置自动破坏性保留期。worker持久卷需要独立磁盘配额与运维清理。

## 验证边界

本地真实密码登录、PG、设备身份、C#/Python签名、实际exe、独立worker、两主题/两尺寸页面通过，不代表生产已部署。真实Demo分类评估不等于programme镜头覆盖率；缺同场GSI与完整时间线时，默认legacy调度。机器可读schema见同目录；黄金fixture中的账号和站点仅为本地QA。
