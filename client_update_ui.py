"""Non-modal GUI update checks. All networking/cache operations run off Tk."""
import json
import random
import sys
import threading
import time
import webbrowser
import queue
from uuid import uuid4
from tkinter import ttk, scrolledtext
from client_version import VERSION
from client_update import decide, cached_result
from client_update_state import UpdateState
from client_update_transport import check, executable_hash
from public_trust import read_public_trust, runtime_trust_path
from desktop_ui import schedule

MESSAGES = {'not_checked':'尚未检查', 'checking':'正在检查', 'up_to_date':'当前为所选通道最新版本',
    'update_available':'发现新版本', 'ahead_of_channel':'当前版本高于所选通道发行版，不需要降级',
    'no_release':'所选通道暂无发行', 'same_version_conflict':'同版本包信息冲突，请核查官网',
    'source_unconfigured':'更新源未配置', 'source_invalid':'更新源配置无效',
    'check_failed':'本次检查失败；历史结果不能代表当前发行', 'busy':'另一窗口正在检查',
    'service_unavailable':'发行服务暂不可用', 'disabled':'自动检查已关闭'}


class Updates:
    def __init__(self, app):
        self.app=app
        self.root=app.root
        self.title=self.root.title()
        self.result={'state':'not_checked'}
        self.cancel=threading.Event()
        self.closed=False
        self.generation=0
        self.last_manual=-float('inf')
        self.window=uuid4().hex
        self.artifact_hash=None
        self.transfer={'state':'idle'}
        self.transfer_cancel=threading.Event()
        self.transfer_events=queue.SimpleQueue()
        self.ready=None
        self.install_result=None
        self.options=app.preferences.read()
        self.root.bind('<Destroy>',self._destroy,add='+')
        schedule(self.root,10000+random.randrange(20001),self.tick)
        schedule(self.root,200,self.transfer_tick)
        if getattr(sys,'frozen',False):
            from client_update_install import last_result
            def received(value,error):
                if not self.closed and not error:self.install_result=value
            self.app.tasks.submit('update-last-result',lambda:last_result(self.app.preferences.path.parent/'client-install',sys.executable),received)

    def _destroy(self,event):
        if event.widget is self.root:
            self.closed=True
            self.cancel.set()
            self.transfer_cancel.set()
            self.generation+=1

    def reconfigure(self, options=None):
        self.options=self.app.preferences.read() if options is None else options
        self.transfer_cancel.set();self.ready=None;self.transfer={'state':'idle'}
        self.cancel.set()
        self.cancel=threading.Event()
        self.generation+=1
        self.result={'state':'not_checked' if self.options['auto_check_updates'] else 'disabled'}
        self.root.title(self.title)

    def tick(self):
        if self.closed:return
        if not getattr(self.app,'_closing',False):
            def received(options,error):
                if self.closed or error:return
                if any(options[key]!=self.options[key] for key in ('auto_check_updates','update_channel')):
                    self.reconfigure(options)
                if getattr(sys,'frozen',False) and self.options['auto_check_updates']:
                    self.request(False)
            self.app.tasks.submit('update-preferences',self.app.preferences.read,received)
        schedule(self.root,60000,self.tick)

    def request(self,manual=True):
        if self.closed or getattr(self.app,'_closing',False):return
        now=time.monotonic()
        if manual and now-self.last_manual<30:return
        generation,cancel=self.generation,self.cancel
        channel=self.options['update_channel']
        directory=self.app.preferences.path.parent
        def work():
            if cancel.is_set():return {'state':'not_checked'}
            try:
                origin=read_public_trust(runtime_trust_path())['server_origin']
            except FileNotFoundError:return {'state':'source_unconfigured'}
            except (ValueError,OSError):return {'state':'source_invalid'}
            if self.artifact_hash is None:self.artifact_hash=executable_hash()
            store=UpdateState(directory);key=store.key(origin,channel)
            cached=store.read(key);now=time.time()
            cached_display=cached_result(cached.get('result'),origin,VERSION,channel,self.artifact_hash)
            def present(result, cached_result=False):
                result=dict(result,cached=cached_result)
                if result.get('state')=='update_available' and result.get('release'):
                    target=json.dumps([origin,channel,result['release']],sort_keys=True)
                    result.update(store.reminder(key,target,self.window))
                return result
            next_check=cached.get('next_check',0)
            last_attempt=cached.get('last_attempt',0)
            if (not manual and cached.get('client_version')==VERSION
                    and cached.get('current_hash')==self.artifact_hash
                    and cached_display is not None
                    and type(last_attempt) in (int,float) and type(next_check) in (int,float)
                    and last_attempt<=now<next_check and next_check-last_attempt<=90000):
                return present(cached_display,True)
            owner=store.claim(key,30)
            if not owner:return {'state':'busy'}
            try:
                response={}
                response=check(origin,channel,cancel)
                if cancel.is_set():return {'state':'not_checked'}
                if 'envelope' in response:
                    result=decide(response['envelope'],VERSION,channel,self.artifact_hash).as_dict()
                    result.update(origin=origin,checked_at=now)
                    release=result.get('release')
                    target=json.dumps([origin,channel,release],sort_keys=True)
                    result['ignored']=cached.get('ignored')==target
                    published=store.complete(key,owner,result=result,last_attempt=now,last_success=now,
                                   next_check=now+86400,failures=0,client_version=VERSION,current_hash=self.artifact_hash)
                    return present(result) if published else {'state':'busy'}
                state='service_unavailable' if response.get('error')=='service_unavailable' else 'check_failed'
            except (ValueError,OSError,RuntimeError):
                state='check_failed'
            finally:
                # complete() releases successful claims; release uses ownership checks.
                if cancel.is_set():store.release(key,owner)
            failures=cached.get('failures',0)
            failures=failures if type(failures) is int and 0<=failures<=100 else 0
            delay=(60,300,1800,21600)[min(failures,3)]
            delay=min(21600,delay+random.randrange(max(1,int(delay*.2))))
            retry=response.get('retry_after')
            if type(retry) is int and 0<retry<=21600:
                delay=max(delay,retry)
            result={'state':state,'checked_at':now,'previous_success':cached.get('last_success')}
            published=store.complete(key,owner,result=result,last_attempt=now,next_check=now+delay,
                                    failures=failures+1,client_version=VERSION,current_hash=self.artifact_hash)
            return result if published else {'state':'busy'}
        def done(result,error):
            if self.closed or generation!=self.generation or cancel.is_set():return
            self.result=result if not error else {'state':'check_failed'}
            release=self.result.get('release')
            suffix=(' · 有新版本 '+release['version']) if release and self.result['state']=='update_available' and self.result.get('remind') else ''
            self.root.title(self.title+suffix)
        if self.app.tasks.submit('client-update',work,done):
            if manual:self.last_manual=now
            self.result={'state':'checking'}

    def ignore(self):
        value=dict(self.result)
        if not value.get('release') or value.get('state')!='update_available':return
        channel=value['release']['channel'];origin=value['origin']
        target=json.dumps([origin,channel,value['release']],sort_keys=True)
        def work():
            store=UpdateState(self.app.preferences.path.parent)
            store.merge(store.key(origin,channel),ignored=target)
        def done(_,error):
            if not error and not self.closed and self.result==value:
                self.result=dict(value,ignored=True)
                self.root.title(self.title)
        self.app.tasks.submit('ignore-client-update',work,done)

    def later(self):
        value=dict(self.result)
        if not value.get('release') or value.get('state')!='update_available':return
        channel=value['release']['channel'];origin=value['origin']
        target=json.dumps([origin,channel,value['release']],sort_keys=True)
        def work():
            store=UpdateState(self.app.preferences.path.parent);now=time.time()
            store.merge(store.key(origin,channel),reminder_target=target,reminder_at=now,
                        reminder_until=now+3600,reminder_window=None)
        def done(_,error):
            if not error and not self.closed and self.result==value:
                self.result=dict(value,remind=False)
                self.root.title(self.title)
        self.app.tasks.submit('snooze-client-update',work,done)

    def website(self):
        try:origin=read_public_trust(runtime_trust_path())['server_origin']
        except (ValueError,OSError):return
        from client_update import public_origin
        webbrowser.open(public_origin(origin)+'/tools/ai-director')

    def transfer_tick(self):
        if self.closed:return
        if getattr(self.app,'_closing',False):self.transfer_cancel.set();self.cancel.set()
        for _ in range(50):
            try:generation,value=self.transfer_events.get_nowait()
            except queue.Empty:break
            if generation==self.generation and self.transfer['state']=='downloading' and not self.transfer_cancel.is_set():
                self.transfer={'state':'downloading',**value}
        schedule(self.root,200,self.transfer_tick)

    def shutdown(self):
        self.cancel.set();self.transfer_cancel.set()
        self.app.tasks.wait_workers(('client-update','update-download','update-prepare'),20)

    def download(self):
        if self.closed or getattr(self.app,'_closing',False):return
        from client_update_download import download,runtime_trust_path
        cancel=threading.Event();generation=self.generation
        channel=self.options['update_channel'];root=self.app.preferences.path.parent/'client-install'
        def work():
            return download(root,runtime_trust_path(),channel,VERSION,cancel,
                lambda value:self.transfer_events.put((generation,value)))
        def done(value,error):
            if self.closed or generation!=self.generation:return
            if cancel.is_set():self.transfer={'state':'cancelled'};self.ready=None;return
            self.transfer=value if not error else {'state':'failed','error':'transfer_unavailable'}
            self.ready=value if not error and value.get('state')=='ready' else None
        if self.app.tasks.submit('update-download',work,done):
            self.transfer_cancel=cancel;self.transfer={'state':'downloading'};self.ready=None

    def cancel_download(self):
        self.transfer_cancel.set();self.transfer={'state':'cancelled'};self.ready=None

    def apply_update(self):
        from tkinter import messagebox
        if not getattr(sys,'frozen',False):
            messagebox.showinfo('使用发行程序','源码运行不替换文件，请使用正式打包的程序。',parent=self.root);return
        if self.closed or getattr(self.app,'_closing',False) or not self.ready:return
        if any(getattr(self.app,key,False) for key in ('_training','_calibrating','_submitting')):
            messagebox.showinfo('任务运行中','请等待训练、校准或结果提交完成后再更新。',parent=self.root);return
        if not messagebox.askyesno('退出并更新','将停止导播、保存记录并关闭此窗口。请先关闭其他导播和训练器窗口。更新失败将保留原程序备份，账号与采集数据保持。现在继续？',parent=self.root):return
        from client_update_install import prepare
        generation=self.generation;ready=self.ready;root=self.app.preferences.path.parent/'client-install'
        def done(path,error):
            if self.closed or getattr(self.app,'_closing',False) or generation!=self.generation:return
            if error:
                self.transfer={'state':'failed','error':'install_preparation_failed'};return
            if any(getattr(self.app,key,False) for key in ('_training','_calibrating','_submitting')):
                self.transfer={'state':'failed','error':'training_started'};return
            self.app._update_job=path
            self.app._on_close()
        role='trainer' if '--trainer' in getattr(self.app,'_launch_arguments',sys.argv[1:]) or self.app.__class__.__name__=='TrainerApp' else 'director'
        self.app.tasks.submit('update-prepare',lambda:prepare(root,ready,sys.executable,role=role),done)

    def recover_update(self):
        if not getattr(sys,'frozen',False):return
        from client_update_install import pending_job
        from tkinter import messagebox
        def done(path,error):
            if self.closed:return
            if error or path is None:
                messagebox.showinfo('更新恢复','没有需要恢复的中断更新。',parent=self.root);return
            if messagebox.askyesno('恢复旧程序','将保存记录并退出此窗口，恢复本次更新保留的旧程序文件。请先关闭其他程序窗口。是否继续？',parent=self.root):
                self.app._update_job=path;self.app._update_recovery=True;self.app._on_close()
        self.app.tasks.submit('update-recovery',lambda:pending_job(self.app.preferences.path.parent/'client-install',sys.executable),done)

    def text(self):
        value=self.result
        lines=[f'当前版本：{VERSION} · 通道：{self.options["update_channel"]}',MESSAGES.get(value['state'],'检查未完成')]
        if value.get('checked_at'):lines.append('检查时间：'+time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(value['checked_at'])))
        if value.get('cached'):lines.append('显示缓存结果')
        if value.get('ignored'):lines.append('已忽略本次发行的自动提醒')
        release=value.get('release')
        if release:lines.extend([f'发行版本：{release["version"]} · {release["artifact_bytes"]/1024/1024:.1f} MiB',release['notes']])
        transfer=self.transfer
        states={'idle':'尚未下载','downloading':'正在获取并验证签名更新','ready':'更新包已验证，可选择退出并安装',
                'cancelled':'下载已取消，已保存的部分可续传','failed':'下载或安装准备未完成；请检查更新签名配置、网络、磁盘和当前发行'}
        lines.append('安装更新：'+states.get(transfer['state'],'处理中'))
        reasons={'update_trust_or_file_missing':'此发行未配置应用更新公钥，或所需文件已丢失。可先前往官网下载。',
                 'transfer_timeout':'下载连接长时间无进展，稍后重试可续传。',
                 'update_not_newer':'所选通道没有更高版本。',
                 'release_changed':'发行已变更或撤回，请重新检查并下载。',
                 'package_integrity':'更新包摘要或大小不符，请重新下载。',
                 'major_upgrade_requires_manual_install':'跨主版本升级需从官网手动安装。'}
        if transfer.get('error') in reasons:lines.append(reasons[transfer['error']])
        if transfer.get('total'):lines.append(f'已下载 {transfer.get("bytes",0)/1048576:.1f} / {transfer["total"]/1048576:.1f} MiB')
        previous={'complete':'上次更新已完成','rolled_back':'上次更新未通过启动检查，已恢复旧程序',
                  'rollback_pending':'上次更新等待恢复，请先关闭其他窗口再选择恢复中断更新',
                  'failed':'上次更新未完成，请检查窗口占用、磁盘和网络；中断时可选择恢复'}
        if self.install_result in previous:lines.append(previous[self.install_result])
        return '\n'.join(lines)


def install(app):
    app.updates=Updates(app)


def mount(parent,app):
    if not hasattr(app,'updates'):return
    import tkinter as tk
    box=ttk.LabelFrame(parent,text='程序更新',padding=10);box.pack(fill='x',pady=10)
    enabled=tk.BooleanVar(value=app.updates.options['auto_check_updates'])
    channel=tk.StringVar(value=app.updates.options['update_channel'])
    def apply():
        app.preferences.update(auto_check_updates=enabled.get(),update_channel=channel.get())
        app.updates.reconfigure()
    ttk.Checkbutton(box,text='启动后自动检查更新（不自动安装）',variable=enabled,command=apply).pack(anchor='w')
    selector=ttk.Combobox(box,textvariable=channel,values=('stable','beta'),state='readonly',width=12)
    selector.pack(anchor='w');selector.bind('<<ComboboxSelected>>',lambda _:apply())
    from desktop_ui import ThemedScrolledText
    details=ThemedScrolledText(box,height=6,wrap='word',state='disabled');details.pack(fill='x')
    actions=ttk.Frame(box);actions.pack(fill='x')
    buttons=[('检查更新',app.updates.request),('前往官网下载',app.updates.website),('一小时后提醒',app.updates.later),('忽略此版本',app.updates.ignore),
             ('下载签名更新',app.updates.download),('取消下载',app.updates.cancel_download),('退出并安装',app.updates.apply_update),('恢复中断更新',app.updates.recover_update)]
    for index,(label,command) in enumerate(buttons):
        ttk.Button(actions,text=label,command=command).grid(row=index//2,column=index%2,sticky='w',padx=3,pady=5)
    def refresh():
        content=app.updates.text()
        if details.get('1.0','end-1c')!=content:
            position=details.yview()[0];details.configure(state='normal');details.delete('1.0','end');details.insert('end',content)
            details.yview_moveto(position);details.configure(state='disabled')
        schedule(box,500,refresh)
    refresh()
