"""Fail-closed diagnostic projection: never export free-form message/exception text."""
from datetime import datetime, timezone
from pathlib import Path
import re
import builtins

MAX_LOG_BYTES = 131072
EXCEPTION_NAMES = frozenset(name for name in dir(builtins)
    if isinstance(getattr(builtins, name), type) and issubclass(getattr(builtins, name), BaseException)) | {
    'AccountError', 'IdentityError', 'ModelNetworkError', 'UpdateProtocolError',
    'ImportFailure', 'TrainingError', 'ArbitrationError', 'DiagnosticFailure',
    'ProtocolError', 'InputCancelled', 'HTTPError', 'URLError', 'InvalidSignature',
    'JSONDecodeError', 'TclError', 'OperationalError', 'DatabaseError', 'IntegrityError'}
SUMMARIES = {'manual': '用户手动提交运行日志', 'error': '程序出现严重错误',
             'crash': '程序异常退出（原因待确认）'}
# Only shipped first-party source names can appear in a traceback, never paths.
SOURCE_NAMES = frozenset(['__init__.py', 'account_client.py', 'account_ui.py', 'ai_trainer.py', 'arbitration_runtime.py', 'backup_bundle.py', 'build_plan_docx.py', 'client_update.py', 'client_update_download.py', 'client_update_install.py', 'client_update_lock.py', 'client_update_manifest.py', 'client_update_state.py', 'client_update_transport.py', 'client_update_ui.py', 'client_version.py', 'cloud_worker.py', 'config.py', 'config_service.py', 'cs2_ai_director.py', 'dataset_export.py', 'dataset_history.py', 'dataset_protocol.py', 'dataset_reader.py', 'dataset_runtime.py', 'dataset_store.py', 'dataset_transport.py', 'dataset_ui.py', 'decision_calibration.py', 'decision_model.py', 'demo_dataset.py', 'demo_import_archive.py', 'demo_import_parser.py', 'demo_import_worker.py', 'demo_live_dataset.py', 'derived_training_input.py', 'desktop_status.py', 'desktop_tasks.py', 'desktop_ui.py', 'device_identity.py', 'diagnostics_client.py', 'diagnostics_privacy.py', 'diagnostics_runtime.py', 'diagnostics_store.py', 'diagnostics_ui.py', 'director_narrative.py', 'director_pro.py', 'director_story.py', 'evaluate_programme.py', 'gsi.py', 'import_docker_sandbox.py', 'input.py', 'legacy.py', 'model_format.py', 'model_runtime.py', 'model_submission.py', 'mulnx_preflight.py', 'offline_runtime.py', 'ordered_writer.py', 'physical_modifiers.py', 'preferences.py', 'public_trust.py', 'replay.py', 'scheduler.py', 'train_decision_model.py', 'train_from_demos.py', 'trainer_gui.py', 'trainer_launcher.py', 'training_data_runtime.py', 'training_recorder.py', 'training_splits.py', 'training_submission.py', 'upload_governance.py', 'upload_outbox.py', 'upload_protocol.py', 'upload_worker.py', 'worker_limits.py'])
EVENTS = {'director.started', 'director.stopped', 'input.focus_lost', 'input.cancelled',
          'input.modifier_conflict', 'input.failed', 'gsi.waiting', 'training.started',
          'training.completed', 'training.failed', 'gui.error', 'runtime.error',
          'runtime.crash', 'runtime.unclean_exit', 'diagnostics.manual', 'runtime.started'}
PREFIXES = (('导播已启动', 'director.started'), ('导播已停止', 'director.stopped'),
            ('CS2 已失焦', 'input.focus_lost'), ('CS2 不在前台', 'input.focus_lost'),
            ('CS2 窗口激活失败', 'input.focus_lost'), ('用户正按住修饰键', 'input.modifier_conflict'),
            ('检测到用户修饰键', 'input.modifier_conflict'), ('输入已取消', 'input.cancelled'),
            ('模拟按键失败', 'input.failed'), ('发送快捷键失败', 'input.failed'),
            ('GSI：等待', 'gsi.waiting'), ('开始从 demo 训练', 'training.started'),
            ('开始解析 demo', 'training.started'), ('训练完成', 'training.completed'),
            ('demo 训练完成', 'training.completed'), ('训练失败', 'training.failed'),
            ('GUI 回调异常', 'gui.error'))
SAFE_LINE = re.compile(r'^(?:\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ )?(INFO|WARNING|ERROR|CRITICAL) event=([a-z._]+)(?: exception=([A-Za-z_][A-Za-z_0-9]{0,63}))?(?: frame=([A-Za-z_0-9./-]{1,80}):(\d{1,6}))?$')


def utc_now(seconds=None):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if seconds is not None else datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def event_line(event, level='INFO', seconds=None):
    if event not in EVENTS or level not in ('INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        raise ValueError('diagnostic_event')
    return f'{utc_now(seconds)} {level} event={event}'


def project_line(line):
    """Historical strings only contribute an allowlisted event category."""
    line = str(line)[:4096].strip()
    # Old global manual-digit logs must never enter a report, even as a category.
    match = SAFE_LINE.fullmatch(line)
    if match and match[2] in EVENTS:
        if match[4] and match[4] not in SOURCE_NAMES:
            return ''
        if match[3] and match[3] not in EXCEPTION_NAMES:
            return f'{match[1]} event={match[2]} exception=Exception'
        return line
    if any(word in line.casefold() for word in ('手动切换', '手动接管', 'manual', '按键轮询', '键盘钩子事件')):
        return ''
    # Strip only the program's conventional timestamp/level prefix.
    message = re.sub(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:,\d+)? (?:INFO|WARNING|ERROR|CRITICAL) ', '', line)
    for prefix, event in PREFIXES:
        if message.startswith(prefix):
            return f'INFO event={event}'
    if re.match(r'^\d{4}-\d\d-\d\d .* (ERROR|CRITICAL) ', line):
        return 'ERROR event=runtime.error'
    return ''


def sanitize_log(text):
    lines = []
    for raw in str(text).splitlines()[-2000:]:
        safe = project_line(raw)
        if safe and (not lines or lines[-1] != safe):
            lines.append(safe)
    selected, size = [], 0
    for line in reversed(lines):
        cost = len(line.encode('utf-8')) + bool(selected)
        if size + cost > MAX_LOG_BYTES: break
        selected.append(line); size += cost
    return '\n'.join(reversed(selected))


def exception_lines(exc_type, tb, event='runtime.error', seconds=None):
    name = getattr(exc_type, '__name__', 'Exception')
    if name not in EXCEPTION_NAMES:
        name = 'Exception'
    base = event_line(event, 'ERROR', seconds) + ' exception=' + name
    lines = [base]
    while tb is not None:
        name = Path(tb.tb_frame.f_code.co_filename).name
        if name in SOURCE_NAMES:
            lines.append(base + f' frame={name}:{min(999999, tb.tb_lineno)}')
        tb = tb.tb_next
    return lines[-8:]


def read_own_history(root):
    """No directory walk, file chooser, configuration, dumps or other apps."""
    root = Path(root).resolve()
    lines = []
    for name in ('director.log.2', 'director.log.1', 'director.log', 'trainer-diagnostics.log'):
        path = root / name
        try:
            if path.is_symlink() or path.is_junction() or path.resolve().parent != root or not path.is_file():
                continue
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                data = stream.read(65536).decode('utf-8', errors='replace')
            lines.append(sanitize_log(data))
        except OSError:
            continue
    return sanitize_log('\n'.join(lines))
