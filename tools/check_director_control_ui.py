"""Hidden Tk component checks; fake hooks/account, isolated data, no game or real input."""
import copy
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
checks=[]
def check(name,value):
    checks.append({'name':name,'passed':bool(value)})
    if not value:raise AssertionError(name)

with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ,{'APPDATA':folder}):
    import cs2_ai_director as d
    from upload_outbox import Binding
    callbacks={}
    def add(chord,callback,**kwargs):callbacks[chord]=callback;return chord
    backend=SimpleNamespace(parse_hotkey=lambda chord:chord,add_hotkey=add,remove_hotkey=lambda handle:None)
    account=mock.Mock(status='隔离测试：未登录',tokens=None,local_device='fixture')
    account.allowed.return_value=False;account.upload_identity.return_value=None
    account.binding.return_value=Binding('unbound','local','fixture')
    account.status_snapshot.return_value={'allowed':False,'logged_in':False,'reason_code':'login_required'}
    root=d.tk.Tk();root.withdraw()
    errors=[]
    with mock.patch.object(d.tk,'Tk',return_value=root),mock.patch('account_client.default_account',return_value=account), \
            mock.patch.object(d,'keyboard',backend),mock.patch.object(d,'HAS_KEYBOARD',True), \
            mock.patch('client_update_ui.install'),mock.patch.object(d.messagebox,'showinfo'), \
            mock.patch.object(d.messagebox,'showerror') as dialog, \
            mock.patch.object(d.DirectorApp,'_refresh_deps'),mock.patch.object(d.DirectorApp,'_update_model_label'):
        app=d.DirectorApp(copy.deepcopy(d.DEFAULT_CONFIG))
        app.root.report_callback_exception=lambda *error:errors.append(type(error[1]).__name__)
        try:
            check('cinematic button exists in ordinary mode',app.btn_cinematics.winfo_manager()=='pack' and not app.preferences.read()['developer_mode'])
            check('default shortcuts are registered',all(k in callbacks for k in ('ctrl+alt+d','ctrl+alt+m','ctrl+alt+p')))
            app.btn_cinematics.invoke()
            check('button turns cinematics off',app.var_cinematics.get() is False and app.btn_cinematics['text']=='运镜：关闭')
            check('switch persists in isolated config',json.loads(d.config_path().read_text(encoding='utf-8'))['cinematics_enabled'] is False)
            callbacks['ctrl+alt+m']();app._poll_control_hotkeys()
            check('hotkey uses same toggle as button',app.var_cinematics.get() is True)
            old=callbacks['ctrl+alt+d']
            app.var_director_hotkey.set('F9');app.var_cinematics_hotkey.set('F10');app._save_only()
            check('saved control shortcuts immediately replace old ones',app._control_hotkeys.registered['director']=='f9' and 'F10' in app.lbl_control_shortcuts['text'])
            old();check('old queued shortcut cannot execute after save',app._control_hotkeys.drain()==[])
            callbacks['f9']();app._poll_control_hotkeys()
            check('start shortcut preserves account/model gate',app.engine is None and dialog.call_count==1)
            fake=SimpleNamespace(cfg=dict(d.DEFAULT_CONFIG,hlae_spawn_ct_key='alt+m'),
                status=lambda:{'running':True,'cinematic_pending_stop':False},set_cinematics_enabled=mock.Mock(),stop=mock.Mock())
            app.engine=fake
            app.var_director_hotkey.set('alt+m')
            try:app._collect_cfg()
            except ValueError:conflict=True
            else:conflict=False
            check('new shortcut cannot collide with immutable running macro',conflict)
            app.var_director_hotkey.set('F9')
            app.btn_cinematics.invoke()
            check('live button updates engine without changing runtime snapshot',fake.set_cinematics_enabled.call_args==mock.call(False) and fake.cfg['cinematics_enabled'] is True)
            app._toggle_director()
            deadline=time.monotonic()+3
            while app.engine is not None and time.monotonic()<deadline:root.update();time.sleep(.01)
            check('stop uses existing background cleanup and releases execution',app.engine is None and fake.stop.call_count==1 and account.release_execution.call_count==1)
            for mode in ('light','dark'):
                app.preferences.update(theme=mode,developer_mode=False);app._apply_preferences();root.update_idletasks()
                check(mode+' theme retains ordinary control button',app.btn_cinematics.winfo_manager()=='pack' and app.btn_cinematics['text']=='运镜：关闭')
            app._unregister_control_hotkeys();callbacks['f10']()
            check('window shutdown removes pending control events',app._control_hotkeys.drain()==[])
            check('no Tk callback errors',not errors)
            account.acquire_execution.assert_not_called()
        finally:
            app._closing=True;app._unregister_control_hotkeys()
            app.timeline.close(time.monotonic());app.training_data.close();app.pro.stop()
            if getattr(app,'diagnostics',None):app.diagnostics.close()
            root.destroy();logging.shutdown()

output=Path(sys.argv[1])
output.parent.mkdir(parents=True,exist_ok=True)
output.write_text(json.dumps({'scope':'hidden Tk components with synthetic account/hooks; no physical keyboard, real game or visual screenshot acceptance',
                             'checks':checks,'total':len(checks),'passed':sum(c['passed'] for c in checks)},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(f'PASS {len(checks)} hidden Tk control checks')
