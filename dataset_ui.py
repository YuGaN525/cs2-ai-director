"""Main-thread dataset queue controls; disk/network work stays in background tasks."""
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
import uuid

from dataset_runtime import describe
from upload_outbox import Binding
import dataset_protocol as p


def open_datasets(app):
    from desktop_ui import schedule,theme,text_dialog,ScrollableFrame
    runtime=app.datasets;top=tk.Toplevel(app.root);top.title('训练数据 · 上传与筛选状态');top.geometry('960x620')
    prefs=app.preferences.read();theme(top,prefs['theme'],prefs['font_size'])
    page=ScrollableFrame(top);page.pack(fill='both',expand=True)
    frame=ttk.Frame(page.inner,padding=16);frame.pack(fill='both',expand=True)
    intro=ttk.Label(frame,text='训练完成后自动上传；所有数据仍须管理员筛选纳入。纳入后等待管理员主动训练。',wraplength=880);intro.pack(anchor='w')
    columns=('rows','source','status');table_frame=ttk.Frame(frame);table_frame.pack(fill='both',expand=True,pady=12)
    tree=ttk.Treeview(table_frame,columns=columns,show='tree headings',height=14,selectmode='browse')
    tree.heading('#0',text='本地训练 / 补传编号');tree.column('#0',width=255,minwidth=180)
    for col,title,width in [('rows','样本',75),('source','来源',125),('status','接收与筛选状态',420)]:tree.heading(col,text=title);tree.column(col,width=width,minwidth=60)
    y=ttk.Scrollbar(table_frame,orient='vertical',command=tree.yview);x=ttk.Scrollbar(table_frame,orient='horizontal',command=tree.xview)
    tree.configure(yscrollcommand=y.set,xscrollcommand=x.set)
    table_frame.rowconfigure(0,weight=1);table_frame.columnconfigure(0,weight=1)
    tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew')
    rows={};view_actor=[None];include_local=tk.BooleanVar(value=False);counter=[0]
    status=ttk.Label(frame,text='',style='Muted.TLabel',wraplength=880);status.pack(anchor='w',fill='x')
    frame.bind('<Configure>',lambda event:(intro.configure(wraplength=max(220,event.width-32)),status.configure(wraplength=max(220,event.width-32))))
    local=Binding('unbound','local',app.account.local_device)

    def result_done(value,error):
        if not top.winfo_exists():return
        if error:messagebox.showerror('操作未完成','数据已保留。'+str(error),parent=top)
        else:runtime.wake.set();refresh()

    def selected():
        items=tree.selection()
        if not items:return None
        row=rows.get(items[0]);current=app.account.binding()
        if row is None or view_actor[0]!=current:
            messagebox.showwarning('账号已变化','请刷新后再操作。',parent=top);return None
        return row

    def change(paused):
        row=selected()
        if row:
            binding=Binding(row['account'],row['origin'],row['device'])
            app.tasks.submit('dataset-change-'+row['id'],lambda:runtime.store.set_paused(row['id'],binding,True) if paused else runtime.store.retry(row['id'],binding),result_done)

    def details():
        row=selected()
        if row is None:return
        data=json.loads(row['descriptor']) if row['descriptor'] else None
        body=['本地编号：'+row['id'],'状态：'+describe(row)]
        if data:
            body+=['提交编号：'+data['clientDatasetId'],'样本数：'+str(data['manifest']['totals']['rows']),'清单摘要：'+data['manifestSha256']]
            for s in data['manifest']['sources']:body.append(f"来源 {s['sourceId']} · {s['rows']} 条 · {s['kind']} · {s['identityQuality']}")
        if row['receipt']:body+=['','接收回执（接收不代表纳入）：',json.dumps(json.loads(row['receipt']),ensure_ascii=False,indent=2)]
        if row['governance']:body+=['','当前筛选状态：',json.dumps(json.loads(row['governance']),ensure_ascii=False,indent=2)]
        text_dialog(top,'训练数据详情','\n'.join(body))

    def bind_local():
        row=selected();identity=app.account.upload_identity()
        if row is None:return
        if row['origin']!='local' or identity is None:
            messagebox.showinfo('关联本地数据','请登录，并选择一条尚未绑定账号的本地记录。',parent=top);return
        if not messagebox.askyesno('关联到当前账号','这批数据将以当前登录账号上传，仍需管理员筛选。确认关联？',parent=top):return
        def work():
            current=app.account.upload_identity()
            if current is None or current.binding!=identity.binding:raise ValueError('账号已变化')
            runtime.store.bind_local(row['id'],local,current.binding)
        app.tasks.submit('dataset-bind-'+row['id'],work,result_done)

    def cancel():
        row=selected()
        if row is None or not row['descriptor'] or row['receipt']:return
        if not messagebox.askyesno('取消本次上传','取消服务器上传会话，本地原件仍保留。是否继续？',parent=top):return
        binding=Binding(row['account'],row['origin'],row['device'])
        app.tasks.submit('dataset-cancel-'+row['id'],lambda:runtime.cancel(row['id'],binding),result_done)

    def renew():
        row=selected()
        if row is None:return
        if not (row['state']=='cancelled' or row['error']=='DIRECTOR_DATASET_EXPIRED'):
            messagebox.showinfo('重新发起上传','仅已取消或已过期会话可重新发起，其余情况使用“重试 / 继续”。',parent=top);return
        if not messagebox.askyesno('重新发起上传','以新提交编号重新上传原样本，旧记录保留；服务器仍执行去重与管理员筛选。是否继续？',parent=top):return
        binding=Binding(row['account'],row['origin'],row['device'])
        app.tasks.submit('dataset-renew-'+row['id'],lambda:runtime.store.renew(row['id'],binding),result_done)

    def historical(kind):
        if kind=='csv':chosen=filedialog.askopenfilename(parent=top,title='选择历史训练CSV',filetypes=[('训练样本','*.csv')])
        else:chosen=filedialog.askdirectory(parent=top,title='选择完整样本包目录' if kind=='bundle' else '选择含spec.json的历史派生目录')
        if not chosen:return
        binding=app.account.binding();preview_id=str(uuid.uuid4());destination=runtime.store.root/'previews'/preview_id
        status.configure(text='正在校验历史样本并生成脱敏预览…')
        def work():
            runtime.store.check_space(p.LIMITS['maxExpandedBytes']+p.LIMITS['maxCompressedBytes'])
            if kind=='bundle':folder=Path(chosen);data=p.validate_directory(folder)
            elif kind=='csv':
                from dataset_export import export_csv
                folder=destination;data=export_csv(chosen,folder,preview_id)
            else:
                from dataset_history import export_derived
                folder=destination;data=export_derived(chosen,folder,preview_id)
            return folder,data
        def preview(value,error):
            if not top.winfo_exists():return
            if error:
                status.configure(text='历史数据未提交；模型文件不能还原完整训练样本。')
                messagebox.showerror('无法生成样本包','请保留原件并检查完整CSV/派生数据。'+str(error),parent=top);return
            if app.account.binding()!=binding:status.configure(text='账号已变化，预览保留在本机，未提交。');return
            folder,data=value;t=data['manifest']['totals'];legacy=any(s['kind']!='demo-derived' for s in data['manifest']['sources'])
            body=f"{t['sources']} 个来源，{t['rows']} 条样本，压缩约 {t['compressedBytes']/1048576:.2f} MB。\n"+('含旧格式或来源未知样本，管理员补核后才能考虑纳入。\n' if legacy else '比赛归属仍需管理员核实。\n')+'将以当前账号进入上传队列；服务器接收不等于纳入训练库。是否继续？'
            if messagebox.askyesno('历史数据补传预览',body,parent=top):
                def enqueue():
                    if app.account.binding()!=binding:raise ValueError('账号已变化')
                    return runtime.store.import_bundle(folder,binding)
                app.tasks.submit('dataset-import-'+preview_id,enqueue,result_done)
            else:status.configure(text='预览保留在本机，未加入上传队列。')
        app.tasks.submit('dataset-preview-'+preview_id,work,preview)

    actions=ttk.Frame(frame);actions.pack(fill='x',pady=8)
    for index,(text,fn) in enumerate([('查看详情',details),('暂停上传',lambda:change(True)),('重试 / 继续',lambda:change(False)),('取消上传',cancel),('重新发起',renew),('关联到当前账号',bind_local)]):ttk.Button(actions,text=text,command=fn).grid(row=index//3,column=index%3,sticky='w',padx=(0,6),pady=3)
    imports=ttk.Frame(frame);imports.pack(fill='x')
    for text,kind in [('补传历史CSV…','csv'),('导入完整样本包…','bundle'),('补传历史派生数据…','derived')]:ttk.Button(imports,text=text,command=lambda k=kind:historical(k)).pack(anchor='w',pady=3)
    ttk.Checkbutton(frame,text='显示本机尚未绑定账号的数据（需手动关联）',variable=include_local,command=lambda:refresh()).pack(anchor='w',pady=8)

    def refresh():
        if not top.winfo_exists() or app._closing:return
        binding=app.account.binding();show_local=include_local.get();counter[0]+=1;generation=counter[0]
        def read():
            value=runtime.store.list(binding)
            if show_local and binding!=local:value+=runtime.store.list(local)
            return value
        def done(value,error):
            if not top.winfo_exists() or generation!=counter[0] or binding!=app.account.binding():return
            if error:status.configure(text='本地数据读取失败，记录未删除。');return
            selection=tree.selection();rows.clear();rows.update({r['id']:r for r in value});view_actor[0]=binding
            for item in tree.get_children():
                if item not in rows:tree.delete(item)
            for row in value:
                descriptor=json.loads(row['descriptor']) if row['descriptor'] else None
                count=descriptor['manifest']['totals']['rows'] if descriptor else '—'
                source='本地未绑定' if row['origin']=='local' else '当前账号'
                description=describe(row)
                if row['state']=='uploading' and row['id'] in runtime.progress:
                    done,total=runtime.progress[row['id']]
                    description+=f' · {done/total:.0%}' if total else ''
                values=(count,source,description);label=row['id'][:13]+'…'
                if tree.exists(row['id']):tree.item(row['id'],text=label,values=values)
                else:tree.insert('',tk.END,iid=row['id'],text=label,values=values)
            if selection and tree.exists(selection[0]):tree.selection_set(selection[0])
            status.configure(text=runtime.status+' · 已接收不代表管理员已纳入。')
        app.tasks.submit('dataset-list-'+str(id(top)),read,done)
    def poll():
        if top.winfo_exists():refresh();schedule(top,2000,poll)
    poll()
    return top
