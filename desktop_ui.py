"""Shared desktop widgets; no policy or authorization decisions live here."""
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import time
import queue
import threading

PALETTES = {
    'dark': dict(bg='#090909', card='#151512', text='#f2f0e9', muted='#b7bdc8', accent='#d4af37', field='#1b1b17', border='#35352e', success='#80d69e', error='#ff9696'),
    'light': dict(bg='#f5f3ed', card='#ffffff', text='#242319', muted='#55564e', accent='#85620e', field='#faf8f2', border='#d8d6ce', success='#226c38', error='#ad2234'),
}


def schedule(widget, delay, callback):
    """Cancel only this widget's timers when it is destroyed."""
    if getattr(widget, '_timers_disposed', False):
        return None
    if threading.current_thread() is not threading.main_thread():
        widget._posted_callbacks.put((delay, callback))
        return None
    if not hasattr(widget, '_owned_timers'):
        widget._owned_timers = set()
        widget._posted_callbacks = queue.SimpleQueue()
        def pump():
            for _ in range(32):
                try:
                    wait, fn = widget._posted_callbacks.get_nowait()
                except queue.Empty:
                    break
                schedule(widget, wait, fn)
            widget._post_pump = widget.after(40, pump)
        widget._post_pump = widget.after(40, pump)
        def cleanup(event):
            if event.widget is widget:
                widget._timers_disposed = True
                widget.after_cancel(widget._post_pump)
                for token in tuple(widget._owned_timers):
                    widget.after_cancel(token)
                widget._owned_timers.clear()
        widget.bind('<Destroy>', cleanup, add='+')
    def run():
        widget._owned_timers.discard(token)
        callback()
    token = widget.after(delay, run)
    widget._owned_timers.add(token)
    return token


def theme(root, mode, font_size=None):
    p = PALETTES[mode]
    accent_foreground = '#ffffff' if mode == 'light' else '#171207'
    font_size = font_size or getattr(root, '_font_size', 10)
    root._font_size = font_size
    root._theme_mode = mode
    owner = root._root()
    if getattr(owner, '_shared_theme_key', None) != (mode, font_size):
        style = ttk.Style(root)
        if style.theme_use() != 'clam':
            style.theme_use('clam')
        style.configure('.', background=p['card'], foreground=p['text'], font=('Microsoft YaHei UI', font_size))
        for name in ('TFrame', 'TLabel', 'TLabelframe', 'TCheckbutton'):
            style.configure(name, background=p['card'], foreground=p['text'])
        style.map('TCheckbutton', background=[('active', p['card'])], foreground=[('disabled', p['muted']), ('active', p['text'])])
        style.configure('TLabelframe', bordercolor=p['border'], lightcolor=p['border'], darkcolor=p['border'], relief='solid', borderwidth=1)
        style.configure('TLabelframe.Label', background=p['card'], foreground=p['accent'])
        style.configure('TButton', background=p['field'], foreground=p['text'], padding=(10, 6))
        style.map('TButton', background=[('active', p['accent'])], foreground=[('active', accent_foreground), ('disabled', p['muted'])])
        for name in ('Card.TFrame', 'Header.TFrame', 'Footer.TFrame'):
            style.configure(name, background=p['card'])
        for name in ('Title.TLabel', 'Subtitle.TLabel', 'Header.TLabel', 'Muted.TLabel'):
            style.configure(name, background=p['card'], foreground=p['text'])
        style.configure('Accent.TButton', background=p['accent'], foreground=accent_foreground)
        style.configure('Stop.TButton', background=p['field'], foreground=p['text'])
        style.configure('TNotebook', background=p['bg'])
        style.configure('TNotebook.Tab', background=p['field'], foreground=p['muted'], padding=(12, 7))
        style.map('TNotebook.Tab', foreground=[('selected', p['accent']), ('active', p['text'])], background=[('selected', p['card']), ('active', p['field'])])
        for name in ('TEntry', 'TCombobox', 'Treeview'):
            style.configure(name, fieldbackground=p['field'], background=p['field'], foreground=p['text'])
        for name in ('TButton', 'TEntry', 'TCombobox', 'TNotebook.Tab', 'Treeview'):
            style.configure(name, bordercolor=p['border'], lightcolor=p['border'], darkcolor=p['border'])
        style.map('TCombobox', fieldbackground=[('readonly', p['field'])], foreground=[('readonly', p['text'])], selectbackground=[('readonly', p['field'])], selectforeground=[('readonly', p['text'])])
        style.map('Accent.TButton', background=[('disabled', p['field']), ('active', p['accent'])], foreground=[('disabled', p['muted'])])
        style.map('Stop.TButton', background=[('disabled', p['field']), ('active', p['accent'])], foreground=[('disabled', p['muted'])])
        style.configure('Vertical.TScrollbar', background=p['field'], troughcolor=p['card'], bordercolor=p['border'], arrowcolor=p['text'])
        style.configure('Treeview.Heading', background=p['field'], foreground=p['accent'])
        style.map('Treeview', background=[('selected', p['accent'])], foreground=[('selected', accent_foreground)])
        root.option_add('*TCombobox*Listbox.background', p['field'])
        root.option_add('*TCombobox*Listbox.foreground', p['text'])
        owner._shared_theme_key = (mode, font_size)
    root.configure(bg=p['bg'])
    def visit(widget):
        if getattr(widget, '_applied_font_size', 10) != font_size and 'font' in widget.keys() and str(widget.cget('font')):
            from tkinter import font as tkfont
            if not hasattr(widget, '_base_font'):
                widget._base_font = tkfont.Font(root=root, font=widget.cget('font')).actual()
            base = widget._base_font
            size = round(base['size'] * font_size / 10)
            widget.configure(font=(base['family'], size, base['weight'], base['slant']))
            widget._applied_font_size = font_size
        if getattr(widget, '_applied_palette', None) != mode:
            # Existing legacy labels have inline colors; use consistent readable text.
            if isinstance(widget, ttk.Label) and str(widget.cget('foreground')):
                widget.configure(foreground=p['text'])
            elif isinstance(widget, (tk.Text, tk.Listbox)):
                widget.configure(bg=p['field'], fg=p['text'], insertbackground=p['text']) if isinstance(widget, tk.Text) else widget.configure(bg=p['field'], fg=p['text'])
            if isinstance(widget, tk.Text):
                for tag, color in (('INFO', p['text']), ('WARN', p['accent']), ('ERROR', p['error'])):
                    if tag in widget.tag_names():
                        widget.tag_configure(tag, foreground=color)
            if isinstance(widget, tk.Canvas):
                widget.configure(bg=p['card'])
            elif isinstance(widget, tk.Checkbutton):
                widget.configure(bg=p['card'], fg=p['text'], selectcolor=p['field'], activebackground=p['card'], activeforeground=p['text'])
            widget._applied_palette = mode
        for child in widget.winfo_children():
            visit(child)
    visit(root)


def fit_window(root, width=1060, height=850):
    available_width, available_height = root.winfo_screenwidth(), root.winfo_screenheight()
    # Work area excludes the taskbar; no system DPI setting is changed.
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
            available_width, available_height = rect.right-rect.left, rect.bottom-rect.top
    except (AttributeError, OSError):
        pass
    width = min(width, max(480, available_width-40))
    height = min(height, max(320, available_height-60))
    root.minsize(min(640, width), min(420, height))
    root.geometry(f'{width}x{height}')


def restore_window(app, kind, width, height):
    values = app.preferences.read()
    saved = values.get(kind+'_size') if values['remember_window'] else ''
    if saved:
        width, height = map(int, saved.split('x'))
    fit_window(app.root, width, height)


def window_preferences(app, kind):
    # Capture on the GUI thread; persist from the close worker.
    import re
    if not getattr(app, '_ui_preferences', {}).get('remember_window', False):
        return {}
    geometry = str(app.root.geometry()).split('+')[0]
    match = re.match(r'([0-9]+x[0-9]+)', geometry)
    return {kind+'_size': match[1]} if match else {}


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, padding=10)
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor='nw')
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.scrollbar.pack(side='right', fill='y')
        self.inner.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self.resize)
        self.remainder = 0
        self.top = self.winfo_toplevel()
        self.binding = self.top.bind('<MouseWheel>', self.wheel, add='+')
        self.focus_binding = self.top.bind('<FocusIn>', self.ensure_focus_visible, add='+')
        self.page_up = self.top.bind('<Prior>', lambda e: self.page(e, -1), add='+')
        self.page_down = self.top.bind('<Next>', lambda e: self.page(e, 1), add='+')
        self.bind('<Destroy>', self.cleanup, add='+')
        self.canvas.bind('<Prior>', lambda e: self.canvas.yview_scroll(-1, 'pages'))
        self.canvas.bind('<Next>', lambda e: self.canvas.yview_scroll(1, 'pages'))

    def resize(self, event):
        if getattr(self, '_last_wrap_width', None) == event.width:
            return
        self._last_wrap_width = event.width
        self.canvas.itemconfigure(self.window, width=event.width)
        def wrap(widget):
            if isinstance(widget, ttk.Label) and int(float(widget.cget('wraplength') or 0)):
                widget.configure(wraplength=max(180, event.width-60))
            for child in widget.winfo_children():
                wrap(child)
        wrap(self.inner)

    def wheel(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        cursor = widget
        while cursor is not None and cursor is not self:
            if isinstance(cursor, ScrollableFrame):
                return  # The innermost scroll container owns this event.
            if isinstance(cursor, (tk.Text, tk.Listbox, ttk.Treeview)):
                first, last = cursor.yview()
                if not ((event.delta > 0 and first <= 0) or (event.delta < 0 and last >= 1)):
                    return
            cursor = getattr(cursor, 'master', None)
        if cursor is not self or not self.winfo_ismapped():
            return
        self.remainder += event.delta
        units = int(self.remainder / 120)
        self.remainder -= units * 120
        if units and self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(-units, 'units')
        return 'break'

    def owns(self, widget):
        while widget is not None and widget is not self:
            if isinstance(widget, ScrollableFrame):
                return False
            widget = getattr(widget, 'master', None)
        return widget is self and self.winfo_ismapped()

    def page(self, event, direction):
        if self.owns(event.widget) and not isinstance(event.widget, (tk.Text, tk.Listbox, ttk.Treeview)):
            self.canvas.yview_scroll(direction, 'pages')
            return 'break'

    def ensure_focus_visible(self, event):
        # Tk sends FocusIn to ancestors too. Scrolling a tall ancestor during
        # button-down moves the target before release and can swallow a click.
        if isinstance(event.widget, (tk.Frame, ttk.Frame, tk.LabelFrame, ttk.LabelFrame, tk.Canvas)):
            return
        if not self.owns(event.widget):
            return
        y = event.widget.winfo_rooty()-self.inner.winfo_rooty()
        height = event.widget.winfo_height()
        top = self.canvas.canvasy(0)
        visible = self.canvas.winfo_height()
        total = max(1, self.inner.winfo_height())
        if y < top:
            self.canvas.yview_moveto(max(0, y-8)/total)
        elif y+height > top+visible:
            self.canvas.yview_moveto(max(0, y+height-visible+8)/total)

    def cleanup(self, event):
        if event.widget is self and self.binding:
            for sequence, token in (('<MouseWheel>', self.binding), ('<FocusIn>', self.focus_binding), ('<Prior>', self.page_up), ('<Next>', self.page_down)):
                self.top.unbind(sequence, token)
            self.binding = None


def append_log(widget, text, maximum=1000, follow_enabled=True):
    follow = follow_enabled and widget.yview()[1] >= .999
    widget.configure(state='normal')
    widget.insert('end', text.rstrip()+'\n')
    count = int(widget.index('end-1c').split('.')[0])
    if count > maximum:
        widget.delete('1.0', f'{count-maximum+1}.0')
    if follow:
        widget.see('end')
    widget.configure(state='disabled')


def settings(app):
    existing = getattr(app, '_settings_window', None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    top = tk.Toplevel(app.root)
    app._settings_window = top
    top.title('设置 · 鱼籽杯 AI 导播')
    fit_window(top, 780, 720)
    page = ScrollableFrame(top)
    page.pack(fill='both', expand=True)
    frame = page.inner
    values = app.preferences.read()
    developer = tk.BooleanVar(value=values['developer_mode'])
    theme_names = {'黑金': 'dark', '浅色': 'light'}
    selected = tk.StringVar(value='黑金' if values['theme']=='dark' else '浅色')
    follow = tk.BooleanVar(value=values['follow_logs'])
    size = tk.IntVar(value=values['font_size'])
    remember = tk.BooleanVar(value=values['remember_window'])
    ttk.Label(frame, text='外观与开发工具', font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w')
    ttk.Combobox(frame, values=tuple(theme_names), textvariable=selected, state='readonly').pack(anchor='w', pady=8)
    ttk.Checkbutton(frame, text='开启开发者模式（显示参数与诊断工具）', variable=developer).pack(anchor='w')
    ttk.Label(frame, text='模式切换只改变界面，不改变授权、模型或正在运行的任务。', wraplength=660).pack(anchor='w', pady=8)
    ttk.Checkbutton(frame, text='日志位于底部时跟随最新消息', variable=follow).pack(anchor='w', pady=6)
    ttk.Label(frame, text='正文字号（pt）').pack(anchor='w')
    ttk.Combobox(frame, values=tuple(range(9, 15)), textvariable=size, state='readonly', width=8).pack(anchor='w', pady=6)
    ttk.Checkbutton(frame, text='记住导播与训练窗口尺寸（下次启动生效）', variable=remember).pack(anchor='w')
    def apply():
        try:
            app.preferences.update(theme=theme_names[selected.get()], developer_mode=developer.get(), follow_logs=follow.get(), font_size=size.get(), remember_window=remember.get())
            app._apply_preferences()
            theme(top, theme_names[selected.get()], size.get())
        except Exception as error:
            messagebox.showerror('设置未保存', str(error), parent=top)
    ttk.Button(frame, text='应用外观与模式', command=apply).pack(anchor='w')
    from account_ui import mount_account
    mount_account(frame, app.account)
    status = scrolledtext.ScrolledText(frame, height=16, wrap='word', state='disabled')
    status.pack(fill='both', expand=True, pady=10)
    last_queue_read = [0.]
    def refresh():
        if not top.winfo_exists():
            return
        if time.monotonic()-last_queue_read[0] > 5 and not getattr(app, '_closing', False):
            last_queue_read[0] = time.monotonic()
            binding = app.training_data.binding()
            def read_queue():
                value = app.training_data.outbox.stats(binding)
                return {'records': value['records'], 'states': value['states'], 'has_error': bool(value['last_error'])}
            def received(value, error):
                if app.training_data.binding() == binding:
                    app._queue_status = {'unknown': True} if error else value
            app.tasks.submit('queue-status', read_queue, received)
        from desktop_status import snapshot
        data = snapshot(app)
        text = '\n\n'.join(f'{key}：{value if value is not None else "暂无"}' if not isinstance(value, dict) else key+'：\n'+'\n'.join(f'  {k}：{v}' for k,v in value.items()) for key,value in data.items())
        if status.get('1.0', 'end-1c') != text:
            position = status.yview()[0]
            selected_range = status.tag_ranges('sel')
            status.configure(state='normal')
            status.delete('1.0', 'end')
            status.insert('end', text)
            if selected_range:
                status.tag_add('sel', *selected_range)
            status.yview_moveto(position)
            status.configure(state='disabled')
        schedule(top, 1000, refresh)
    ttk.Button(frame, text='请求账号与正式模型重新检查', command=app.account.sync_requested.set).pack(anchor='w')
    ttk.Button(frame, text='唤醒自动补传', command=lambda: app.training_data.uploader.wake_event.set()).pack(anchor='w', pady=6)
    def export_records():
        path = filedialog.asksaveasfilename(parent=top, defaultextension='.csv', initialfile='director-local-records.csv')
        if not path:
            return
        binding = app.training_data.binding()
        def done(count, error):
            if not top.winfo_exists():
                return
            if error:
                messagebox.showerror('导出未完成', '本地记录读取或文件写入失败，请检查路径与磁盘。', parent=top)
            else:
                messagebox.showinfo('导出完成', f'已导出当前账号在本设备的 {count} 条记录。', parent=top)
        app.tasks.submit('export-records', lambda: app.training_data.outbox.export_csv(binding, path), done)
    ttk.Button(frame, text='导出当前账号本地记录', command=export_records).pack(anchor='w', pady=6)
    ttk.Label(frame, text='功能：自动导播、MulNX运镜、可选OBS、叙事、CSV/Demo训练。常用连接与功能开关在主窗口设置页。正式模型仍需服务端批准；本地训练只生成候选。', wraplength=660).pack(anchor='w', pady=8)
    def export():
        path = filedialog.asksaveasfilename(parent=top, defaultextension='.json', initialfile='director-diagnostics.json')
        if path:
            from pathlib import Path
            from desktop_status import snapshot
            Path(path).write_text(json.dumps(snapshot(app), ensure_ascii=False, indent=2), encoding='utf-8')
    ttk.Button(frame, text='导出状态诊断（不含凭据）', command=export).pack(anchor='w')
    if hasattr(app, '_export_bundle'):
        backup = ttk.LabelFrame(frame, text='备份与恢复', padding=10)
        backup.pack(fill='x', pady=12)
        ttk.Button(backup, text='导出配置与模型备份', command=app._export_bundle).pack(anchor='w')
        ttk.Button(backup, text='导入已有备份', command=app._import_bundle).pack(anchor='w', pady=6)
    if hasattr(app, '_collect_cfg'):
        camera = ttk.LabelFrame(frame, text='MulNX 更新与运镜包', padding=10)
        camera.pack(fill='x', pady=10)
        from mulnx_preflight import UPGRADE_HELP
        ttk.Label(camera, text=UPGRADE_HELP, wraplength=660, justify='left').pack(anchor='w')
        def check_camera():
            from pathlib import Path
            from mulnx_preflight import inspect, describe
            try:
                draft = app._collect_cfg()
            except Exception:
                messagebox.showerror('先检查配置', '主窗口配置存在无效字段，请修正后再核对。', parent=top)
                return
            root = filedialog.askdirectory(parent=top, title='选择实际使用的 MulNX 安装目录',
                                           initialdir=getattr(app, '_mulnx_directory', str(Path.home()/'Desktop')))
            if not root:
                return
            app._mulnx_directory = root
            pack = filedialog.askdirectory(parent=top, title='选择当前地图运镜包（CamPacks 内的一个文件夹）',
                                           initialdir=str(Path(root)/'CS2OBTool'/'CameraSystem'/'CamPacks'))
            if not pack:
                return
            def done(result, error):
                if not top.winfo_exists():
                    return
                text_dialog(top, 'MulNX 运镜包核对', str(error) if error else describe(result))
            app.tasks.submit('mulnx-preflight', lambda: inspect(root, pack, draft), done)
        ttk.Button(camera, text='选择安装目录与运镜包，核对快捷键', command=check_camera).pack(anchor='w', pady=8)
        ttk.Label(camera, text='按主窗口当前草稿核对；运行中配置仍在停止后下次启动生效。仅读取文件，不启动 MulNX、不覆盖运镜包。', wraplength=660).pack(anchor='w')
    help_box = ttk.LabelFrame(frame, text='使用帮助', padding=10)
    help_box.pack(fill='x', pady=10)
    ttk.Label(help_box, text='1. 登录并等待正式模型就绪。\n2. 选择 CS2 程序、安装 GSI 后重启游戏。\n3. 配置与 MulNX 一致的运镜快捷键，再启动导播。\n4. 手动切人会让 AI 暂时让位；停止导播会释放按键并保存记录。\n\n本地训练生成候选模型；模型获批后才可用于正式执行。离线许可从服务端签发时起算，最长 168 小时。', wraplength=660, justify='left').pack(anchor='w')
    refresh()
    theme(top, values['theme'], values['font_size'])


def text_dialog(parent, title, text):
    top = tk.Toplevel(parent)
    top.title(title)
    fit_window(top, 760, 600)
    content = scrolledtext.ScrolledText(top, wrap='word', padx=14, pady=14)
    content.pack(fill='both', expand=True)
    content.insert('1.0', text)
    content.configure(state='disabled')
    ttk.Button(top, text='关闭', command=top.destroy).pack(anchor='e', padx=12, pady=8)
    theme(top, getattr(parent, '_theme_mode', 'dark'), getattr(parent, '_font_size', 10))
    return top
