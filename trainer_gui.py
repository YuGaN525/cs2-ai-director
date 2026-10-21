#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CS2 AI 决策模型训练器：独立 GUI，读取 decisions.csv 训练随机森林。"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Optional

from decision_model import decision_csv_path, decision_model_path
from train_decision_model import TrainingError, train_from_csv

VERSION = "1.0.0"


class TrainerApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(f"CS2 AI 决策模型训练器 v{VERSION}")
        self.root.geometry("820x650")
        self.root.minsize(720, 560)
        self._training = False

        self.var_csv = tk.StringVar(value=str(decision_csv_path()))
        self.var_out_dir = tk.StringVar(value=str(decision_model_path().parent))
        self.var_min = tk.StringVar(value="1000")
        self.var_trees = tk.StringVar(value="300")
        self.var_depth = tk.StringVar(value="8")
        self.var_test = tk.StringVar(value="0.25")

        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
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

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 6))
        self.btn_train = ttk.Button(actions, text="开始训练", command=self._start)
        self.btn_train.pack(side="left")
        ttk.Button(actions, text="打开输出目录", command=self._open_out).pack(
            side="left", padx=8)
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=220)
        self.progress.pack(side="left", padx=10)
        self.lbl_status = ttk.Label(actions, text="就绪", foreground="#555555")
        self.lbl_status.pack(side="left", padx=6)

        ttk.Label(
            outer,
            text="训练完成后，导播会检测到 decision_model.json.gz 更新并自动重新加载；"
                 "样本少于最少样本时不会训练。",
            foreground="#555555", wraplength=760, justify="left",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(0, 6))

        self.log = scrolledtext.ScrolledText(
            outer, height=18, wrap="word", font=("Consolas", 9))
        self.log.grid(row=5, column=0, columnspan=3, sticky="nsew")
        self.log.configure(state="disabled")
        outer.rowconfigure(5, weight=1)

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

    def _append(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _worker_log(self, text: str) -> None:
        self._events.put(("log", text))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "log":
                    self._append(str(payload))
                elif kind == "done":
                    self._done(payload, None)
                elif kind == "error":
                    self._done(None, str(payload))
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_events)

    def _start(self) -> None:
        if self._training:
            return
        try:
            min_samples = int(self.var_min.get().strip())
            trees = int(self.var_trees.get().strip())
            depth = int(self.var_depth.get().strip())
            test_size = float(self.var_test.get().strip())
            if not (0.1 <= test_size <= 0.5):
                raise ValueError("验证集比例需在 0.1–0.5")
        except ValueError as e:
            messagebox.showwarning("参数错误", str(e), parent=self.root)
            return
        csv_path = Path(self.var_csv.get().strip())
        if not csv_path.is_file():
            messagebox.showwarning("找不到 CSV", str(csv_path), parent=self.root)
            return
        out_dir = Path(self.var_out_dir.get().strip())
        out = out_dir / "decision_model.pkl"
        metrics = out_dir / "decision_model_metrics.json"

        self._training = True
        self.btn_train.configure(state="disabled")
        self.progress.start(10)
        self.lbl_status.configure(text="训练中…", foreground="#a15c00")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

        def worker() -> None:
            try:
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

    def _done(self, result: Optional[dict[str, Any]], error: Optional[str]) -> None:
        self._training = False
        self.btn_train.configure(state="normal")
        self.progress.stop()
        if error:
            self.lbl_status.configure(text="训练失败", foreground="#b00020")
            self._append("训练失败：" + error)
            messagebox.showerror("训练失败", error, parent=self.root)
            return
        assert result is not None
        self.lbl_status.configure(
            text=f"完成：AUC {result['auc']:.4f}，样本 {result['samples']}",
            foreground="#087f23",
        )
        self._append("")
        self._append(f"训练完成。AUC={result['auc']:.4f}，"
                     f"模型={result['model_json']}")
        messagebox.showinfo(
            "训练完成",
            f"AUC：{result['auc']:.4f}\n样本：{result['samples']}\n"
            f"导播会自动加载：\n{result['model_json']}",
            parent=self.root,
        )

    def _on_close(self) -> None:
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    # 便于自动化/打包后验证：CS2AIDecisionTrainer.exe --cli ...
    if "--cli" in sys.argv:
        from train_decision_model import main as cli_main
        args = [a for a in sys.argv[1:] if a != "--cli"]
        raise SystemExit(cli_main(args))
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    TrainerApp().run()


if __name__ == "__main__":
    main()
