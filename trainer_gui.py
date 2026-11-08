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
APP_SUBTITLE = "CS2 AI 决策模型训练器"
from client_version import VERSION

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
        self.root.title(f"{APP_TITLE} v{VERSION} · {APP_SUBTITLE}")
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
        self._build()
        self._apply_preferences()
        from training_data_runtime import TrainingDataRuntime
        self.training_data = TrainingDataRuntime(identity_provider=self.account.upload_identity, binding_provider=self.account.binding)
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
            self.btn_calibrate.pack(side='left', before=self.progress)
        else:
            self.btn_calibrate.pack_forget()

    def _on_tk_exception(self, exc: Any, val: Any, tb: Any) -> None:
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
        header = tk.Frame(self.root, bg=UI_HEADER, height=92)
        header.pack(fill="x")
        header.pack_propagate(False)
        try:
            self._logo_img = tk.PhotoImage(file=str(asset_path("yuzi_logo_76.png")))
            tk.Label(header, image=self._logo_img, bg=UI_HEADER).pack(
                side="left", padx=(18, 14), pady=8)
        except Exception:
            self._logo_img = None
        title_box = tk.Frame(header, bg=UI_HEADER)
        title_box.pack(side="left", fill="y")
        tk.Label(title_box, text=APP_TITLE, bg=UI_HEADER, fg="#f0c46a",
                 font=("Microsoft YaHei UI", 17, "bold")).pack(
            side="top", anchor="w", pady=(12, 0))
        tk.Label(title_box, text=f"{APP_SUBTITLE}  ·  v{VERSION}",
                 bg=UI_HEADER, fg="#b9b2a5",
                 font=("Microsoft YaHei UI", 9)).pack(side="top", anchor="w", pady=(2, 0))
        tk.Label(header, text="YUZI CUP  ·  TRAINER", bg=UI_HEADER,
                 fg="#7f7769", font=("Segoe UI", 9, "bold")).pack(
            side="right", anchor="e", padx=(0, 18))

        from account_ui import mount_account
        mount_account(self.root,self.account)
        from desktop_ui import ScrollableFrame, settings as open_settings
        bar = ttk.Frame(self.root)
        bar.pack(fill="x")
        ttk.Button(bar, text="设置中心", command=lambda: open_settings(self)).pack(side="right", padx=10)
        scroll = ScrollableFrame(self.root)
        scroll.pack(fill="both", expand=True)
        outer = scroll.inner
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, text="决策 CSV").grid(row=0, column=0, sticky="w")
        ttk.Entry(outer, textvariable=self.var_csv).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Button(outer, text="浏览…", width=8, command=self._browse_csv).grid(
            row=0, column=2)

        ttk.Label(outer, text="输出目录").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(outer, textvariable=self.var_out_dir).grid(
            row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(outer, text="浏览…", width=8, command=self._browse_out).grid(
            row=1, column=2, pady=(6, 0))

        params = ttk.LabelFrame(outer, text=" 训练参数 ", padding=8)
        params.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        for i, (label, var, width) in enumerate((
            ("最少样本", self.var_min, 8),
            ("树数量", self.var_trees, 8),
            ("最大深度", self.var_depth, 8),
            ("验证集比例", self.var_test, 8),
        )):
            ttk.Label(params, text=label).grid(row=0, column=i * 2, sticky="w",
                                               padx=(0 if i == 0 else 12, 4))
            ttk.Entry(params, textvariable=var, width=width).grid(
                row=0, column=i * 2 + 1, sticky="w")

        ai_box = ttk.LabelFrame(
            outer, text=" AI 辅助训练（DeepSeek 顾问 + 本地验证） ", padding=8)
        ai_box.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 6))
        ai_box.columnconfigure(0, weight=1)
        self.chk_ai = tk.Checkbutton(
            ai_box, text="启用 DeepSeek 顾问（读取导播 config.json 的 Key）",
            variable=self.var_ai, bg=UI_CARD, fg=UI_INK,
            activebackground=UI_CARD, activeforeground=UI_INK,
            selectcolor="#ffffff", highlightthickness=0, bd=0, anchor="w")
        self.chk_ai.grid(row=0, column=0, sticky="w")
        self.btn_ai = ttk.Button(ai_box, text="AI 辅助训练（CSV）",
                                 command=self._start_ai, style="Accent.TButton")
        self.btn_ai.grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Label(ai_box, textvariable=self.var_ai_status,
                  foreground=UI_GOLD_DARK).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 2))
        ttk.Label(
            ai_box,
            text="只发送聚合统计（样本/正负比/指标/特征重要性），不含 SteamID/昵称；"
                 "AI 候选需在本地验证集上超过基线且体积不超标才采用，否则保留基线；"
                 "无 Key 或调用失败自动回退本地扩展搜索。",
            foreground="#555555", wraplength=820, justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w")

        self._developer_widgets = [params, ai_box]
        demo = ttk.LabelFrame(outer, text=" 从 .dem 导入（可选） ", padding=8)
        demo.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 6))
        demo.columnconfigure(1, weight=1)
        ttk.Label(demo, text="demo 文件/目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(demo, textvariable=self.var_demos).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Button(demo, text="选文件", width=8, command=self._browse_demos).grid(
            row=0, column=2)
        ttk.Button(demo, text="选目录", width=8, command=self._browse_demo_dir).grid(
            row=0, column=3, padx=(4, 0))
        row = ttk.Frame(demo)
        row.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Label(row, text="预测窗口 tick").pack(side="left")
        ttk.Entry(row, textvariable=self.var_horizon, width=8).pack(side="left", padx=4)
        ttk.Label(row, text="采样间隔 tick").pack(side="left")
        ttk.Entry(row, textvariable=self.var_step, width=8).pack(side="left", padx=4)
        self.btn_demo = ttk.Button(row, text="解析 demo 并训练",
                                   command=self._start_demo)
        self.btn_demo.pack(side="left", padx=8)
        ttk.Label(demo, text="live 阶段定时采样；按 64tick 估算：默认每 2 秒采样，预测未来 3 秒伤害/击杀。",
                  foreground="#777777").grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(4, 6))
        self.btn_train = ttk.Button(actions, text="用 CSV 开始训练",
                                    command=self._start, style="Accent.TButton")
        self.btn_train.pack(side="left")
        ttk.Button(actions, text="打开输出目录", command=self._open_out).pack(
            side="left", padx=8)
        self.btn_calibrate = ttk.Button(
            actions, text="校准线上模型", command=self._calibrate)
        self.btn_calibrate.pack(side="left")
        ttk.Button(actions, text="训练数据状态", command=lambda: __import__("desktop_ui").settings(self)).pack(side="left", padx=4)
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=200)
        self.progress.pack(side="left", padx=10)
        self.lbl_status = ttk.Label(actions, text="就绪", foreground="#555555")
        self.lbl_status.pack(side="left", padx=6)

        ttk.Label(
            outer,
            text="本地训练产物保存在输出目录；正式导播使用服务端批准并校验的模型。"
                 "样本少于最少样本时不会训练。",
            foreground="#555555", wraplength=800, justify="left",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 6))

        self.log = scrolledtext.ScrolledText(
            outer, height=13, wrap="word", font=("Consolas", 9),
            background="#111418", foreground="#d6d6d6",
            insertbackground="#d6d6d6", relief="flat")
        self.log.grid(row=7, column=0, columnspan=3, sticky="nsew")
        ttk.Button(outer, text="回到最新日志", command=lambda: self.log.see("end")).grid(row=8, column=0, columnspan=3, sticky="e")
        self.log.configure(state="disabled")
        outer.rowconfigure(7, weight=1)

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
            os.startfile(self.var_out_dir.get().strip())  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("打开目录", str(e), parent=self.root)

    def _calibrate(self) -> None:
        if self._closing:
            return
        if self._training or self._calibrating:
            return
        csv_path = decision_csv_path()
        out_dir = Path(self.var_out_dir.get().strip())
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
            try:
                if demo_paths:
                    from demo_dataset import build_demo_csv
                    self._worker_log("开始解析 demo …")
                    build_demo_csv(demo_paths, csv_path, horizon=horizon,
                                   step=step, log=self._worker_log)
                self.training_data.capture_training_file(csv_path, "offline" if demo_paths else "online")
                result = train_from_csv(
                    csv_path, out, metrics,
                    min_samples=min_samples, test_size=test_size,
                    trees=trees, max_depth=depth,
                    log=self._worker_log,
                )
                self._events.put(("done", result))
            except TrainingError as e:
                self._events.put(("error", str(e)))
            except Exception as e:
                self._events.put(("error", f"{type(e).__name__}: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _start(self) -> None:
        if self._closing:
            return
        if self._training:
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
        if self._training:
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
        if self._training or self._calibrating:
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
            try:
                self.training_data.capture_training_file(csv_path, "online")
                report = ai_assist.run_ai_training(
                    csv_path, out_dir,
                    out_dir / "decision_model_metrics.json",
                    min_samples=min_samples, test_size=test_size,
                    trees=trees, max_depth=depth, use_ai=use_ai,
                    log=self._worker_log,
                )
                self._events.put(("ai_done", report))
            except TrainingError as e:
                self._events.put(("ai_error", str(e)))
            except Exception as e:  # noqa: BLE001
                self._events.put(("ai_error", f"{type(e).__name__}: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _ai_done(self, report: dict[str, Any]) -> None:
        self._training = False
        self.btn_train.configure(state="normal")
        self.btn_demo.configure(state="normal")
        self.btn_ai.configure(state="normal")
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
        self.progress.stop()
        if error:
            self.lbl_status.configure(text="训练失败", foreground=self._colors['error'])
            self._append("训练失败：" + error)
            messagebox.showerror("训练失败", error, parent=self.root)
            return
        assert result is not None
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
        if self._training or self._calibrating:
            messagebox.showinfo("任务运行中", "请等待训练或校准完成后关闭工作台。", parent=self.root)
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
    try:
        build_demo_csv(ns.demos, Path(ns.demo_csv), ns.tick_offset,
                       horizon=ns.horizon_ticks, step=ns.step_ticks)
        if runtime is not None:
            runtime.capture_training_file(Path(ns.demo_csv), "offline")

        train_from_csv(
            Path(ns.demo_csv), Path(ns.out), Path(ns.metrics),
            min_samples=ns.min_samples, test_size=ns.test_size,
            trees=ns.trees, max_depth=ns.max_depth,
        )
    except Exception as e:  # noqa: BLE001
        print(f"失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    # PyInstaller onefile 下如果仍被间接使用 multiprocessing，先注册 freeze_support。
    import multiprocessing
    multiprocessing.freeze_support()
    # 便于自动化/打包后验证：Yuzi_CS2_AIDecisionTrainer.exe --cli ...
    if "--cli" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--cli"]
        from account_client import default_account
        from training_data_runtime import TrainingDataRuntime
        account = default_account()
        account.start()
        runtime = TrainingDataRuntime(identity_provider=account.upload_identity,binding_provider=account.binding)
        try:
            if "--csv" in args and "--calibrate" not in args:
                selected=Path(args[args.index("--csv")+1])
                if runtime.capture_training_file(selected,"online") is None:print(runtime.status_text())
            if "--calibrate" in args:
                from decision_calibration import main as cal_main
                raise SystemExit(cal_main([a for a in args if a != "--calibrate"]))
            if "--ai" in args:
                from ai_trainer import main as ai_main
                raise SystemExit(ai_main([a for a in args if a != "--ai"]))
            if "--demos" in args:
                raise SystemExit(_demo_cli(args,runtime))
            from train_decision_model import main as cli_main
            raise SystemExit(cli_main(args))
        finally:
            runtime.close();account.close()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app=TrainerApp()
    if '--client-update-health' in sys.argv:
        from client_update_install import health_ready
        try:health_ready(app,Path(sys.argv[sys.argv.index('--client-update-health')+1]))
        except (OSError,ValueError,IndexError,KeyError):app._on_close()
    app.run()


if __name__ == "__main__":
    main()
