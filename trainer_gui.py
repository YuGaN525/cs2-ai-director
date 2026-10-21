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
        self.root.geometry("860x780")
        self.root.minsize(760, 680)
        self._training = False

        self.var_csv = tk.StringVar(value=str(decision_csv_path()))
        self.var_out_dir = tk.StringVar(value=str(decision_model_path().parent))
        self.var_demos = tk.StringVar()
        self.var_tick = tk.StringVar(value="160")
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

        demo = ttk.LabelFrame(outer, text=" 从 .dem 导入（可选） ", padding=8)
        demo.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(4, 6))
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
        ttk.Label(row, text="回看 tick").pack(side="left")
        ttk.Entry(row, textvariable=self.var_tick, width=8).pack(side="left", padx=4)
        self.btn_demo = ttk.Button(row, text="解析 demo 并训练",
                                   command=self._start_demo)
        self.btn_demo.pack(side="left", padx=8)
        ttk.Label(row, text="64tick 下 160≈2.5 秒", foreground="#777777").pack(
            side="left")

        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(4, 6))
        self.btn_train = ttk.Button(actions, text="用 CSV 开始训练", command=self._start)
        self.btn_train.pack(side="left")
        ttk.Button(actions, text="打开输出目录", command=self._open_out).pack(
            side="left", padx=8)
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=200)
        self.progress.pack(side="left", padx=10)
        self.lbl_status = ttk.Label(actions, text="就绪", foreground="#555555")
        self.lbl_status.pack(side="left", padx=6)

        ttk.Label(
            outer,
            text="训练完成后，导播会检测到 decision_model.json.gz 更新并自动重新加载；"
                 "样本少于最少样本时不会训练。",
            foreground="#555555", wraplength=800, justify="left",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 6))

        self.log = scrolledtext.ScrolledText(
            outer, height=16, wrap="word", font=("Consolas", 9))
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew")
        self.log.configure(state="disabled")
        outer.rowconfigure(6, weight=1)

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
        self.progress.start(10)
        self.lbl_status.configure(text=message, foreground="#a15c00")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _run_worker(self, csv_path: Path, out: Path, metrics: Path,
                    params: tuple[int, int, int, float],
                    demo_paths: Optional[list[str]] = None,
                    tick_offset: int = 160) -> None:
        min_samples, trees, depth, test_size = params

        def worker() -> None:
            try:
                if demo_paths:
                    from demo_dataset import build_demo_csv
                    self._worker_log("开始解析 demo …")
                    build_demo_csv(demo_paths, csv_path, tick_offset,
                                   log=self._worker_log)
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
            tick_offset = max(1, int(self.var_tick.get().strip()))
        except ValueError:
            messagebox.showwarning("参数错误", "回看 tick 必须是正整数。",
                                   parent=self.root)
            return
        out_dir = Path(self.var_out_dir.get().strip())
        out_dir.mkdir(parents=True, exist_ok=True)
        self._begin_training("解析 demo 并训练中…")
        self._run_worker(
            out_dir / "demo_decisions.csv",
            out_dir / "decision_model.pkl",
            out_dir / "decision_model_metrics.json",
            params, demo_paths=demos, tick_offset=tick_offset,
        )

    def _done(self, result: Optional[dict[str, Any]], error: Optional[str]) -> None:
        self._training = False
        self.btn_train.configure(state="normal")
        self.btn_demo.configure(state="normal")
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


def _demo_cli(args: list[str]) -> int:
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
    parser.add_argument("--tick-offset", type=int, default=160)
    ns = parser.parse_args(args)
    try:
        build_demo_csv(ns.demos, Path(ns.demo_csv), ns.tick_offset)
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
    # 便于自动化/打包后验证：CS2AIDecisionTrainer.exe --cli ...
    if "--cli" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--cli"]
        if "--demos" in args:
            raise SystemExit(_demo_cli(args))
        from train_decision_model import main as cli_main
        raise SystemExit(cli_main(args))
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    TrainerApp().run()


if __name__ == "__main__":
    main()
