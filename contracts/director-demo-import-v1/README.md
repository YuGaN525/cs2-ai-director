# Demo 批量导入 v1

仅当前具有 `director:train` 权限的后台管理员可使用。网页普通用户和客户端不复用此原始 Demo 接口。旧 director-upload-v1、旧 schema1 快照保持兼容。

单文件严格小于 50,000,000,000 字节；批次最多 100,000,000,000 字节、200 个文件。分片为 8 MiB，末片可不足，服务端校验 Content-Length 和 SHA-256。ZIP、RAR、7z 不支持密码和分卷；路径穿越、链接、展开量超限拒绝处理。具体资源限制读取 capabilities。

1. 创建批次保存文件 SHA、长度及相对路径，requestId 保证重复请求不建第二份。
2. GET 文件详情恢复已确认分片；PUT 分片携带当前批次 generation。确认只针对已持久化字节，不代表整份分析成功。
3. complete 返回 202，进入 queued；后台 worker 自动领取并处理。接管增加 generation，旧上传和旧处理租约失效。
4. worker claim 返回 Lease 或 null；每 15 秒 heartbeat，租约 90 秒。解析子进程无网络、密钥及 Docker 接口，受内存、CPU、时限和磁盘预算约束。
5. 结果分片先持久化并复核摘要，再提交不可变 receipt。相同回执可重试，冲突回执不可覆盖。成功来源初始 quarantined，经来源核实与质检批准后才可冻结新训练快照。
6. 可靠保存可重训数据后删除服务器原始文件。失败/取消原件保留 7 天；无操作上传 7 天过期，再保留 7 天清理。用户自己的本地原件不删除。

新快照支持 derivedIds 并使用 schema2。每个来源保留原始 SHA、固定解析版本、manifest 摘要及独立 gzip CSV 分片；同一原始 Demo 与对应派生来源在快照内合并，不能通过重复导入扩充权重。来源分组由后台核实，同 BO 的地图与重复录制必须同组，不能跨训练/验证/测试分区。

生产统一固定 Linux 镜像；Windows 本地解析只用于开发验收。浮点库及压缩实现可能导致跨环境字节不同，不能绕过既有不可变对象冲突。

OpenAPI 描述管理与内部 worker 路由，JSON schema 和 fixtures 固定新增请求及结果格式。语义约束（总量、权限、租约、连续索引、行数总和、SHA）仍由服务端校验。内部路径必须在公网 nginx 返回 404，worker key 和 lease token 不得写入日志。
