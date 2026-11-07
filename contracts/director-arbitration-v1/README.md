# 仲裁 v1 契约

2026-11-08；客户端候选1.13.0。代码能力不等于生产开放。平台API按用户决定保持空配置；UID100009一次性初始化无限预算，不按角色推导。原identity/data/delivery v1身份和模型门保留。

## 状态与身份

Off / Platform / Personal。程序启用和备份导入后默认关闭，用户主动选择并启动。Platform使用DirectorDevice凭据；Personal使用本机账号范围DPAPI密钥，仍须有效产品和批准模型资格，有效既有离线许可沿用原执行门。个人密钥不送网站、不导出、不扣平台预算。

平台错误、用尽、暂停后本次云端会话停用；补额不自动恢复，不切换个人API。停止、人工接管、账号/设备、地图/回合/阶段/候选身份映射变更使旧代次无效。TTL从原始采样开始计算，默认2.5秒；候选不能绕过本地冷却、焦点与输入安全门。

## 接口

完整路径、请求及响应data schema见openapi.json。响应使用既有ApiResponse包络。

- 设备：GET policy、device-budget；POST requests；GET requests/{request}；POST requests/{request}/observation，均在/api/director/arbitration下。
- 网站本人：GET budget、budget/history。分页每页50，页码1～10000。
- 管理：/api/admin/director-arbitration下的budgets?uid=、budgets/{account}及history使用director:budget；改变无限标志须SUPER_ADMIN。
- 管理策略GET/POST policy使用director:provider。密钥只写，加密存储；GET绝不回显。
- pending、summary、requests/{row}/reconcile和correct使用director:reconcile。pending默认最早100条，也可request=UUID查历史；correct含expectedCharge并只追加差额冲正。
- 所有后台权限读取当前数据库，普通ADMIN不自动获新权限，view-only不挂载业务页。

## 金额及调用

CNY定点8位字符串，不用JavaScript Number做账。无限为unlimited=true、available=null，仍记录费用并遵守暂停/限流。Granted为累计授予，减额保留历史消耗及在途责任。

价格由管理员按实际供应商计价维护，不提供虚构默认价格。缓存输入包含于总输入，合计后向上取0.00000001元；每请求固定价格快照。结构化输入最多3072字节，按4096输入Token、128输出Token预留。真实费用超过预留仍如实入账并暂停账号。

短执行总锁→账号advisory锁→事务内预算行锁；HTTP在事务外。固定DeepSeek HTTPS源，无重定向/重试/对冲，2.5秒、64KiB响应上限。账号3秒间隔、12次/分钟、2笔未决；全局32笔pending、600次/分钟，最近一分钟5笔unknown熔断。无限账号也遵守。

pending→settled或unknown。usage确定即释放预留并记实际消费，坏建议/迟到仍计费。未知保持预留，至少经过一分钟执行窗口才可人工核账；无费用核实后释放，禁止超时自动退款/重发。查询旧回执及重复POST始终slot=null，不能复用旧镜头建议。

账本包含bootstrap、adjust、reserve、release、settle、reconcile、correction。source_request_id关联请求；预留=sum(reserve)-sum(release)，消耗=sum(settle)+sum(reconcile)+sum(correction)。冲正追加有符号差额，不覆盖原始请求。历史金额不可直接跨种类求和。

## 恢复及隐私

401/403停用并核对身份；400/422修正资料；409刷新版本并保留后台草稿；429/503/网络错误停用本次平台会话、查询原回执，不自动发起新的付费POST。管理命令相同内容保留请求ID；改内容换ID。

不存原始提示词、玩家名字、密钥或完整供应商响应。金额账本和关联请求摘要保留审计，运营清理不得破坏未决核账及汇总重建。客户端received/applied/discarded与耗时是自报，不影响计费；applied仅表示输入适配器报告成功，不证明真实游戏镜头或质量收益。

beta/stable独立；旧发行API无channel参数仍为stable。新页面显式选择beta，启用不会禁用stable。批次冻结名单、原授权、客户端/模型摘要与策略版本，不自动授权或宣告通过。结束前在原灰度管理核对撤下测试模型，长期资格保留。

fixtures均为合成数据。真实供应商费用、CS2/MulNX/OBS、4h+24h、真实DPI/G4仍是独立门禁，不能用合成结果替代。

后台供应商、计价、全局暂停分别由director:provider、director:pricing、director:pause对应的实时模块编辑权限控制；共享策略写入仅允许修改本人有权的字段。核账与预算分权独立。
