#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 辅助训练：DeepSeek 训练顾问 + 本地验证。

流程：
1. 对 decisions.csv 做聚合画像（不读取 SteamID/昵称）；
2. 可选调用 DeepSeek 给出诊断与搜索空间建议；
3. 先用默认参数训练基线，再用 AI 建议（或本地扩展搜索）训练候选；
4. 只有验证集指标提升且模型体积可控才采用候选，否则保留基线；
5. 写出 ai_training_report.json，记录画像、建议、两组指标与采用原因。

任何 AI 调用失败都自动回退本地搜索，不阻塞训练；训练与导出仍是纯
scikit-learn 树森林 + 纯 JSON，不引入新的运行时依赖。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from decision_model import DECISION_FEATURES
from train_decision_model import (
    TrainingError,
    load_dataset,
    normalize_search_space,
    train_from_csv,
)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
MAX_JSON_MB = 20.0
MIN_JSON_BUDGET_MB = 5.0
MAX_PROMPT_FEATURES = 12


class AdviceError(Exception):
    """AI 建议不可用或格式非法。"""


@dataclass
class AIConfig:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    source: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key.strip())


@dataclass
class Advice:
    diagnosis: list[str] = field(default_factory=list)
    data_risks: list[str] = field(default_factory=list)
    search_space: dict[str, list[Any]] = field(default_factory=dict)
    weight_boost: dict[str, float] = field(default_factory=dict)
    n_iter: int = 8
    split_mode: str = "auto"
    notes: str = ""


def director_config_path() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home()
    return root / "CS2AIDirector" / "config.json"


def load_ai_config(path: Optional[Path] = None,
                   env: Optional[dict[str, str]] = None) -> AIConfig:
    """读取导播配置里的 DeepSeek Key；环境变量优先，便于 CI/离线覆盖。"""
    env = env if env is not None else os.environ
    cfg = AIConfig()
    cfg_path = Path(path) if path else director_config_path()
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        cfg.api_key = str(data.get("deepseek_api_key", "") or "").strip()
        cfg.base_url = (str(data.get("deepseek_base_url", "") or "").strip()
                        or DEFAULT_BASE_URL)
        cfg.model = (str(data.get("deepseek_model", "") or "").strip()
                     or DEFAULT_MODEL)
        cfg.source = str(cfg_path) if cfg.api_key else ""
    env_key = str(env.get("DEEPSEEK_API_KEY", "") or "").strip()
    if env_key:
        cfg.api_key = env_key
        cfg.source = "环境变量"
    cfg.base_url = (str(env.get("DEEPSEEK_BASE_URL", "") or "").strip()
                    or cfg.base_url)
    cfg.model = (str(env.get("DEEPSEEK_MODEL", "") or "").strip()
                 or cfg.model)
    return cfg


def _safe_name(value: Any, limit: int = 32) -> str:
    text = re.sub(r"[^A-Za-z0-9_\-]", "", str(value or ""))[:limit]
    return text or "unknown"


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _fmt_ts(value: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value)))
    except (OverflowError, OSError, ValueError):
        return "?"


def profile_dataset(csv_path: Path) -> dict[str, Any]:
    """只生成聚合统计：样本量、类别、分组、地图、特征区分度。

    明确不读取 target_name / target_steamid，避免把选手身份发送给外部服务。
    """
    xs, ys, groups, _weights, group_sizes, stats, times = load_dataset(csv_path)
    if not ys:
        raise TrainingError("CSV 没有有效样本，无法生成数据画像。")

    aucs: dict[str, Optional[float]] = {name: None for name in DECISION_FEATURES}
    means: dict[str, dict[str, float]] = {}
    try:
        from sklearn.metrics import roc_auc_score
        for i, name in enumerate(DECISION_FEATURES):
            column = [row[i] for row in xs]
            pos = [v for v, y in zip(column, ys) if y]
            neg = [v for v, y in zip(column, ys) if not y]
            means[name] = {"pos": round(_mean(pos), 4), "neg": round(_mean(neg), 4)}
            if len(set(ys)) >= 2:
                aucs[name] = round(float(roc_auc_score(ys, column)), 4)
    except Exception:
        pass
    ranked = sorted(
        ((name, value) for name, value in aucs.items() if value is not None),
        key=lambda item: abs(item[1] - 0.5), reverse=True,
    )[:MAX_PROMPT_FEATURES]

    maps: dict[str, dict[str, int]] = {}
    group_set: set[str] = set()
    try:
        with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if str(row.get("label", "")).strip() not in ("0", "1"):
                    continue
                name = _safe_name(row.get("map", ""))
                bucket = maps.setdefault(name, {"rows": 0, "positive": 0})
                bucket["rows"] += 1
                bucket["positive"] += 1 if str(row.get("label")) == "1" else 0
                if str(row.get("group", "") or "").strip():
                    group_set.add(str(row.get("group")).strip())
    except OSError:
        pass

    size_buckets = {"1": 0, "2-4": 0, "5+": 0}
    for size in group_sizes:
        if size <= 1:
            size_buckets["1"] += 1
        elif size <= 4:
            size_buckets["2-4"] += 1
        else:
            size_buckets["5+"] += 1

    span = (max(times) - min(times)) if times else 0.0
    return {
        "rows": stats["rows"],
        "valid": len(ys),
        "skipped": stats["skipped"],
        "positive": stats["positive"],
        "negative": stats["negative"],
        "positive_rate": round(stats["positive"] / len(ys), 4),
        "ambiguous": stats["ambiguous"],
        "ambiguous_rate": round(stats["ambiguous"] / len(ys), 4),
        "switches": stats["switches"],
        "groups": len(group_set) or len(set(groups)),
        "group_size_buckets": size_buckets,
        "maps": maps,
        "time_span": {
            "start": _fmt_ts(min(times)) if times else "?",
            "end": _fmt_ts(max(times)) if times else "?",
            "hours": round(span / 3600.0, 2),
        },
        "feature_auc": dict(ranked),
        "feature_means": means,
    }


def _metrics_summary(metrics: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not metrics:
        return {}
    keys = ("samples", "positive_rate", "auc", "top1", "top1_groups",
            "split", "split_hash", "dataset_hash", "algorithm", "model_nodes", "model_json_bytes", "n_iter")
    out = {key: metrics.get(key) for key in keys if key in metrics}
    if "model_json_bytes" in out:
        out["model_json_mb"] = round(
            float(out.pop("model_json_bytes") or 0) / (1024 * 1024), 2)
    return out


def build_advice_prompt(profile: dict[str, Any],
                        metrics: Optional[dict[str, Any]] = None) -> str:
    """构造只含聚合信息的提示词；不包含选手昵称/SteamID/逐行数据。"""
    schema = {
        "diagnosis": ["一句话诊断，最多 4 条"],
        "data_risks": ["数据风险，最多 4 条"],
        "search_space": {
            "n_estimators": [200, 400],
            "max_depth": [6, 10, None],
            "min_samples_leaf": [2, 5, 10],
            "max_features": ["sqrt", 0.5],
            "class_weight": ["balanced", "balanced_subsample"],
        },
        "weight_boost": {"ambiguous": 2.0, "switch": 0.25, "gap": 0.75},
        "n_iter": 12,
        "split_mode": "auto",
        "notes": "不超过 120 字",
    }
    return "\n".join([
        "你是 CS2 观战导播行为克隆模型的训练顾问。",
        "模型是 scikit-learn RandomForest/ExtraTrees，输入固定 8 个候选特征："
        + ", ".join(DECISION_FEATURES) + "。",
        "目标不是提高击杀预测 AUC，而是提升导播实际 kill_coverage；"
        "但当前只能用验证集 AUC/top-1 与模型体积做离线代理指标。",
        "数据来自导播运行日志，标签是 3 秒内目标是否造成伤害/击杀；"
        "模型只在规则前两名接近时接管。",
        "约束：不能新增特征或标签，不能改用深度学习，不能引入新依赖；"
        "导出 JSON 必须 ≤20MB；搜索空间每个列表最多 5 个值；n_iter 4-24；"
        "split_mode 只能是 auto/group/time/random。",
        "",
        "数据画像：",
        json.dumps(profile, ensure_ascii=False, separators=(",", ":")),
        "",
        "当前指标：",
        json.dumps(_metrics_summary(metrics), ensure_ascii=False,
                   separators=(",", ":")) if metrics else "尚未训练",
        "",
        "请只输出一个 JSON 对象，不要 Markdown、不要解释，结构如下：",
        json.dumps(schema, ensure_ascii=False),
    ])


def _http_json(cfg: AIConfig, prompt: str, timeout: float) -> str:
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system",
             "content": "你是严谨的机器学习训练顾问，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1400,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    url = cfg.base_url.rstrip("/") + "/chat/completions"

    def post(payload_obj: dict[str, Any]) -> str:
        body_bytes = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body_bytes, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.api_key}",
                "User-Agent": "Yuzi_CS2_AIDecisionTrainer/1.2",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    body = ""
    last_http: Optional[urllib.error.HTTPError] = None
    for attempt in range(2):
        try:
            body = post(payload)
            break
        except urllib.error.HTTPError as e:
            last_http = e
            # 部分兼容端点不支持 response_format，去掉后重试一次。
            if attempt == 0 and e.code in (400, 404, 422):
                payload.pop("response_format", None)
                continue
            break
        except urllib.error.URLError as e:
            raise AdviceError(f"DeepSeek 连接失败：{e.reason}")
        except TimeoutError:
            raise AdviceError("DeepSeek 请求超时")
    if not body:
        detail = ""
        if last_http is not None:
            try:
                raw = last_http.read().decode("utf-8", errors="replace")
                detail = str(json.loads(raw).get("error", {}).get("message", ""))[:200]
            except Exception:
                detail = ""
            raise AdviceError(
                f"DeepSeek HTTP {last_http.code}"
                + (f"：{detail}" if detail else ""))
        raise AdviceError("DeepSeek 无响应")
    try:
        obj = json.loads(body)
        return str(obj["choices"][0]["message"]["content"])
    except (ValueError, KeyError, IndexError, TypeError):
        raise AdviceError("DeepSeek 响应格式异常")


def _clean_list(value: Any, limit: int = 4, width: int = 160) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        text = str(item).strip().replace("\n", " ")
        if text:
            out.append(text[:width])
    return out


def parse_advice(text: str) -> Advice:
    raw = (text or "").strip()
    if not raw:
        raise AdviceError("AI 返回空内容")
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    obj: Any = None
    try:
        obj = json.loads(raw)
    except ValueError:
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            try:
                obj = json.loads(match.group(0))
            except ValueError:
                obj = None
    if not isinstance(obj, dict):
        raise AdviceError("AI 返回不是 JSON 对象")

    space = normalize_search_space(obj.get("search_space"))
    boost = obj.get("weight_boost") if isinstance(obj.get("weight_boost"), dict) else {}
    try:
        n_iter = int(float(obj.get("n_iter", 8)))
    except (TypeError, ValueError):
        n_iter = 8
    split = str(obj.get("split_mode", "auto")).strip().lower()
    if split not in ("auto", "group", "time", "random"):
        split = "auto"
    advice = Advice(
        diagnosis=_clean_list(obj.get("diagnosis")),
        data_risks=_clean_list(obj.get("data_risks")),
        search_space=space,
        weight_boost={
            "ambiguous": _clamp(boost.get("ambiguous"), 0.0, 5.0, 2.0),
            "switch": _clamp(boost.get("switch"), 0.0, 1.5, 0.25),
            "gap": _clamp(boost.get("gap"), 0.0, 1.5, 0.75),
        },
        n_iter=max(4, min(24, n_iter)),
        split_mode=split,
        notes=str(obj.get("notes", ""))[:400],
    )
    if not (advice.diagnosis or advice.data_risks or advice.notes):
        raise AdviceError("AI 建议为空")
    return advice


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def ask_deepseek(profile: dict[str, Any],
                 cfg: Optional[AIConfig] = None,
                 metrics: Optional[dict[str, Any]] = None,
                 timeout: float = 45.0,
                 log: Optional[Callable[[str], None]] = None) -> Advice:
    emit = log or (lambda _text: None)
    cfg = cfg or load_ai_config()
    if not cfg.enabled:
        raise AdviceError("未配置 DeepSeek API Key")
    prompt = build_advice_prompt(profile, metrics)
    started = time.time()
    text = _http_json(cfg, prompt, timeout)
    emit(f"DeepSeek 顾问返回（{time.time() - started:.1f}s，{cfg.model}）")
    return parse_advice(text)


def local_search_space(trees: int = 300, max_depth: int = 8) -> dict[str, list[Any]]:
    """无 AI 时使用的扩展搜索空间，仍受体积与参数上限约束。"""
    return normalize_search_space({
        "n_estimators": sorted({max(50, min(600, int(trees))),
                                max(50, min(600, int(trees) * 2))}),
        "max_depth": [max(2, int(max_depth) - 2), int(max_depth),
                      min(24, int(max_depth) + 2), None],
        "min_samples_leaf": [2, 3, 5, 10],
        "max_features": ["sqrt", 0.5, 0.8],
        "class_weight": ["balanced", "balanced_subsample"],
    }, trees, max_depth)


def choose_result(baseline: dict[str, Any],
                  candidate: dict[str, Any]) -> tuple[str, list[str]]:
    """守卫：只有候选确有提升、且没有明显退步/体积超标时才采用。"""
    if not baseline.get("split_hash") or baseline.get("split_hash") != candidate.get("split_hash"):
        raise TrainingError("基线与候选的冻结切分不一致，拒绝比较")
    base_auc = float(baseline.get("auc") or 0.0)
    cand_auc = float(candidate.get("auc") or 0.0)
    base_top1 = float(baseline.get("top1") or 0.0)
    cand_top1 = float(candidate.get("top1") or 0.0)
    base_mb = float(baseline.get("model_json_bytes") or 0) / (1024 * 1024)
    cand_mb = float(candidate.get("model_json_bytes") or 0) / (1024 * 1024)
    auc_gain = cand_auc - base_auc
    top1_gain = cand_top1 - base_top1
    improved = auc_gain >= 0.002 or top1_gain >= 0.01
    size_ok = cand_mb <= MAX_JSON_MB and cand_mb <= max(MIN_JSON_BUDGET_MB,
                                                        base_mb * 1.5)
    not_worse = cand_auc >= base_auc - 0.005 and cand_top1 >= base_top1 - 0.02
    reasons = [
        f"AUC {base_auc:.4f} → {cand_auc:.4f}（{auc_gain:+.4f}）",
        f"top-1 {base_top1:.1%} → {cand_top1:.1%}（{top1_gain:+.1%}）",
        f"JSON {base_mb:.2f}MB → {cand_mb:.2f}MB",
    ]
    if not improved:
        reasons.append("候选未达到提升门槛（AUC +0.2pp 或 top-1 +1pp）")
    if not size_ok:
        reasons.append(f"候选体积超出预算（≤{MAX_JSON_MB:.0f}MB 且 ≤基线 1.5 倍）")
    if not not_worse:
        reasons.append("候选至少一项指标明显退步（AUC -0.5pp / top-1 -2pp）")
    accept = improved and size_ok and not_worse
    reasons.append("采用候选" if accept else "保留基线")
    return ("candidate" if accept else "baseline"), reasons


def _publish_artifact(source: Path, destination: Path) -> None:
    # Stage on the same volume so the inference reader sees a complete old/new file.
    handle, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=".model_")
    try:
        with os.fdopen(handle, "wb") as stream, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, stream)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_metrics_with_ai(metrics_path: Path, metrics: dict[str, Any],
                           report: dict[str, Any]) -> None:
    payload = dict(metrics)
    outputs = report.get("outputs") or {}
    payload["model"] = outputs.get("model", payload.get("model"))
    payload["model_json"] = outputs.get("model_json", payload.get("model_json"))
    payload["csv"] = report.get("csv", payload.get("csv"))
    payload["ai_training"] = {
        "decision": report["decision"],
        "adopted": report["adopted"],
        "ai_used": report["ai"]["used"],
        "reasons": report["reasons"],
        "report": report["report_path"],
    }
    metrics_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def budget_space(space: dict[str, list[Any]], samples: int,
                 iterations: int) -> tuple[dict[str, list[Any]], int, list[str]]:
    """按数据量压搜索成本，避免 AI 给出的大模型把训练拖到几十分钟。"""
    if samples >= 50000:
        max_trees, max_depth, max_iter = 300, 18, 8
    elif samples >= 20000:
        max_trees, max_depth, max_iter = 350, 20, 12
    else:
        max_trees, max_depth, max_iter = 600, 24, 16
    notes: list[str] = []
    out = {key: list(values) for key, values in space.items()}
    trees = [v for v in out.get("n_estimators", [])
             if isinstance(v, int)]
    capped = [v for v in trees if v <= max_trees]
    if not capped:
        capped = [max_trees if trees else min(max_trees, 300)]
    if capped != out.get("n_estimators"):
        notes.append(f"树数量限制到 ≤{max_trees}")
    out["n_estimators"] = capped
    depths = []
    for value in out.get("max_depth", []):
        if value is None:
            depths.append(None)
        elif isinstance(value, int):
            depths.append(min(value, max_depth))
    if depths and depths != out.get("max_depth"):
        notes.append(f"深度限制到 ≤{max_depth}")
    if depths:
        out["max_depth"] = depths
    budget_iters = max(1, min(int(iterations), max_iter))
    if budget_iters != iterations:
        notes.append(f"搜索组数限制到 {budget_iters}")
    return out, budget_iters, notes


def run_ai_training(
    csv_path: Path,
    out_dir: Path,
    metrics_path: Optional[Path] = None,
    min_samples: int = 1000,
    test_size: float = 0.25,
    trees: int = 300,
    max_depth: int = 8,
    use_ai: bool = True,
    timeout: float = 45.0,
    search_iters: Optional[int] = None,
    advisor: Optional[Callable[[dict[str, Any]], Advice]] = None,
    ai_config: Optional[AIConfig] = None,
    log: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """训练基线 + AI/本地候选，按守卫选择最终模型并写报告。"""
    emit = log or print
    csv_path = Path(csv_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = Path(metrics_path) if metrics_path else (
        out_dir / "decision_model_metrics.json")
    report_path = out_dir / "ai_training_report.json"

    from training_splits import make_plan
    xs, ys, groups, _, sizes, _, times = load_dataset(csv_path)
    plan = make_plan(xs, ys, [g.split("|event:", 1)[0] for g in groups], times, validation_size=test_size)
    # The advisor only sees the training partition, including aggregate statistics.
    with tempfile.TemporaryDirectory(prefix="director_profile_") as profile_tmp:
        profile_csv = Path(profile_tmp) / "training.csv"
        with profile_csv.open("w", newline="", encoding="utf-8") as stream:
            fields = list(DECISION_FEATURES) + ["label", "group", "group_size", "timestamp"]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for i in plan.train:
                writer.writerow(dict(zip(DECISION_FEATURES, xs[i]), label=ys[i], group=groups[i],
                                     group_size=sizes[i], timestamp=times[i]))
        profile = profile_dataset(profile_csv)
    profile["scope"] = "training-partition-only"
    emit(f"数据画像：有效 {profile['valid']} 行，正样本率 "
         f"{profile['positive_rate']:.1%}，模糊样本 "
         f"{profile['ambiguous_rate']:.1%}，{profile['groups']} 组")
    ai_info: dict[str, Any] = {"used": False, "model": "", "source": "",
                               "error": "", "latency": 0.0}
    advice: Optional[Advice] = None
    if use_ai:
        cfg = ai_config or load_ai_config()
        if advisor is None and not cfg.enabled:
            ai_info["error"] = "未找到 DeepSeek API Key"
            emit("未找到 DeepSeek Key，回退本地扩展搜索")
        else:
            ai_info.update(model=cfg.model or "injected",
                           source=cfg.source or "注入建议")
            started = time.time()
            try:
                if advisor is not None:
                    advice = advisor(profile)
                else:
                    advice = ask_deepseek(profile, cfg, timeout=timeout, log=emit)
                ai_info["used"] = True
                ai_info["latency"] = round(time.time() - started, 2)
                for line in advice.diagnosis:
                    emit(f"AI 诊断：{line}")
                for line in advice.data_risks:
                    emit(f"AI 风险：{line}")
            except Exception as e:  # noqa: BLE001
                ai_info["error"] = f"{type(e).__name__}: {e}"
                emit(f"AI 顾问不可用，回退本地搜索：{ai_info['error']}")

    with tempfile.TemporaryDirectory(dir=out_dir, prefix=".ai_tmp_") as tmp:
        tmp_path = Path(tmp)
        base_dir = tmp_path / "baseline"
        cand_dir = tmp_path / "candidate"
        emit("训练基线（默认参数）…")
        baseline = train_from_csv(
            csv_path, base_dir / "decision_model.pkl", base_dir / "metrics.json",
            min_samples=min_samples, test_size=test_size,
            trees=trees, max_depth=max_depth, log=emit, split_plan=plan, evaluate_final=False,
        )
        if advice is not None:
            label = "AI"
            space = advice.search_space
            iterations = search_iters or advice.n_iter
            boost = advice.weight_boost
            split = advice.split_mode
        else:
            label = "本地"
            space = local_search_space(trees, max_depth)
            iterations = search_iters or 16
            boost = None
            split = "auto"
        space, iterations, budget_notes = budget_space(
            space, int(profile["valid"]), int(iterations))
        if budget_notes:
            emit("搜索预算：" + "；".join(budget_notes))
        emit(f"训练{label}候选…")
        candidate = train_from_csv(
            csv_path, cand_dir / "decision_model.pkl", cand_dir / "metrics.json",
            min_samples=min_samples, test_size=test_size,
            trees=trees, max_depth=max_depth, log=emit,
            search_space=space, n_iter=iterations,
            weight_boost=boost, split_mode=split, split_plan=plan, evaluate_final=False,
        )
        decision, reasons = choose_result(baseline, candidate)
        if advice is not None and advice.split_mode not in ("auto", plan.mode):
            reasons.append("AI切分建议仅记录；对比固定使用同一冻结切分")
        winner_dir = cand_dir if decision == "candidate" else base_dir
        final_pkl = out_dir / "decision_model.pkl"
        winner = candidate if decision == "candidate" else baseline
        _publish_artifact(winner_dir / "decision_model.pkl", final_pkl)
        _publish_artifact(winner_dir / "decision_model.json.gz",
                     final_pkl.with_suffix(".json.gz"))

    # Final test is evaluated once, after the winner has been selected.
    import joblib
    from sklearn.metrics import roc_auc_score
    selected_model = joblib.load(final_pkl)
    final_p = selected_model.predict_proba([xs[i] for i in plan.final_test])[:, 1]
    winner["final_test"] = {"auc": float(roc_auc_score([ys[i] for i in plan.final_test], final_p)),
                             "samples": len(plan.final_test), "selection_used": False}
    report: dict[str, Any] = {
        "split_hash": plan.split_hash,
        "final_test": winner["final_test"],
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "csv": str(csv_path),
        "profile": profile,
        "ai": ai_info,
        "advice": {
            "diagnosis": advice.diagnosis,
            "data_risks": advice.data_risks,
            "search_space": {k: [str(v) for v in vals]
                             for k, vals in advice.search_space.items()},
            "weight_boost": advice.weight_boost,
            "n_iter": advice.n_iter,
            "split_mode": advice.split_mode,
            "notes": advice.notes,
        } if advice else None,
        "baseline": _metrics_summary(baseline),
        "candidate": _metrics_summary(candidate),
        "decision": decision,
        "adopted": ("ai" if advice else "local") if decision == "candidate"
                   else "baseline",
        "reasons": reasons,
        "budget_notes": budget_notes,
        "outputs": {
            "model": str(out_dir / "decision_model.pkl"),
            "model_json": str(out_dir / "decision_model.json.gz"),
            "metrics": str(metrics_path),
        },
        "report_path": str(report_path),
        "privacy": "只发送聚合统计与指标，不发送 SteamID/昵称/逐行数据",
    }
    metrics_path.with_suffix(".split.json").write_text(json.dumps(plan.manifest(), indent=2), encoding="utf-8")
    _write_metrics_with_ai(metrics_path, winner, report)
    from model_submission import write_submission
    try:
        write_submission(out_dir / "decision_model.json.gz",[xs[i] for i in plan.final_test],[ys[i] for i in plan.final_test],plan.split_hash)
    except (ValueError,OSError) as error:
        emit(f"本地模型已保存，发布评估文件未生成：{error}")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return report


def _summary_text(report: dict[str, Any]) -> str:
    base = report["baseline"]
    cand = report["candidate"]
    verdict = {
        "baseline": "保留基线",
        "ai": "采用 AI 候选",
        "local": "采用本地搜索候选",
    }.get(report["adopted"], report["adopted"])
    lines = [
        f"结果：{verdict}",
        f"基线 AUC {float(base.get('auc') or 0):.4f} / "
        f"候选 AUC {float(cand.get('auc') or 0):.4f}",
        f"基线 JSON {float(base.get('model_json_mb') or 0):.2f}MB / "
        f"候选 JSON {float(cand.get('model_json_mb') or 0):.2f}MB",
    ]
    if report["ai"]["error"]:
        lines.append(f"AI 未使用：{report['ai']['error']}")
    lines.append(f"报告：{report['report_path']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AI 辅助训练决策模型")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--out", default="decision_model.pkl")
    parser.add_argument("--metrics", default="")
    parser.add_argument("--min-samples", type=int, default=1000)
    parser.add_argument("--test-size", type=float, default=0.25)
    parser.add_argument("--trees", type=int, default=300)
    parser.add_argument("--max-depth", type=int, default=8)
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--timeout", type=float, default=45.0)
    ns = parser.parse_args(argv)
    out_path = Path(ns.out)
    try:
        report = run_ai_training(
            Path(ns.csv), out_path.parent,
            Path(ns.metrics) if ns.metrics else None,
            min_samples=ns.min_samples, test_size=ns.test_size,
            trees=ns.trees, max_depth=ns.max_depth,
            use_ai=not ns.no_ai, timeout=ns.timeout,
        )
    except TrainingError as e:
        print(e, file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"训练失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(_summary_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
