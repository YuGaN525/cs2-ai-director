#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导播模型/配置的一键导出与导入。

归档内容固定，防止 zip-slip；导入前自动备份，写入使用临时文件替换。
"""

from __future__ import annotations

import json
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

FORMAT = "yuzi-cs2-director-backup"
VERSION = 1
MANIFEST = "manifest.json"
CONFIG = "config.json"
ARTIFACTS = (
    "decision_model.pkl",
    "decision_model.json.gz",
    "decision_model_metrics.json",
    "decision_calibration.json",
    "learner.json",
)
MAX_TOTAL_BYTES = 300 * 1024 * 1024


class BundleError(Exception):
    pass


def _safe_name(name: str) -> str:
    p = Path(name)
    if p.is_absolute() or ".." in p.parts or len(p.parts) != 1:
        raise BundleError(f"非法归档路径：{name}")
    return p.name


def export_bundle(dest: Path, cfg: dict[str, Any], data_dir: Path,
                  app_version: str, include_secrets: bool = False) -> dict[str, Any]:
    dest = Path(dest)
    data_dir = Path(data_dir)
    payload = dict(cfg)
    if not include_secrets:
        payload["deepseek_api_key"] = ""
        payload["obs_password"] = ""
    files = [CONFIG]
    for name in ARTIFACTS:
        if (data_dir / name).is_file():
            files.append(name)
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app_version": app_version,
        "secrets_included": bool(include_secrets),
        "files": files,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
            z.writestr(CONFIG, json.dumps(payload, ensure_ascii=False, indent=2))
            for name in files:
                if name == CONFIG:
                    continue
                z.write(data_dir / name, name)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return {"path": str(dest), "files": files, "manifest": manifest}


def inspect_bundle(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise BundleError(f"文件不存在：{path}")
    try:
        with zipfile.ZipFile(path, "r") as z:
            infos = z.infolist()
            total = sum(max(0, i.file_size) for i in infos)
            if total > MAX_TOTAL_BYTES:
                raise BundleError(f"归档解压后过大：{total} 字节")
            names = []
            for info in infos:
                name = _safe_name(info.filename)
                if name != MANIFEST and name != CONFIG and name not in ARTIFACTS:
                    raise BundleError(f"归档包含未知文件：{name}")
                names.append(name)
            if MANIFEST not in names or CONFIG not in names:
                raise BundleError("归档缺少 manifest.json 或 config.json")
            manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
            if manifest.get("format") != FORMAT:
                raise BundleError("不是 Yuzi 导播备份")
            if int(manifest.get("version", 0)) > VERSION:
                raise BundleError(f"备份版本过新：{manifest.get('version')}")
            cfg = json.loads(z.read(CONFIG).decode("utf-8"))
            if not isinstance(cfg, dict):
                raise BundleError("config.json 不是对象")
            return {"manifest": manifest, "config": cfg, "files": names,
                    "path": str(path)}
    except zipfile.BadZipFile as e:
        raise BundleError(f"不是有效 ZIP：{e}") from e
    except BundleError:
        raise
    except Exception as e:
        raise BundleError(f"读取备份失败：{type(e).__name__}: {e}") from e


def import_bundle(path: Path, data_dir: Path) -> dict[str, Any]:
    path = Path(path)
    data_dir = Path(data_dir)
    info = inspect_bundle(path)
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = data_dir / f"backup_import_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for name in ARTIFACTS:
        src = data_dir / name
        if src.is_file():
            shutil.copy2(src, backup_dir / name)
    if (data_dir / CONFIG).is_file():
        shutil.copy2(data_dir / CONFIG, backup_dir / CONFIG)
    with zipfile.ZipFile(path, "r") as z:
        payload = {_safe_name(i.filename): z.read(i.filename)
                   for i in z.infolist()}
    for name, raw in payload.items():
        if name == MANIFEST:
            continue
        target = data_dir / name
        tmp = target.with_name(target.name + ".importtmp")
        tmp.write_bytes(raw)
        tmp.replace(target)
    # 精确恢复：归档里没有的模型/校准文件不能留下旧版本，
    # 否则 ModelArbiter 会优先加载旧的 json.gz。
    for name in ARTIFACTS:
        if name not in payload:
            (data_dir / name).unlink(missing_ok=True)
    # 模型文件时间会变，重新绑定校准，否则 ModelArbiter 会把它当旧模型校准。
    cal_path = data_dir / "decision_calibration.json"
    model_path = data_dir / "decision_model.pkl"
    if cal_path.is_file() and model_path.is_file():
        try:
            cal = json.loads(cal_path.read_text(encoding="utf-8"))
            cal["model_mtime"] = model_path.stat().st_mtime
            cal_path.write_text(json.dumps(cal, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception:
            pass
    return {"backup_dir": str(backup_dir), "files": info["files"],
            "manifest": info["manifest"], "config": info["config"]}
