# 训练数据集 v1：ADT-01 / TD-CLI-00 冻结候选

2026-11-10。两端已确认本目录的字段、限制和行为；以 `manifest.json` 文件清单校验所交换的同一版本。本轮仅契约、合成样本和离线验证，没有新增客户端上传、训练器界面或生产接口。网站完成独立验证后记录双方冻结摘要。`capabilities.example.json` 的 `enabled=false` 是明确的未启用状态，不得拿样例当生产能力。

训练成功后提交样本 → 持久接收 → **所有上传均待管理员筛选** → 纳入训练库 → 管理员主动创建训练。训练管理员上传不免审；上传、纳入、训练和发布是不同操作。普通程序旧采集继续使用原权限和协议；本目录的新训练器端点要求设备凭据、有效产品资格、当前 `director:train`。不接受后台 JWT 代替设备身份，不向客户端提供 worker 凭据。

## 1. 数据及身份

`Initialize.schema.json` 定义全部字段，额外字段拒绝。初始化的 `clientDatasetId` / `runId`、每个 `sourceId` 均为非空小写规范 UUID；客户端在冻结快照时生成并持久化，不因重试改变。所有权从服务端设备认证取得，客户端不能提交账号/设备/可信等级字段。

manifest 的来源和文件数组非空，sourceId 唯一，文件 index 严格为数组位置 0..N-1，每个来源至少有一个文件。每个文件仅属于一个来源。rows/positiveRows/bytes/expandedBytes 必须在文件、来源、总量各层与实际一致；positiveRows≤rows。一个文件的 partCount 必须等于 ceil(bytes/262144)。同一来源不得重复声明同一压缩文件摘要；不同来源的录制身份重复由内容/样本去重处理，不能多算可训练样本。

`kind=demo-derived` 对应 `rowSchema=demo-live36-v1`：rawDemoSha256 非空，recordingId=rawDemoSha256，parserVersion=`demo-v2-live.1`，完整 sampling 对象，identityQuality 可为 recording-only/filename-inferred/user-declared。`declaredMatchGroup` 可空；非空仅是分组声明，服务端必须独立核实，不能直接纳入。当前标签版本用 sampling.labelDefinition 和 parserVersion 共同识别，未来语义变化必须新版本。

`kind=legacy-offline|legacy-selected-only` 对应 `rowSchema=legacy26-v1`：rawDemoSha256/parserVersion/sampling=null，identityQuality=unknown；不伪补十列。recordingId=SHA256(ASCII `legacy-recording-v1:` + 原输入文件字节的64位小写SHA256)。原文件内容、路径不上传。这是输入文件身份，不是 Demo 身份、权威比赛身份或上传者身份。相同数据经过不同原文件编码可能有不同录制身份，不能以此证明无跨录制重复。

declaredMatchGroup 为空的来源、来源未经核实或缺读取器均可接收并进入待筛选，但不能被自动认定为可训练。官方比赛/其它比赛的核实关系由后台另存，不信任客户端声明。多源可属于同一声明组，跨包、跨账号、同 BO 多地图的分区隔离由审核后的服务端关系决定。

## 2. 规范化和摘要（不是通用 JSON 标准的替代实现）

此限域规则仅用于 Initialize / manifest，响应展示文本不受 ASCII 限制。

1. 输入严格 UTF-8、无 BOM，深度≤16。拒绝重复 JSON 键、尾随内容、NaN/Infinity、注释和非规范数值 token。
2. 值仅允许 object、array、null、boolean、0..9007199254740991 整数及 ASCII 可打印字符串 U+0020..U+007E。负数、`-0`、小数、指数形式和超大整数均拒绝。属性名也满足 ASCII 要求。
3. 键按 ASCII 升序，数组保持原顺序，无空白和末尾换行；整数十进制无前导零。字符串仅转义双引号为 `\"`、反斜杠为 `\\`；斜杠、`<>&` 不额外转义。输入允许等价 JSON 转义，但解析值必须符合上述字符域。
4. `manifestSha256 = SHA256(canonical(manifest))`；`requestHash = SHA256(canonical(整个 Initialize))`，不排除 runId/clientDatasetId/manifestSha256。账号、设备、站点在服务端幂等命名空间中单独绑定。
5. 每文件 `sha256` 哈希实际 gzip 字节；`contentSha256` 哈希解压后的规范 CSV 字节。不要求不同 gzip 库产生相同压缩字节，不重新压缩后对比上传摘要。传输 X-Part-Sha256 哈希该次原始分片字节。
6. clientDatasetId 是命令身份，非内容身份。相同 ID 相同 requestHash 返回既有会话/回执；相同 ID 不同请求拒绝。不同 ID 的内容重复可复用底层对象，但产生独立、可追溯、仍待筛选的提交关系，不更改旧审核或旧回执。

## 3. CSV 及36列脱敏映射

文件必须为一个完整 gzip 成员，无串接成员、尾随垃圾、其它归档、模型 JSON 或 pickle。展开内容为 UTF-8 无 BOM、LF 行尾、结尾 LF，逗号分隔、最小双引号包裹、双引号转义，按 `columns.json` 固定顺序且无额外列。单行≤65536 UTF-8字节、单字段≤2048字符。所有 wire 字段仅 ASCII 可打印字符。数值文本保留原有限十进制拼写（可带指数）；不强行跨语言重格式化浮点值，不允许 NaN/Infinity 或绝对值>1e12。hash 固定实际规范CSV字节，数值语义等价不保证 hash 相同。

| 列 | 脱敏与校验 |
| --- | --- |
| timestamp | demo 为 source_tick/64 的相对秒，精确十进制无多余末尾0；legacy 用同一来源内最早有效时刻作0，按十进制数值相减，不代表UTC。无法确定相对时间时空值，不能用于时间切分 |
| map | `^[a-zA-Z0-9_-]{1,64}$`；未知用unknown，不放自由文本或路径 |
| round | 非负整数或空；demo必填 |
| action | demo-derived固定demo；legacy-offline可demo/hold/switch；legacy-selected-only仅hold/switch |
| target_slot/current_slot | demo保留现解析器1..10；target必填、current可空，不重编号；legacy来源基数未在本契约核实时两列为空，不猜测旧索引基数 |
| rule_score/rule_gap/distance | 有限数值或空；distance非负 |
| candidate_count/group_size | 1..10或空；group_size必填。demo同候选组实际行数等于声明，且组内target_slot唯一；legacy只保留历史声明，不作为候选完整性证明，legacy-selected-only固定group_size=1，重复历史分组不代表同时候选 |
| is_ambiguous | 0/1或空 |
| group | 仅 `g_`+64位小写hash。SHA256(ASCII `candidate-group-v1:` + recordingId + `:` + UTF8原group)；原group不上传，同源同组稳定。原group缺失无法补造完整候选关系，须拒绝新包或重新采集 |
| target_hp/enemy_hp | 0..100整数或空 |
| aim_self/aim_enemy/win/enemy_low_hp/self_low_hp/proximity/enemy_damaged/hp_advantage | 必填有限数值，顺序固定pov-v1，不用近似补充缺失特征 |
| label | 必填0/1；成熟的合并事件标签，不以模型预测代替 |
| label_damage/label_kill | 0/1或空；当前demo parser的label_damage必须等于label（合并伤害或击杀），label_kill空，不重新解释为独立真值 |
| match_source_id | demo必须等于来源recordingId；不发原名称/SteamID，不冒充BO身份 |
| source_tick | demo非负整数，来源内原始tick，保持group关系 |
| label_horizon_ticks | 与source.sampling.horizonTicks一致；正整数 |
| sampling_weight | 当前demo固定1；不得伪称当前旧训练器已消费该列 |
| missing_fields | 固定 `armor_helmet_flash_defuse_bomb_ammo_approx`，表示近似/不可得字段 |
| parser_version/sampling_method | demo-v2-live.1 / periodic-live，和source一致 |
| sample_step_ticks/tickrate_assumption | 与source一致；正step、tickrate=64 |
| label_definition | 固定 `damage_or_kill_in_(tick,tick+horizon]`，与source一致 |

导出端先白名单去掉姓名、SteamID、reason、iso_time、任意文本和本机路径；wire端出现额外列直接拒绝。数值/枚举字段也不得夹带文本。源内容hash和伪名仍属于受权限保护的来源数据，不代表完全匿名。legacy缺来源、group_size或候选不全不伪造；只允许能证明为单条已选视角的数据按selected-only入口处理。其重复历史group不自动升级为完整候选事件；仅demo-live36-v1进行完整候选组校验。

## 4. 限制和容量依据

硬上限及协商字段见 `capabilities.example.json`：manifest规范字节1MiB，Initialize原始及规范字节各≤2MiB，256源、1024文件、每包200000行、压缩128MiB/展开512MiB；每文件压缩8MiB/展开32MiB/50000行；传输块固定256KiB。客户端最多2并发，服务端初始每账号最多2活动会话、预留压缩总量256MiB。服务端可下调容量/并发，不可静默改块长或提高v1硬上限。

来自实际只读统计的41源/154文件/167517行：gzip总8,300,611字节，完整36列展开69,501,209字节；最大单文件65,188压缩/524,705展开/1285行。另47,413,715字节的合并dataset已裁列，不能替代完整样本规模。本轮没有上传或复制真实样本进契约。200000沿用当前worker行预算，接收上限不保证满额样本一定能在CPU/内存预算内训练；任务仍需独立预检查。

未完成会话7日过期，此后暂存再保留7日；终态不允许借原ID重新提交异内容。成功后仅在对象已持久且引用保护建立后清理传输暂存。用户本地原件不自动删除。配额预留/释放须事务化、并发不超售，重试不重复扣配额；实现属于ADT-03/04，本轮不启用生产能力。

## 5. 方法与路径

统一基址 `/api/director/training/datasets`。所有方法必须设备认证、有效资格及当前train权限，检查每次请求而非仅初始化。`clientDatasetId` 受当前账号/设备/站点限定，跨范围查询返回404，不能通过已知ID越权。普通采集旧端点不新增train要求。

| 方法/相对路径 | 请求与结果 |
| --- | --- |
| GET /capabilities | 无body，返回能力；生产实现前应未提供或enabled=false，不能自动回退到模型上传 |
| POST / | Initialize；首次201/重试200，返回TransportState；未知结果先GET原ID，查无才原样重试 |
| GET /{clientDatasetId} | 返回TransportState，已收分片索引可用于恢复；receiving状态只说明传输进度 |
| PUT /{clientDatasetId}/files/{fileIndex}/parts/{partIndex} | application/octet-stream，X-Part-Sha256；非末片恰好256KiB，末片与清单字节余数一致；成功200 PartReceipt，同片同字节幂等，异内容409 |
| POST /{clientDatasetId}/complete | body恰为{}，全部校验通过返回200 Receipt；校验进行中可202 TransportState；缺片409，损坏422；未知结果查询receipt，不另造ID |
| GET /{clientDatasetId}/receipt | 成功后固定不可变Receipt；尚无回执404专用代码，不能据此清除传输记录 |
| POST /{clientDatasetId}/cancel | body恰为{}；receiving/verifying可取消并返回200状态，重复取消幂等；已received返回409且不撤销接收/审核 |
| GET /{clientDatasetId}/governance | 返回当前GovernanceState，revision及各source状态；和初始Receipt分离，无修改能力 |

成功使用 `{success:true,code:'OK',message:'...',data:...}`，错误同形但success=false、data=null。schema定义data对象。查询/回执/能力均Cache-Control:no-store，拒绝跨域重定向；客户端不能携令牌跟随3xx。整包提交需原子建立接收关系，半包永不进入可纳入集合。

## 6. 状态与回执

transportStatus为receiving/verifying/received/cancelled/expired/rejected；只有received有不可变Receipt，received不允许退回传输态。尚未收到内容即可超限/撤权等阻断，不凭HTTP200推断可训练。

Receipt的receivedRows=acceptedRows+duplicateRows；accepted只代表新接收的样本，绝不是审核通过。receivedFiles与manifest文件数一致，receivedRows与manifest总行数一致；sample去重不得仅凭特征相等认定同一训练样本，优先可信录制/tick/候选/版本键，legacy身份不足不得盲目跨源合并。重复内容只复用对象，不绕过新提交pending-review。

Receipt.initialGovernanceStatus永远pending-review。GovernanceState另有revision、pending-review/partially-included/included/excluded/blocked、每source状态及稳定blockers代码。质检revision变化不修改旧回执。排除/阻断可经后续管理员动作重新处理，审核与创建任务的权限和并发规则由ADT-06～10实现；本契约不给客户端审核能力。

错误代码/HTTP及重试语义见 `errors.json`。小写hex/UUID、schema格式、字段域/计数/摘要、CSV结构/标签/候选组完整性按本文件和QA参考器检查，JSON Schema不能单独表达全部规则。校验先限制原始长度和JSON结构，再字段/schema/计数、摘要、压缩、CSV内容；不得先信任声明大小进行无界解压。

## 7. 验证入口及兼容范围

运行 `python -B contracts/director-training-dataset-v1/qa/verify.py`；`fixtures.json` 列出合成最小、多BO、legacy和负例，`canonical-vectors.json`提供跨语言规范化向量。脚本只读契约及样例，无网络、数据库、队列或训练副作用。schema、CSV及gzip都在本目录固定。新样本接受不表示当前旧worker可直接读取新manifest：尚需TD-CLI-08适配，candidate-v2保持独立读取器/协议，不能塞入36列。

PowerShell 7下运行 `qa/verify-canonical.ps1`，使用独立C#实现与Python黄金字节比较规范化和请求/清单hash；该脚本不验证完整样本语义。Python若未安装jsonschema会明确记录meta-schema验证未运行；网站独立.NET/Node验证另行回报，不能将hash互通冒充API联验。fixture生成器仅合成数据，勿向其加入实际私人样本；manifest.json不包含自身，记录目录其余所有交付文件的SHA256，QA输出另存目录外，避免摘要循环。

旧director-derived-v1清单和gzip可作为本地导出输入；新包脱敏会改变内容与摘要，必须生成新对象，绝不覆盖历史派生数据。Demo候选索引保持原1～10，不重编号。旧training/submissions继续只接模型候选，原回执、模型资格及灰度不变。旧26列采集继续旧接口及legacy质量标记，上传协议升级与已有未确认批次的切换由TD-CLI-03/04后续解决，本轮不迁移队列。
