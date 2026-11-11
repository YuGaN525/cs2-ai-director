"""Manual preview/confirm and visible owner-scoped diagnostics queue."""
import json
import tkinter as tk
from tkinter import ttk, messagebox
from diagnostics_privacy import sanitize_log


def mount(parent, app):
    box = ttk.LabelFrame(parent, text='日志上传与故障排查', padding=10)
    box.pack(fill='x', pady=10)
    runtime = getattr(app, 'diagnostics', None)
    if runtime is None:
        ttk.Label(box, text='诊断服务暂不可用；本地导播功能不受影响。').pack(anchor='w')
        return
    enabled = tk.BooleanVar(value=runtime.preferences.read()['auto_report_diagnostics'])
    status = tk.StringVar(value=runtime.last_status)
    def toggle():
        try: runtime.set_enabled(enabled.get())
        except Exception:
            messagebox.showerror('设置未保存', '本机存储暂不可用，请稍后重试。', parent=box)
    ttk.Checkbutton(box, text='自动上报严重错误与异常退出', variable=enabled, command=toggle).pack(anchor='w')
    ttk.Label(box, text='仅上传本程序的脱敏事件、异常类型和代码位置，不包含聊天、按键文本、路径或凭据。服务器保留 30 天，仅授权管理员可查。关闭自动上报后停止自动采集与补传，仍可手动提交。', wraplength=660, justify='left').pack(anchor='w', pady=6)
    ttk.Label(box, textvariable=status, wraplength=660).pack(anchor='w', pady=4)
    from desktop_ui import schedule
    def refresh():
        if not box.winfo_exists(): return
        status.set(runtime.last_status)
        enabled.set(runtime.enabled)
        schedule(box, 1000, refresh)
    schedule(box, 1000, refresh)

    def preview(log, owner, ident=None):
        top = tk.Toplevel(app.root)
        top.title('日志上传 · 脱敏预览')
        from desktop_ui import fit_window, theme, ThemedScrolledText
        fit_window(top, 780, 650)
        prefs = app.preferences.read(); theme(top, prefs['theme'], prefs['font_size'])
        ttk.Label(top, text='请核对以下全部上报内容。仅当前账号可提交；未登录时先保存在本机。', wraplength=740).pack(anchor='w', padx=16, pady=12)
        text = ThemedScrolledText(top, wrap='word', height=22)
        text.pack(fill='both', expand=True, padx=16)
        text.insert('1.0', log); text.configure(state='disabled')
        def confirm():
            def work():
                if ident: runtime.confirm_existing(ident, owner); return ident
                return runtime.submit_manual(log, owner)
            def done(result, error):
                if error:
                    messagebox.showerror('暂未提交', '账号状态、队列或本机存储已变化，请重新预览后重试。', parent=top)
                    return
                status.set('已加入当前账号上传队列' if owner else '已保存在本机；登录后请在待传记录中预览确认')
                top.destroy()
            app.tasks.submit('diagnostics-manual', work, done)
        ttk.Button(top, text='确认并提交' if owner else '保存到本机', command=confirm).pack(side='right', padx=16, pady=12)
        ttk.Button(top, text='取消', command=top.destroy).pack(side='right', pady=12)
        theme(top, prefs['theme'], prefs['font_size'])

    def create_preview():
        owner = runtime.owner()
        def done(log, error):
            if error: status.set('本机日志暂不可读')
            else: preview(log, owner)
        app.tasks.submit('diagnostics-preview', runtime.preview, done)

    def show_queue():
        owner = runtime.owner()
        def done(rows, error):
            if error: status.set('本机队列暂不可读'); return
            top = tk.Toplevel(app.root); top.title('日志上传 · 待传与回执')
            from desktop_ui import fit_window, theme
            fit_window(top, 760, 480)
            labels = {'pending': '等待重试', 'sending': '正在提交', 'received': '已接收', 'rejected': '停止重试'}
            content = ttk.Frame(top); content.pack(fill='both', expand=True, padx=12, pady=12)
            listing = tk.Listbox(content, height=12)
            scrollbar = ttk.Scrollbar(content, orient='vertical', command=listing.yview)
            scrollbar.pack(side='right', fill='y')
            listing.configure(yscrollcommand=scrollbar.set)
            listing.pack(side='left', fill='both', expand=True)
            for row in rows:
                label = labels.get(row['state'], row['state']) if row['owner'] else '未绑定账号，需确认'
                listing.insert('end', f"{json.loads(row['payload'])['occurredAt']}  {row['kind']}  {label}")
            ttk.Label(top, text='本地最多保留 20 份、30 天。其他账号记录不会在这里显示或发送。', wraplength=720).pack(anchor='w', padx=12)
            def select():
                selected = listing.curselection()
                if not selected: return
                row = rows[selected[0]]
                if row['state'] == 'received':
                    receipt = json.loads(row['receipt'])
                    messagebox.showinfo('服务器回执', f"报告：{receipt['id']}\n接收：{receipt['receivedAt']}\n到期：{receipt['expiresAt']}", parent=top)
                elif row['state'] == 'pending':
                    preview(sanitize_log(json.loads(row['payload'])['log']), owner, row['id'])
                else:
                    messagebox.showinfo('日志状态', labels.get(row['state'], '暂不可重试')+'；请稍后刷新。', parent=top)
            ttk.Button(top, text='预览重试 / 查看回执', command=select).pack(pady=10)
            prefs = app.preferences.read(); theme(top, prefs['theme'], prefs['font_size'])
        app.tasks.submit('diagnostics-list', lambda: runtime.store.rows(owner, include_unbound=True), done)

    actions = ttk.Frame(box); actions.pack(fill='x', pady=5)
    ttk.Button(actions, text='日志上传：预览并提交', command=create_preview).pack(side='left')
    ttk.Button(actions, text='待传记录与回执', command=show_queue).pack(side='left', padx=8)
