"""Tk integration checks with isolated data and no real input/account actions."""
import copy
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
errors=[]
results=[]
with tempfile.TemporaryDirectory() as temp:
    os.environ['APPDATA']=temp
    import cs2_ai_director as d
    import trainer_gui
    from desktop_ui import settings,schedule,ScrollableFrame
    from upload_outbox import Binding
    account=mock.Mock(status='未登录测试账户',tokens=None)
    account.allowed.return_value=False
    account.binding.return_value=Binding('unbound','local','fixture')
    account.upload_identity.return_value=None
    account.local_device='fixture'
    account.status_snapshot.return_value=dict(allowed=False,logged_in=False,reason_code='login_required',remaining_seconds=0,last_verified_at=None,model_state='未登录')
    with mock.patch('account_client.default_account',return_value=account),mock.patch.object(d.DirectorApp,'_register_control_hotkeys'),mock.patch.object(d.DirectorApp,'_unregister_control_hotkeys'):
        for cls,args in ((d.DirectorApp,(copy.deepcopy(d.DEFAULT_CONFIG),)),(trainer_gui.TrainerApp,())):
            app=cls(*args)
            app.root.report_callback_exception=lambda *error:errors.append(str(error))
            try:
                for size in ('640x480','960x650','1280x800'):
                    for mode in ('dark','light'):
                        for developer in (False,True):
                            app.preferences.update(theme=mode,developer_mode=developer)
                            app._apply_preferences()
                            app.root.geometry(size)
                            app.root.update()
                            from tkinter import ttk
                            style=ttk.Style(app.root)
                            from desktop_ui import PALETTES
                            assert style.lookup('TCheckbutton','foreground',('active',))==PALETTES[mode]['text']
                            assert style.lookup('TCheckbutton','background',('active',))==PALETTES[mode]['card']
                            for name in ('TButton','Accent.TButton','Stop.TButton'):
                                for states in ((),('active',),('pressed',),('focus',),('disabled',)):
                                    for edge in ('bordercolor','lightcolor','darkcolor'):
                                        expected=PALETTES[mode]['accent' if states==('focus',) else 'border']
                                        assert style.lookup(name,edge,states)==expected,(name,edge,states)
                            if hasattr(app,'btn_stop'):
                                for button in (app.btn_start,app.btn_stop):
                                    y=button.winfo_rooty()-app.root.winfo_rooty()
                                    assert 0<=y and y+button.winfo_height()<=app.root.winfo_height()
                            if hasattr(app,'submission_status'):
                                for label in (app.submission_status,app.lbl_status,app.dataset_note):
                                    assert int(label.cget('wraplength'))<=label.winfo_width()
                            settings(app)
                            app.root.update()
                            app._settings_window.destroy()
                            app.root.update()
                            if hasattr(app,'datasets'):
                                from dataset_ui import open_datasets
                                dataset_window=open_datasets(app);dataset_window.geometry(size)
                                app.root.update();dataset_window.destroy();app.root.update()
                            results.append([cls.__name__,size,mode,developer])
                for font_size in (9,14):
                    app.preferences.update(font_size=font_size)
                    app._apply_preferences()
                    app.root.geometry('640x480')
                    app.root.update()
                    settings(app);app.root.update()
                    app._settings_window.destroy();app.root.update()
                    if hasattr(app,'btn_stop'):
                        button=app.btn_stop
                        y=button.winfo_rooty()-app.root.winfo_rooty()
                        assert 0<=y and y+button.winfo_height()<=app.root.winfo_height()
                app.preferences.update(font_size=10)
                app._apply_preferences()
                import tkinter as tk
                from tkinter import ttk
                focus_window=tk.Toplevel(app.root)
                focus_window.geometry('320x180')
                focus_page=ScrollableFrame(focus_window)
                focus_page.pack(fill='both',expand=True)
                for i in range(30):ttk.Label(focus_page.inner,text=f'line {i}').pack()
                focus_button=ttk.Button(focus_page.inner,text='last action')
                focus_button.pack()
                app.root.update()
                before_focus=focus_page.canvas.yview()
                focus_page.inner.event_generate('<FocusIn>')
                app.root.update()
                assert focus_page.canvas.yview()==before_focus, 'ancestor focus moved click target'
                focus_button.focus_force();app.root.update()
                y=focus_button.winfo_rooty()-focus_page.canvas.winfo_rooty()
                assert 0<=y and y+focus_button.winfo_height()<=focus_page.canvas.winfo_height()
                focus_window.destroy();app.root.update()
                posted=[]
                t=threading.Thread(target=lambda:schedule(app.root,0,lambda:posted.append(threading.get_ident())))
                t.start();t.join()
                deadline=time.monotonic()+2
                while not posted and time.monotonic()<deadline:
                    app.root.update();time.sleep(.01)
                assert posted==[threading.get_ident()]
                before=app.root.bind('<MouseWheel>')
                for _ in range(100):
                    frame=ScrollableFrame(app.root)
                    frame.destroy()
                normalize=lambda value:[line for line in value.splitlines() if line.strip()]
                assert normalize(app.root.bind('<MouseWheel>'))==normalize(before),(before,app.root.bind('<MouseWheel>'))
                if hasattr(app,'_collect_cfg'):
                    cfg_before=app._collect_cfg()
                    app.preferences.update(developer_mode=False)
                    app._apply_preferences()
                    assert app._collect_cfg()==cfg_before
            finally:
                if hasattr(app,'timeline'):assert app.timeline.close(time.monotonic())
                assert app.training_data.close()
                if hasattr(app,'datasets'):assert app.datasets.close()
                if hasattr(app,'pro'):app.pro.stop()
                app.root.destroy()
                logging.shutdown()
        logging.shutdown()
assert not errors,errors
print(json.dumps({'cases':len(results),'matrix':results,'callback_errors':errors,'font_extremes':[9,14],'real_os_dpi_matrix':False},ensure_ascii=False,indent=2))
