"""Finite synthetic Tk interaction timing, no real accounts or input hooks."""
import json, time, tempfile, os, sys, logging, statistics
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from upload_outbox import Binding
rows=[]
errors=[]
with tempfile.TemporaryDirectory() as root:
    os.environ['APPDATA']=root
    import cs2_ai_director as d
    import trainer_gui
    from desktop_ui import settings
    account=mock.Mock(status='未登录测试账户',tokens=None)
    account.allowed.return_value=False
    account.binding.return_value=Binding('unbound','local','fixture')
    account.upload_identity.return_value=None
    account.status_snapshot.return_value=dict(allowed=False,logged_in=False,reason_code='login_required',remaining_seconds=0,last_verified_at=None,model_state='未登录')
    with mock.patch('account_client.default_account',return_value=account),mock.patch.object(d.DirectorApp,'_register_pause_hotkey'),mock.patch.object(d.DirectorApp,'_unregister_pause_hotkey'):
        for cls,args in ((d.DirectorApp,(dict(d.DEFAULT_CONFIG),)),(trainer_gui.TrainerApp,())):
            app=cls(*args)
            app.root.report_callback_exception=lambda *error:errors.append(str(error))
            app.root.update()
            deadline=time.monotonic()+10
            while 'dependencies' in app.tasks.running and time.monotonic()<deadline:
                app.root.update();time.sleep(.01)
            try:
                for index in range(30):
                    started=time.perf_counter_ns()
                    app.preferences.update(developer_mode=bool(index%2),theme='light' if index%2 else 'dark')
                    app._apply_preferences()
                    callback_ms=(time.perf_counter_ns()-started)/1e6
                    app.root.update()
                    rows.append({'app':cls.__name__,'operation':'mode-theme-apply-layout','callback_ms':callback_ms,'ms':(time.perf_counter_ns()-started)/1e6})
                    started=time.perf_counter_ns()
                    settings(app)
                    callback_ms=(time.perf_counter_ns()-started)/1e6
                    app.root.update();app._settings_window.destroy();app.root.update()
                    rows.append({'app':cls.__name__,'operation':'settings-open-layout-close','callback_ms':callback_ms,'ms':(time.perf_counter_ns()-started)/1e6})
            finally:
                if hasattr(app,'timeline'):assert app.timeline.close(time.monotonic())
                assert app.training_data.close()
                if hasattr(app,'pro'):app.pro.stop()
                app.root.destroy();logging.shutdown()
summary={}
for app in ('DirectorApp','TrainerApp'):
    for op in ('mode-theme-apply-layout','settings-open-layout-close'):
        values=sorted(r['ms'] for r in rows if r['app']==app and r['operation']==op)
        callbacks=sorted(r['callback_ms'] for r in rows if r['app']==app and r['operation']==op)
        summary[app+'/'+op]={'samples':len(values),'callback_p95_ms':callbacks[int(len(callbacks)*.95)-1],'p50_ms':statistics.median(values),'p95_ms':values[int(len(values)*.95)-1],'max_ms':max(values)}
result={'scope':'synthetic Tk apply/layout round-trip; isolated unbound account; no training/download/upload concurrency; not human click latency', 'summary':summary,'callback_errors':errors,'samples':rows}
assert not errors,errors
Path('build').mkdir(exist_ok=True)
(Path('build')/'ui-latency.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(summary,indent=2))
