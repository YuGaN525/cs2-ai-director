"""Allowlisted status; no credentials, raw GSI, paths or config are exported."""
from client_version import VERSION

ACCOUNT_REASONS = {
    'login_pending': '等待网页确认设备登录',
    'revoked': '账号资格或设备许可已撤销，请联网恢复资格',
    'reauth_required': '登录已失效，请重新登录',
    'clock_rollback': '本机时间回退，请校准时间后联网检查',
    'model_changed': '正式模型缺失或变化，等待校验',
    'login_required': '请先登录并绑定设备',
    'model_or_validation_required': '等待正式模型下载或本地校验',
    'lease_expired': '离线许可已到期，请联网校验',
    'local_validation_required': '本地许可或模型校验未通过',
    'ready': '可执行正式导播',
}
from account_security import MESSAGES
ACCOUNT_REASONS.update(MESSAGES)


def snapshot(app):
    account = app.account.status_snapshot()
    engine = getattr(app, 'engine', None)
    state = engine.status() if engine is not None else {}
    recorder = app.training_data.recorder.stats()
    queue = getattr(app, '_queue_status', {})
    counts = queue.get('states', {})
    queue_known = 'records' in queue and not queue.get('unknown')
    cfg = getattr(app, '_runtime_cfg', getattr(app, 'cfg', {}))
    pro = app.pro.status() if hasattr(app, 'pro') else {}
    features = {
        '自动导播': '执行许可有效' if account.get('allowed') else ACCOUNT_REASONS.get(account['reason_code'], '未就绪'),
        'MulNX 运镜': '快捷键已配置，实际联动需游戏确认' if all(cfg.get(k) for k in ('hlae_spawn_ct_key','hlae_spawn_t_key','hlae_bomb_a_key','hlae_bomb_b_key')) else '请配置运镜快捷键',
        'OBS': '已连接' if pro.get('obs') else '未连接（可选）',
        '事件规则': '已启用' if cfg.get('event_actions_enabled') else '未启用',
        '叙事': '已启用' if cfg.get('narrative_enabled') else '未启用',
        '本地训练': '生成候选模型，等待服务端批准',
    }
    from director_core import __version__ as core_version
    from demo_live_dataset import PARSER_VERSION
    return {
        '客户端版本': VERSION,
        '核心版本': core_version,
        '运镜适配': 'MulNX 快捷键；HLAE 单列后续适配',
        'Demo 解析器': PARSER_VERSION,
        '账号': '已登录' if account['logged_in'] else '未登录',
        '执行条件': ACCOUNT_REASONS.get(account['reason_code'], '状态未知'),
        '离线剩余秒数': account['remaining_seconds'],
        '上次联网校验时间戳': account['last_verified_at'],
        '正式模型': account['model_state'],
        '程序': '正在关闭' if getattr(app, '_closing', False) else ('运行中' if state.get('running') else '已停止'),
        '游戏数据': state.get('gsi', '未启动'),
        '数据距今秒': state.get('gsi_age'),
        '人工接管剩余秒': state.get('paused_for', 0),
        '服务端实时资格': '未知；离线许可只代表上次校验结果',
        '运行配置版本': getattr(app, '_config_version', None),
        '镜头写入': app.timeline.persistence_status() if hasattr(app, 'timeline') else '不适用',
        '本次已保存记录': recorder.get('persisted', 0),
        '等待写入记录': recorder.get('buffered', 0),
        '未接收记录': recorder.get('not_accepted', 0),
        '记录状态': '写入异常，请检查磁盘并重试关闭' if recorder.get('error') else '正常',
        '自动同步': app.training_data.uploader.status,
        '上传队列': {'本设备当前账号记录数': queue.get('records', '尚未读取'),
                     '待传批次': counts.get('pending', 0) if queue_known else '未知',
                     '传输中': counts.get('inflight', 0) if queue_known else '未知',
                     '已确认': counts.get('confirmed', 0) if queue_known else '未知',
                     '阻塞或拒绝': counts.get('blocked', 0)+counts.get('rejected', 0) if queue_known else '未知'},
        '功能': features,
        '模式': '开发者' if getattr(app, '_ui_preferences', {}).get('developer_mode', False) else '普通',
    }
