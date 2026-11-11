#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CS2 AI 决策模型训练器：导播内置训练工作台，读取 decisions.csv 训练随机森林。"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from desktop_ui import schedule
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Optional

from decision_calibration import calibrate_csv
from decision_model import decision_csv_path, decision_model_path
from train_decision_model import TrainingError, train_from_csv
import ai_trainer as ai_assist
from demo_live_dataset import DEFAULT_HORIZON, DEFAULT_STEP, validate_sampling

APP_TITLE = "Yuzi_CS2_AIDirector · 训练工作台"
from client_version import VERSION, SLOGAN
APP_SUBTITLE = SLOGAN

UI_BG = "#f2ead9"
UI_CARD = "#fffdf8"
UI_INK = "#20242a"
UI_MUTED = "#6f6a60"
UI_GOLD = "#b8862f"
UI_GOLD_DARK = "#7a561c"
UI_HEADER = "#171a20"
UI_BORDER = "#e2d6bf"


def asset_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets" / name


class TrainerApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        from desktop_tasks import Tasks
        self.tasks = Tasks(self.root)
        self._closing = False
        self.root.title(f"Yuzi CS2 AI Director · 训练工作台 v{VERSION}")
        self.root.geometry("940x920")
        from desktop_ui import fit_window
        fit_window(self.root, 940, 850)
        from preferences import Preferences
        self.preferences = Preferences(decision_model_path().parent)
        from desktop_ui import restore_window
        restore_window(self, 'trainer', 940, 850)
        self.root.report_callback_exception = self._on_tk_exception
        self._init_style()
        self._set_window_icon()
        self._training = False
        self._calibrating = False
        self._submitting = False

        self.var_csv = tk.StringVar(value=str(decision_csv_path()))
        self.var_out_dir = tk.StringVar(value=str(decision_model_path().parent))
        self.var_demos = tk.StringVar()
        self.var_horizon = tk.StringVar(value=str(DEFAULT_HORIZON))
        self.var_step = tk.StringVar(value=str(DEFAULT_STEP))
        self.var_min = tk.StringVar(value="1000")
        self.var_trees = tk.StringVar(value="300")
        self.var_depth = tk.StringVar(value="8")
        self.var_test = tk.StringVar(value="0.25")
        self.var_ai = tk.BooleanVar(value=True)
        ai_cfg = ai_assist.load_ai_config()
        self.var_ai_status = tk.StringVar(
            value=(f"DeepSeek 已就绪：{ai_cfg.model}" if ai_cfg.enabled
                   else "未找到 API Key，将只做本地扩展搜索"))

        self._events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=2000)
        self._dropped_log_lines = 0
        from account_client import default_account
        self.account = default_account()
        from diagnostics_runtime import attach
        attach(self)
        self._build()
        self._apply_preferences()
        from training_data_runtime import TrainingDataRuntime
        self.training_data = TrainingDataRuntime(identity_provider=self.account.upload_identity, binding_provider=self.account.binding)
        from dataset_runtime import DatasetRuntime
        self.datasets = DatasetRuntime(self.account)
        self.account.start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        schedule(self.root,100, self._poll_events)
        from client_update_ui import install as install_updates
        install_updates(self)

    def _apply_preferences(self):
        from desktop_ui import theme
        prefs = self.preferences.read()
        self._ui_preferences = prefs
        from desktop_ui import PALETTES
        self._colors = PALETTES[prefs['theme']]
        theme(self.root, prefs['theme'], prefs['font_size'])
        self._follow_logs = prefs['follow_logs']
        for widget in self._developer_widgets:
            widget.grid() if prefs['developer_mode'] else widget.grid_remove()
        if prefs['developer_mode']:
            self._cal_row.grid()
        else:
            self._cal_row.grid_remove()

    def _on_tk_exception(self, exc: Any, val: Any, tb: Any) -> None:
        from diagnostics_runtime import capture_gui
        capture_gui(self, exc, tb)
        try:
            messagebox.showerror("界面异常", f"{getattr(exc, '__name__', exc)}: {val}",
                                 parent=self.root)
        except Exception:
            pass

    def _init_style(self) -> None:
        from desktop_ui import theme
        self.root.option_add('*Font', ('Microsoft YaHei UI', 10))
        from desktop_ui import PALETTES
        self._colors = PALETTES[self.preferences.read()['theme']]
        theme(self.root, self.preferences.read()['theme'])

    def _set_window_icon(self) -> None:
        try:
            ico = asset_path("yuzi.ico")
            if ico.is_file():
                self.root.iconbitmap(str(ico))
        except Exception:
            pass
        try:
            png = asset_path("yuzi_logo_64.png")
            if png.is_file():
                self._icon_img = tk.PhotoImage(file=str(png))
                self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _build(self) -> None:
        colors = self._colors
        header = tk.Frame(self.root, bg=colors['header'], height=100)
        header._theme_surface = 'header'
        header.pack(fill="x")
        header.pack_propagate(False)
        header_inner = tk.Frame(header, bg=colors['header'])
        header_inner._theme_surface = 'header'
        header_inner.pack(fill="both", expand=True, padx=22, pady=(13, 11))
        try:
            self._logo_img = tk.PhotoImage(file=str(asset_path("yuzi_logo_76.png")))
            tk.Label(header_inner, image=self._logo_img,
                     bg=colors['header']).pack(side="left", padx=(0, 16))
        except Exception:
            self._logo_img = None

        title_box = tk.Frame(header_inner, bg=colors['header'])
        title_box._theme_surface = 'header'
        title_box.pack(side="left", fill="y")
        tk.Label(title_box, text="Yuzi CS2 AI Director",
                 bg=colors['header'], fg=colors['accent'],
                 font=("Microsoft YaHei UI", 21, "bold")).pack(
            side="top", anchor="w", pady=(1, 0))
        tk.Label(title_box, text=f"训练工作台  ·  {APP_SUBTITLE}  ·  v{VERSION}",
                 bg=colors['header'], fg=colors['muted'],
                 font=("Microsoft YaHei UI", 9)).pack(side="top", anchor="w", pady=(2, 0))

        mark = tk.Frame(header_inner, bg=colors['header'])
        mark._theme_surface = 'header'
        mark.pack(side="right", anchor="e", padx=(0, 2))
        tk.Label(mark, text="YUZI CUP", bg=colors['header'],
                 fg=colors['accent'], font=("Segoe UI", 9, "bold")).pack(anchor="e")
        tk.Label(mark, text="TRAINER CONSOLE", bg=colors['header'],
                 fg=colors['muted'], font=("Segoe UI", 8)).pack(anchor="e", pady=(2, 0))
        rule = tk.Frame(header, bg=colors['accent'], height=2, bd=0, highlightthickness=0)
        rule._theme_surface = 'accent'
        rule.place(relx=0, rely=1.0, relwidth=1.0, anchor="sw")

        from account_ui import mount_account
        mount_account(self.root, self.account)
        from desktop_ui import ScrollableFrame, settings as open_settings

        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=22, pady=(8, 0))
        top.columnconfigure(0, weight=1)
        title_col = ttk.Frame(top)
        title_col.grid(row=0, column=0, sticky="w")
        ttk.Label(title_col, text="训练工作台", style='H1.TLabel').pack(anchor="w")
        ttk.Label(title_col, text="准备数据、训练候选、校准并提交审核；所有产物先保存在本机。",
                  style='Caption.TLabel').pack(anchor="w", pady=(2, 0))
        ttk.Button(top, text="设置中心", style='Link.TButton',
                   command=lambda: open_settings(self)).grid(row=0, column=1, sticky="e")

        scroll = ScrollableFrame(self.root)
        scroll.pack(fill="both", expand=True, padx=12, pady=(10, 10))
        page = scroll.inner
        page.columnconfigure(0, weight=3)
        page.columnconfigure(1, weight=2)

        def _card(parent):
            shell = tk.Frame(parent, bg=colors['card'],
                             highlightbackground=colors['border'],
                             highlightthickness=1, bd=0)
            shell._theme_surface = 'card'
            inner = ttk.Frame(shell)
            inner.pack(fill="both", expand=True, padx=16, pady=10)
            return shell, inner

        def _caption(parent, text, pady=(4, 0)):
            label = ttk.Label(parent, text=text, style='Caption.TLabel',
                              justify="left", wraplength=460)
            label._own_wrap = True
            label.pack(anchor="w", pady=pady)
            parent.bind(
                '<Configure>',
                lambda e, w=label: w.configure(wraplength=max(180, e.width - 28)),
                add='+')
            return label

        def _section(parent, number, title, note):
            head = ttk.Frame(parent)
            head.pack(fill="x")
            ttk.Label(head, text=number, style='StepNum.TLabel').pack(side="left", anchor="n")
            text_col = ttk.Frame(head)
            text_col.pack(side="left", fill="x", expand=True, padx=(10, 0))
            ttk.Label(text_col, text=title, style='StepTitle.TLabel').pack(anchor="w")
            note_label = ttk.Label(text_col, text=note, style='Caption.TLabel',
                                   justify="left", wraplength=420)
            note_label._own_wrap = True
            note_label.pack(anchor="w", pady=(2, 0))
            parent.bind(
                '<Configure>',
                lambda e, w=note_label: w.configure(wraplength=max(180, e.width - 68)),
                add='+')

        left = ttk.Frame(page)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left.columnconfigure(0, weight=1)
        right = ttk.Frame(page)
        right.grid(row=0, column=1, sticky="nsew")

        data_shell, data = _card(left)
        data_shell.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        _section(data, "01", "训练数据", "读取导播在线决策日志，训练候选模型。")
        form = ttk.Frame(data)
        form.pack(fill="x", pady=(10, 0))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="决策 CSV", style='Muted.TLabel').grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(form, textvariable=self.var_csv).grid(
            row=0, column=1, sticky="ew", padx=(8, 6), pady=(0, 6))
        ttk.Button(form, text="浏览…", width=7, command=self._browse_csv).grid(
            row=0, column=2, pady=(0, 6))
        ttk.Label(form, text="输出目录", style='Muted.TLabel').grid(
            row=1, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.var_out_dir).grid(
            row=1, column=1, sticky="ew", padx=(8, 6))
        ttk.Button(form, text="浏览…", width=7, command=self._browse_out).grid(
            row=1, column=2)
        _caption(data, "模型产物写入输出目录；正式导播只使用服务端批准并校验的模型。")

        demo_shell, demo = _card(left)
        demo_shell.grid(row=1, column=0, sticky="ew")
        _section(demo, "02", "样本来源", "可选：从 .dem 解析 live 采样，生成训练 CSV。")
        demo_body = ttk.Frame(demo)
        demo_body.pack(fill="x")
        demo_body.columnconfigure(0, weight=1)
        ttk.Entry(demo_body, textvariable=self.var_demos).grid(
            row=0, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(demo_body, text="选文件", width=7, command=self._browse_demos).grid(
            row=0, column=3, padx=(6, 0), pady=(10, 0))
        ttk.Button(demo_body, text="选目录", width=7, command=self._browse_demo_dir).grid(
            row=0, column=4, padx=(6, 0), pady=(10, 0))
        ticks = ttk.Frame(demo_body)
        ticks.grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Label(ticks, text="预测窗口 tick", style='Muted.TLabel').pack(side="left")
        ttk.Entry(ticks, textvariable=self.var_horizon, width=8).pack(side="left", padx=(6, 14))
        ttk.Label(ticks, text="采样间隔 tick", style='Muted.TLabel').pack(side="left")
        ttk.Entry(ticks, textvariable=self.var_step, width=8).pack(side="left", padx=(6, 14))
        self.btn_demo = ttk.Button(ticks, text="解析 demo 并训练",
                                   command=self._start_demo)
        self.btn_demo.pack(side="left")
        _caption(demo, "live 阶段定时采样；按 64 tick 估算：默认每 2 秒采样，预测未来 3 秒伤害/击杀。")

        params_shell, params = _card(left)
        params_shell.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        _section(params, "03", "训练参数", "控制样本门槛、树的规模与验证集比例。")
        param_grid = ttk.Frame(params)
        param_grid.pack(fill="x", pady=(10, 0))
        for i, (label, var, width) in enumerate((
            ("最少样本", self.var_min, 8),
            ("树数量", self.var_trees, 8),
            ("最大深度", self.var_depth, 8),
            ("验证集比例", self.var_test, 8),
        )):
            cell = ttk.Frame(param_grid)
            cell.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 18), pady=(0, 8))
            ttk.Label(cell, text=label, style='Muted.TLabel').pack(anchor="w")
            ttk.Entry(cell, textvariable=var, width=width).pack(anchor="w", pady=(2, 0))

        ai_shell, ai_box = _card(left)
        ai_shell.grid(row=3, column=0, sticky="ew")
        _section(ai_box, "04", "AI 辅助", "DeepSeek 只接收聚合统计；候选仍须本地验证通过。")
        ai_row = ttk.Frame(ai_box)
        ai_row.pack(fill="x", pady=(10, 0))
        self.chk_ai = tk.Checkbutton(
            ai_row, text="启用 DeepSeek 顾问",
            variable=self.var_ai, bg=colors['card'], fg=colors['text'],
            activebackground=colors['card'], activeforeground=colors['text'],
            selectcolor=colors['field'], highlightthickness=0, bd=0, anchor="w")
        self.chk_ai.pack(side="left", anchor="w")
        self.btn_ai = ttk.Button(ai_row, text="AI 辅助训练", style="Accent.TButton",
                                 command=self._start_ai)
        self.btn_ai.pack(side="right")
        ttk.Label(ai_box, textvariable=self.var_ai_status, style='Accent.TLabel',
                  justify="left", wraplength=420).pack(anchor="w", pady=(6, 2))
        _caption(ai_box, "只发送样本量、正负比、指标与特征重要性，不含 SteamID 或昵称；无 Key 时回退本地扩展搜索。")

        action_shell, action = _card(right)
        action_shell.pack(fill="x", pady=(0, 12))
        action.columnconfigure(0, weight=1)
        ttk.Label(action, text="训练操作", style='H3.TLabel').grid(row=0, column=0, sticky="w")
        action_note = ttk.Label(
            action, text="训练会写入本地候选模型；提交后仍需网站审核与发布。",
            style='Caption.TLabel', justify="left", wraplength=300)
        action_note._own_wrap = True
        action_note.grid(row=1, column=0, sticky="w", pady=(2, 0))
        action.bind(
            '<Configure>',
            lambda e, w=action_note: w.configure(wraplength=max(180, e.width - 32)),
            add='+')
        self.btn_train = ttk.Button(action, text="用 CSV 开始训练",
                                    command=self._start, style="Accent.TButton")
        self.btn_train.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        secondary = ttk.Frame(action)
        secondary.grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Button(secondary, text="打开输出目录",
                   command=self._open_out).pack(side="left")
        ttk.Button(secondary, text="训练数据状态",
                   command=lambda: __import__("desktop_ui").settings(self)).pack(
            side="left", padx=(6, 0))
        self._cal_row = ttk.Frame(action)
        self._cal_row.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.btn_calibrate = ttk.Button(
            self._cal_row, text="校准线上模型", command=self._calibrate)
        self.btn_calibrate.pack(side="left")

        status_panel = tk.Frame(action, bg=colors['field'],
                                highlightbackground=colors['border'],
                                highlightthickness=1, bd=0)
        status_panel._theme_surface = 'field'
        status_panel.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self.progress = ttk.Progressbar(status_panel, mode="indeterminate", length=160)
        self.progress.pack(fill="x", padx=10, pady=(9, 5))
        self.lbl_status = ttk.Label(status_panel, text="就绪", style='Status.TLabel',
                                    justify="left", wraplength=240)
        self.lbl_status.pack(anchor="w", fill="x", padx=10, pady=(0, 9))
        self.lbl_status._own_wrap = True
        status_panel.bind(
            '<Configure>',
            lambda e: self.lbl_status.configure(wraplength=max(160, e.width - 22)),
            add='+')

        sub_shell, submission = _card(right)
        sub_shell.pack(fill="x", pady=(0, 12))
        ttk.Label(submission, text="训练数据上传", style='H3.TLabel').pack(anchor="w")
        self.btn_datasets = ttk.Button(submission, text="查看上传与筛选状态…",
                                      command=lambda: __import__('dataset_ui').open_datasets(self))
        self.btn_datasets.pack(fill='x', pady=(8,0))
        self.dataset_note=ttk.Label(submission, text='训练成功后自动排队。所有数据均须管理员筛选纳入，再由管理员主动训练。',
                  style='Muted.TLabel', wraplength=260, justify='left')
        self.dataset_note._own_wrap=True
        self.dataset_note.pack(fill='x', pady=(8,0))
        self.btn_submit = ttk.Button(submission, text="提交模型候选…",
                                     command=self._submit_result)
        self.btn_submit.pack(fill="x", pady=(8, 0))
        self.submission_status = ttk.Label(
            submission, style='Muted.TLabel', justify='left', wraplength=280,
            text='模型候选是独立的旧入口；上传模型不等于上传可重训样本。需要训练管理员权限。')
        self.submission_status.pack(anchor="w", fill="x", pady=(8, 0))
        self.submission_status._own_wrap = True
        submission.bind(
            '<Configure>',
            lambda e: (self.submission_status.configure(wraplength=max(160, e.width - 32)),
                       self.dataset_note.configure(wraplength=max(160,e.width-32))),
            add='+')

        help_shell, help_box = _card(right)
        help_shell.pack(fill="x")
        ttk.Label(help_box, text="训练说明", style='H3.TLabel').pack(anchor="w")
        for text in (
            "样本少于「最少样本」时不会开始训练。",
            "模型 JSON 会随树数量和深度增大，指标接近时优先紧凑模型。",
            "训练数据上传可后台续传；正式导播模型仍只由服务端审核发布。",
        ):
            row = ttk.Frame(help_box)
            row.pack(fill="x", pady=(7, 0))
            ttk.Label(row, text="·", style='Accent.TLabel').pack(side="left", anchor="n")
            label = ttk.Label(row, text=text, style='Caption.TLabel',
                              justify="left", wraplength=280)
            label._own_wrap = True
            label.pack(side="left", fill="x", expand=True, padx=(7, 0))
            help_box.bind(
                '<Configure>',
                lambda e, w=label: w.configure(wraplength=max(160, e.width - 46)),
                add='+')

        log_shell, log_card = _card(left)
        log_shell.grid(row=4, column=0, sticky="ew")
        log_head = ttk.Frame(log_card)
        log_head.pack(fill="x")
        ttk.Label(log_head, text="运行日志", style='H3.TLabel').pack(side="left")
        ttk.Button(log_head, text="回到最新日志", style='Link.TButton',
                   command=lambda: self.log.see("end")).pack(side="right")
        from desktop_ui import ThemedScrolledText
        self.log = ThemedScrolledText(
            log_card, height=7, wrap="word", font=("Consolas", 9),
            background=colors['field'], foreground=colors['text'],
            insertbackground=colors['text'], relief="flat")
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self.log.configure(state="disabled")

        self._developer_widgets = [params_shell, ai_shell]

        layout_state = {'narrow': None}

        def _reflow(event=None):
            width = event.width if event is not None else page.winfo_width()
            narrow = width < 820
            if layout_state['narrow'] == narrow:
                return
            layout_state['narrow'] = narrow
            if narrow:
                page.columnconfigure(0, weight=1)
                page.columnconfigure(1, weight=0)
                left.grid_configure(row=0, column=0, padx=0, sticky="ew")
                right.grid_configure(row=1, column=0, padx=0, pady=(14, 0), sticky="ew")
            else:
                page.columnconfigure(0, weight=3)
                page.columnconfigure(1, weight=2)
                left.grid_configure(row=0, column=0, padx=(0, 14), sticky="nsew")
                right.grid_configure(row=0, column=1, padx=0, pady=0, sticky="nsew")

        page.bind('<Configure>', _reflow, add='+')

    def _submit_result(self):
        if self._closing or self._training or self._calibrating or self._submitting:
            return
        path = filedialog.askopenfilename(parent=self.root,title='选择要上传的训练结果',
            initialdir=str(getattr(self,'_last_model_dir',self.var_out_dir.get().strip())),initialfile='decision_model.json.gz',
            filetypes=[('训练模型','*.json.gz')])
        if not path:
            return
        from training_submission import SubmissionClient, explain_error
        self._submitting = True
        for button in (self.btn_submit,self.btn_train,self.btn_demo,self.btn_ai,self.btn_calibrate):
            button.configure(state='disabled')
        self.submission_status.configure(text='正在校验结果并核对服务器回执…',foreground=self._colors['accent'])
        def done(receipt,error):
            self._submitting = False
            for button in (self.btn_submit,self.btn_train,self.btn_demo,self.btn_ai,self.btn_calibrate):
                button.configure(state='normal')
            if error:
                text = explain_error(error)
                self.submission_status.configure(text=text,foreground=self._colors['error'])
                self._append('训练结果提交未完成：'+text)
                return
            text = ('服务器已核对已有模型，原状态保持。' if receipt['existingModel'] else '服务器已接收，等待审核。')+'提交编号：'+receipt['requestId']
            self.submission_status.configure(text=text,foreground=self._colors['success'])
            self._append(text+'；正式导播模型保持。')
        self.tasks.submit('model-submission',lambda: SubmissionClient(self.account).submit(path),done)

    def _browse_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 decisions.csv", filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if path:
            self.var_csv.set(path)

    def _browse_out(self) -> None:
        path = filedialog.askdirectory(title="选择模型输出目录")
        if path:
            self.var_out_dir.set(path)

    def _open_out(self) -> None:
        try:
            os.startfile(str(getattr(self,'_last_model_dir',self.var_out_dir.get().strip())))  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("打开目录", str(e), parent=self.root)

    def _calibrate(self) -> None:
        if self._closing:
            return
        if self._training or self._calibrating or self._submitting:
            return
        csv_path = decision_csv_path()
        out_dir = getattr(self, '_last_model_dir', Path(self.var_out_dir.get().strip()))
        model_path = out_dir / "decision_model.pkl"
        if not csv_path.is_file():
            messagebox.showwarning("校准", f"找不到线上日志：\n{csv_path}", parent=self.root)
            return
        if not model_path.is_file():
            messagebox.showwarning("校准", f"找不到模型：\n{model_path}", parent=self.root)
            return
        self._calibrating = True
        self.btn_calibrate.configure(state="disabled")
        self.lbl_status.configure(text="校准中…", foreground=self._colors['accent'])
        self._append("开始用线上 decisions.csv 校准概率…")

        def worker() -> None:
            try:
                result = calibrate_csv(
                    csv_path, model_path,
                    out_dir / "decision_calibration.json",
                    min_samples=500,
                )
                self._events.put(("calibration_done", result))
            except Exception as e:  # noqa: BLE001
                self._events.put(("calibration_error", f"{type(e).__name__}: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _append(self, text: str) -> None:
        if getattr(self, "diagnostics", None): self.diagnostics.observe(text)
        from desktop_ui import append_log
        append_log(self.log, text, follow_enabled=getattr(self, '_follow_logs', True))

    def _worker_log(self, text: str) -> None:
        try:
            self._events.put_nowait(('log', text))
        except queue.Full:
            self._dropped_log_lines += 1

    def _poll_events(self) -> None:
        if self._dropped_log_lines:
            count, self._dropped_log_lines = self._dropped_log_lines, 0
            self._append(f'显示队列繁忙，省略 {count} 条日志；训练结果与本地记录不受影响。')
        try:
            for _ in range(80):
                kind, payload = self._events.get_nowait()
                if kind == "log":
                    self._append(str(payload))
                elif kind == "done":
                    self._done(payload, None)
                elif kind == "error":
                    self._done(None, str(payload))
                elif kind == "calibration_done":
                    self._calibration_done(payload)
                elif kind == "calibration_error":
                    self._calibration_done({"ok": False, "reason": str(payload)})
                elif kind == "ai_done":
                    self._ai_done(payload)
                elif kind == "ai_error":
                    self._done(None, str(payload))
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            schedule(self.root,100, self._poll_events)

    def _browse_demos(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择 .dem 文件", filetypes=[("CS2 demo", "*.dem")])
        if paths:
            self.var_demos.set(";".join(paths))

    def _browse_demo_dir(self) -> None:
        path = filedialog.askdirectory(title="选择包含 .dem 的文件夹")
        if path:
            self.var_demos.set(path)

    def _read_params(self) -> Optional[tuple[int, int, int, float]]:
        try:
            min_samples = int(self.var_min.get().strip())
            trees = int(self.var_trees.get().strip())
            depth = int(self.var_depth.get().strip())
            test_size = float(self.var_test.get().strip())
            if not (0.1 <= test_size <= 0.5):
                raise ValueError("验证集比例需在 0.1–0.5")
            if min_samples < 2:
                raise ValueError("最少样本至少为 2")
            return min_samples, trees, depth, test_size
        except ValueError as e:
            messagebox.showwarning("参数错误", str(e), parent=self.root)
            return None

    def _begin_training(self, message: str) -> None:
        self._training = True
        self.btn_train.configure(state="disabled")
        self.btn_demo.configure(state="disabled")
        self.btn_ai.configure(state="disabled")
        self.btn_submit.configure(state="disabled")
        self.progress.start(10)
        self.lbl_status.configure(text=message, foreground=self._colors['accent'])
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _run_worker(self, csv_path: Path, out: Path, metrics: Path,
                    params: tuple[int, int, int, float],
                    demo_paths: Optional[list[str]] = None,
                    horizon: int = DEFAULT_HORIZON,
                    step: int = DEFAULT_STEP) -> None:
        min_samples, trees, depth, test_size = params

        def worker() -> None:
            run_id = None
            try:
                if demo_paths:
                    from demo_dataset import build_demo_csv
                    self._worker_log("开始解析 demo …")
                    build_demo_csv(demo_paths, csv_path, horizon=horizon,
                                   step=step, log=self._worker_log)
                run_id, frozen_csv = self.datasets.begin_training(csv_path)
                run_out = out.parent / 'runs' / run_id
                run_out.mkdir(parents=True, exist_ok=False)
                result = train_from_csv(
                    frozen_csv, run_out / out.name, run_out / metrics.name,
                    min_samples=min_samples, test_size=test_size,
                    trees=trees, max_depth=depth,
                    log=self._worker_log,
                )
                self._dataset_completed(run_id, result)
                self._events.put(("done", result))
            except TrainingError as e:
                if run_id:self.datasets.failed(run_id)
                self._events.put(("error", str(e)))
            except Exception as e:
                if run_id:self.datasets.failed(run_id)
                self._events.put(("error", f"{type(e).__name__}: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _dataset_completed(self, run_id, result):
        try:
            data = self.datasets.succeeded(run_id, result)
            self._worker_log('训练数据已进入持久上传队列；服务器接收后仍待管理员筛选。' if data else
                             '本地训练完成；训练数据整理未完成，请在上传状态中查看原因并重试。')
        except Exception:
            self._worker_log('本地训练完成；数据队列暂不可用，输入快照已保留，请在上传状态中处理。')

    def _start(self) -> None:
        if self._closing:
            return
        if self._training or self._calibrating or self._submitting:
            return
        params = self._read_params()
        if params is None:
            return
        csv_path = Path(self.var_csv.get().strip())
        if not csv_path.is_file():
            messagebox.showwarning("找不到 CSV", str(csv_path), parent=self.root)
            return
        out_dir = Path(self.var_out_dir.get().strip())
        self._begin_training("训练中…")
        self._run_worker(
            csv_path, out_dir / "decision_model.pkl",
            out_dir / "decision_model_metrics.json", params,
        )

    def _start_demo(self) -> None:
        if self._closing:
            return
        if self._training or self._calibrating or self._submitting:
            return
        params = self._read_params()
        if params is None:
            return
        raw = self.var_demos.get().strip()
        demos = [p.strip() for p in raw.replace("\n", ";").split(";") if p.strip()]
        if not demos:
            messagebox.showwarning("缺少 demo", "请选择 .dem 文件或文件夹。",
                                   parent=self.root)
            return
        try:
            horizon = int(self.var_horizon.get().strip())
            step = int(self.var_step.get().strip())
            validate_sampling(horizon, step)
        except ValueError:
            messagebox.showwarning("参数错误", "预测窗口和采样间隔 tick 必须是正整数。",
                                   parent=self.root)
            return
        out_dir = Path(self.var_out_dir.get().strip())
        out_dir.mkdir(parents=True, exist_ok=True)
        self._begin_training("解析 demo 并训练中…")
        self._run_worker(
            out_dir / "demo_decisions.csv",
            out_dir / "decision_model.pkl",
            out_dir / "decision_model_metrics.json",
            params, demo_paths=demos, horizon=horizon, step=step,
        )

    def _start_ai(self) -> None:
        if self._closing:
            return
        if self._training or self._calibrating or self._submitting:
            return
        params = self._read_params()
        if params is None:
            return
        csv_path = Path(self.var_csv.get().strip())
        if not csv_path.is_file():
            messagebox.showwarning("找不到 CSV", str(csv_path), parent=self.root)
            return
        min_samples, trees, depth, test_size = params
        out_dir = Path(self.var_out_dir.get().strip())
        out_dir.mkdir(parents=True, exist_ok=True)
        use_ai = bool(self.var_ai.get())
        self._begin_training("AI 辅助训练中…" if use_ai else "本地搜索训练中…")

        def worker() -> None:
            run_id = None
            try:
                run_id, frozen_csv = self.datasets.begin_training(csv_path)
                run_out = out_dir / 'runs' / run_id
                run_out.mkdir(parents=True, exist_ok=False)
                report = ai_assist.run_ai_training(
                    frozen_csv, run_out,
                    run_out / "decision_model_metrics.json",
                    min_samples=min_samples, test_size=test_size,
                    trees=trees, max_depth=depth, use_ai=use_ai,
                    log=self._worker_log,
                )
                self._dataset_completed(run_id, dict(report.get('outputs') or {}, split_hash=report.get('split_hash')))
                self._events.put(("ai_done", report))
            except TrainingError as e:
                if run_id:self.datasets.failed(run_id)
                self._events.put(("ai_error", str(e)))
            except Exception as e:  # noqa: BLE001
                if run_id:self.datasets.failed(run_id)
                self._events.put(("ai_error", f"{type(e).__name__}: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _ai_done(self, report: dict[str, Any]) -> None:
        if (report.get('outputs') or {}).get('model_json'):
            self._last_model_dir = Path(report['outputs']['model_json']).parent
        self._training = False
        self.btn_train.configure(state="normal")
        self.btn_demo.configure(state="normal")
        self.btn_ai.configure(state="normal")
        self.btn_submit.configure(state="normal")
        self.progress.stop()
        decision = report.get("decision")
        final = (report.get("candidate") if decision == "candidate"
                 else report.get("baseline")) or {}
        base = report.get("baseline") or {}
        adopted = report.get("adopted")
        label = {"ai": "采用 AI 候选", "local": "采用本地搜索候选"}.get(
            adopted, "保留基线")
        auc = float(final.get("auc") or 0.0)
        top1 = final.get("top1")
        top1_text = "--" if top1 is None else f"{float(top1):.1%}"
        size_mb = float(final.get("model_json_mb") or 0.0)
        self.lbl_status.configure(
            text=f"{label}：AUC {auc:.4f} / top-1 {top1_text}",
            foreground=self._colors['success'])
        self._append("")
        self._append(f"AI 辅助训练完成：{label}，最终 AUC={auc:.4f}，"
                     f"top-1={top1_text}，JSON={size_mb:.2f}MB")
        for line in report.get("reasons", []):
            self._append("  " + str(line))
        advice = report.get("advice") or {}
        for line in (advice.get("diagnosis") or [])[:4]:
            self._append(f"AI 诊断：{line}")
        ai_error = (report.get("ai") or {}).get("error")
        if ai_error:
            self._append("AI 未使用：" + str(ai_error))
        body = [
            f"结果：{label}",
            f"基线 AUC：{float(base.get('auc') or 0):.4f}",
            f"最终 AUC：{auc:.4f}（top-1 {top1_text}）",
            f"模型体积：{size_mb:.2f}MB",
        ]
        diagnosis = advice.get("diagnosis") or []
        if diagnosis:
            body.extend(["", "AI 诊断："]
                        + [f"· {line}" for line in diagnosis[:4]])
        if ai_error:
            body.extend(["", f"AI 未使用：{ai_error}"])
        body.extend([
            "",
            f"模型：{((report.get('outputs') or {}).get('model_json', ''))}",
            f"报告：{report.get('report_path', '')}",
        ])
        from desktop_ui import text_dialog
        text_dialog(self.root, "AI 辅助训练完成 · 本地候选", "\n".join(body)+"\n\n本地训练生成候选，正式执行仍需后台审核与发布。")

    def _calibration_done(self, result: dict[str, Any]) -> None:
        self._calibrating = False
        self.btn_calibrate.configure(state="normal")
        if result.get("ok"):
            self.lbl_status.configure(text="校准完成", foreground=self._colors['success'])
            text = (f"校准完成：Brier {result.get('brier_before'):.4f} → "
                    f"{result.get('brier_after'):.4f}，"
                    f"AUC {result.get('auc', 0):.4f}，"
                    f"样本 {result.get('samples', 0)}")
            self._append(text)
            messagebox.showinfo(
                "校准完成",
                text + f"\n\n导播会自动加载：\n{result.get('path', '')}",
                parent=self.root)
        else:
            self.lbl_status.configure(text="校准未应用", foreground=self._colors['accent'])
            self._append("校准未应用：" + str(result.get("reason", "")))
            messagebox.showwarning(
                "校准未应用", str(result.get("reason", "")), parent=self.root)

    def _done(self, result: Optional[dict[str, Any]], error: Optional[str]) -> None:
        self._training = False
        self.btn_train.configure(state="normal")
        self.btn_demo.configure(state="normal")
        self.btn_ai.configure(state="normal")
        self.btn_submit.configure(state="normal")
        self.progress.stop()
        if error:
            self.lbl_status.configure(text="训练失败", foreground=self._colors['error'])
            self._append("训练失败：" + error)
            if getattr(self, 'diagnostics', None): self.diagnostics.capture('error')
            messagebox.showerror("训练失败", error, parent=self.root)
            return
        assert result is not None
        self._last_model_dir = Path(result['model_json']).parent
        top1 = result.get("top1")
        top1_text = "--" if top1 is None else f"{float(top1):.1%}"
        ambiguous = int(result.get("ambiguous", 0) or 0)
        split = str(result.get("split", "?"))
        size_mb = float(result.get("model_json_bytes", 0) or 0) / (1024 * 1024)
        self.lbl_status.configure(
            text=f"完成：AUC {result['auc']:.4f} / top-1 {top1_text}，"
                 f"{result.get('algorithm', '')}，{size_mb:.1f}MB，样本 {result['samples']}",
            foreground=self._colors['success'],
        )
        self._append("")
        self._append(f"训练完成。AUC={result['auc']:.4f}，top-1={top1_text}，"
                     f"切分={split}，模糊样本={ambiguous}，"
                     f"模型={result.get('algorithm', '')}，JSON={size_mb:.1f}MB，"
                     f"文件={result['model_json']}")
        warning = ""
        if ambiguous == 0:
            warning = ("\n\n注意：ambiguous=0，这份 CSV 可能是旧版生成的；"
                       "请用当前训练器重新从 .dem 导入后再训练。")
        if size_mb > 20:
            warning += (f"\n\n注意：模型 JSON 为 {size_mb:.1f}MB，较大；"
                        "启动/推理会变慢，建议在指标接近时优先选 RandomForest。")
        from desktop_ui import text_dialog
        text_dialog(
            self.root,
            "训练完成",
            f"AUC：{result['auc']:.4f}\n"
            f"top-1：{top1_text}（{result.get('top1_groups', 0)} 组）\n"
            f"切分：{split}\n"
            f"模糊样本：{ambiguous}\n"
            f"模型：{result.get('algorithm', '')}（JSON {size_mb:.1f}MB）\n"
            f"样本：{result['samples']}\n"
            f"本地候选模型（正式执行需服务端批准）：\n{result['model_json']}"
            + warning,
        )

    def _on_close(self) -> None:
        if self._training or self._calibrating or self._submitting:
            messagebox.showinfo("任务运行中", "请等待训练、校准或结果提交完成后关闭工作台。", parent=self.root)
            return
        if self._closing:
            return
        self._closing = True
        self._append('正在保存本地记录，请稍候…')
        from desktop_ui import window_preferences
        window_changes = window_preferences(self, 'trainer')
        def work():
            self.updates.shutdown()
            if window_changes:
                self.preferences.update(**window_changes)
            if not self.training_data.close():
                return False
            if not self.datasets.close():
                return False
            if getattr(self, "diagnostics", None): self.diagnostics.close()
            self.account.close()
            return True
        def done(flushed, error):
            if error or not flushed:
                self._closing = False
                messagebox.showerror('关闭未完成', str(error) if error else self.training_data.status_text(), parent=self.root)
                return
            from client_update_install import before_destroy
            before_destroy(self)
            self.root.destroy()
        self.tasks.submit('close', work, done)

    def run(self) -> None:
        self.root.mainloop()


def _demo_cli(args: list[str], runtime=None) -> int:
    import argparse
    from demo_dataset import build_demo_csv

    parser = argparse.ArgumentParser(description="从 demo 训练决策模型")
    parser.add_argument("--demos", nargs="+", required=True)
    parser.add_argument("--demo-csv", default="demo_decisions.csv")
    parser.add_argument("--out", default=str(decision_model_path()))
    parser.add_argument("--metrics", default="decision_model_metrics.json")
    parser.add_argument("--min-samples", type=int, default=1000)
    parser.add_argument("--test-size", type=float, default=0.25)
    parser.add_argument("--trees", type=int, default=300)
    parser.add_argument("--max-depth", type=int, default=8)
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--horizon-ticks", type=int, default=DEFAULT_HORIZON,
                        help="未来伤害/击杀标签窗口，默认 192 tick")
    window.add_argument("--tick-offset", type=int,
                        help="弃用别名：仅设置未来标签窗口，不再按击杀回看")
    parser.add_argument("--step-ticks", type=int, default=DEFAULT_STEP,
                        help="live 阶段采样间隔，默认 128 tick")
    ns = parser.parse_args(args)
    run_id = None
    try:
        build_demo_csv(ns.demos, Path(ns.demo_csv), ns.tick_offset,
                       horizon=ns.horizon_ticks, step=ns.step_ticks)
        if runtime is not None:
            run_id, csv_input = runtime.begin_training(Path(ns.demo_csv))
            output = Path(ns.out).parent / 'runs' / run_id
            output.mkdir(parents=True, exist_ok=False)
        else:
            csv_input = Path(ns.demo_csv)
            output = None
        result = train_from_csv(
            csv_input, output / Path(ns.out).name if output else Path(ns.out),
            output / Path(ns.metrics).name if output else Path(ns.metrics),
            min_samples=ns.min_samples, test_size=ns.test_size,
            trees=ns.trees, max_depth=ns.max_depth,
        )
        if run_id:
            try:runtime.succeeded(run_id, result)
            except Exception:print('本地训练完成；数据整理未完成，输入快照保留。', file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        if run_id:runtime.failed(run_id)
        print(f"失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


def _csv_cli(args, runtime):
    """GUI and frozen CLI use the same immutable lifecycle; never feed the old sample queue."""
    import json
    def value(flag, default):
        for i, arg in enumerate(args):
            if arg.startswith(flag+'='):return arg.split('=',1)[1]
            if arg==flag:
                if i+1>=len(args):raise ValueError('缺少参数：'+flag)
                return args[i+1]
        return default
    def replace(values, flag, selected):
        result=[];skip=False
        for arg in values:
            if skip:skip=False;continue
            if arg==flag:skip=True;continue
            if arg.startswith(flag+'='):continue
            result.append(arg)
        return result+[flag,str(selected)]
    from train_decision_model import main as normal_main
    from ai_trainer import main as ai_main
    invoke=ai_main if '--ai' in args else normal_main
    arguments=[a for a in args if a!='--ai']
    if '--help' in arguments or '-h' in arguments:return invoke(arguments)
    run_id=None
    try:
        selected=Path(value('--csv',str(decision_csv_path())))
        model=Path(value('--out',str(decision_model_path())))
        metrics=Path(value('--metrics','decision_model_metrics.json') or 'decision_model_metrics.json')
        run_id,frozen=runtime.begin_training(selected)
        output=model.parent/'runs'/run_id;output.mkdir(parents=True,exist_ok=False)
        arguments=replace(arguments,'--csv',frozen)
        arguments=replace(arguments,'--out',output/model.name)
        arguments=replace(arguments,'--metrics',output/metrics.name)
        code=invoke(arguments)
        if code!=0:runtime.failed(run_id);return code
        try:runtime.succeeded(run_id,json.loads((output/metrics.name).read_text(encoding='utf-8')))
        except Exception:print('本地训练完成；数据队列需要处理，输入快照已保留。',file=sys.stderr)
        return 0
    except Exception as error:
        if run_id:runtime.failed(run_id)
        print('训练未完成：'+str(error),file=sys.stderr);return 1


def main() -> None:
    # PyInstaller onefile 下如果仍被间接使用 multiprocessing，先注册 freeze_support。
    import multiprocessing
    multiprocessing.freeze_support()
    # 便于自动化/打包后验证：Yuzi_CS2_AIDecisionTrainer.exe --cli ...
    if "--cli" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--cli"]
        from account_client import default_account
        from training_data_runtime import TrainingDataRuntime
        from dataset_runtime import DatasetRuntime
        account = default_account()
        account.start()
        runtime = TrainingDataRuntime(identity_provider=account.upload_identity,binding_provider=account.binding)
        datasets = DatasetRuntime(account)
        try:
            if "--calibrate" in args:
                from decision_calibration import main as cal_main
                raise SystemExit(cal_main([a for a in args if a != "--calibrate"]))
            if "--demos" in args:
                raise SystemExit(_demo_cli(args,datasets))
            raise SystemExit(_csv_cli(args,datasets))
        finally:
            datasets.close();runtime.close();account.close()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    from diagnostics_runtime import bootstrap
    bootstrap(decision_model_path().parent)
    app=TrainerApp()
    if '--client-update-health' in sys.argv:
        from client_update_install import health_ready
        try:health_ready(app,Path(sys.argv[sys.argv.index('--client-update-health')+1]))
        except (OSError,ValueError,IndexError,KeyError):app._on_close()
    app.run()


if __name__ == "__main__":
    main()
