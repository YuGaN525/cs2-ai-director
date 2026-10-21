#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回合开始/冷却阶段的低风险叙事文案。

只读取 GSI/StoryState，不改变镜头；可选推送到 OBS 文本源。
出生位只做“左右/中心”描述，不声称预测首交火。
"""

from __future__ import annotations

from typing import Any, Optional

ECONOMY = {"full": "全起", "force": "强起", "eco": "经济局", "unknown": "未知"}


def _pos(p: Any) -> Optional[tuple[float, float, float]]:
    pos = getattr(p, "pos", None)
    return pos if pos else None


def _centroid(players: list[Any]) -> Optional[tuple[float, float]]:
    pts = [_pos(p) for p in players]
    pts = [p for p in pts if p is not None]
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts))


def _side(team: str) -> str:
    return "CT" if str(team).upper() == "CT" else "T"


def _left_right(team_xy: Optional[tuple[float, float]],
                center_xy: Optional[tuple[float, float]]) -> str:
    if not team_xy or not center_xy:
        return ""
    return "左" if team_xy[0] < center_xy[0] else "右"


def freeze_text(snap: Any, story: Any, last_winner: str = "") -> str:
    """生成 freeze 阶段的叙事行；没有位置时仍返回比分/经济。"""
    players = [p for p in (getattr(snap, "players", {}) or {}).values()
               if int(getattr(p, "health", 0) or 0) > 0
               and str(getattr(p, "team", "")).upper() in ("CT", "T")]
    ct = [p for p in players if str(getattr(p, "team", "")).upper() == "CT"]
    t = [p for p in players if str(getattr(p, "team", "")).upper() == "T"]
    center = _centroid(players)
    ct_xy = _centroid(ct)
    t_xy = _centroid(t)
    parts = []
    rnd = getattr(story, "round_num", None) or getattr(snap, "round_num", None)
    if rnd:
        parts.append(f"第{rnd}回合")
    parts.append(f"CT {int(getattr(story, 'score_ct', 0) or 0)}:"
                 f"{int(getattr(story, 'score_t', 0) or 0)} T")
    eco_ct = ECONOMY.get(str(getattr(story, "economy_ct", "unknown")), "未知")
    eco_t = ECONOMY.get(str(getattr(story, "economy_t", "unknown")), "未知")
    if eco_ct != "未知" or eco_t != "未知":
        parts.append(f"经济 CT{eco_ct}/T{eco_t}")
    if last_winner in ("CT", "T"):
        parts.append(f"上回合{last_winner}胜")
    lr_ct = _left_right(ct_xy, center)
    lr_t = _left_right(t_xy, center)
    if lr_ct and lr_t:
        parts.append(f"出生点 CT偏{lr_ct}/T偏{lr_t}（参考）")
    if getattr(story, "match_point", False):
        parts.append(f"赛点 {getattr(story, 'match_point_team', '')}")
    if getattr(story, "clutch", ""):
        parts.append(f"残局 {getattr(story, 'clutch', '')}")
    return " · ".join(parts)


def round_end_text(snap: Any, story: Any, last_winner: str = "") -> str:
    winner = str(getattr(snap, "round_win_team", "") or last_winner or "").upper()
    if winner not in ("CT", "T"):
        winner = ""
    parts = ["回合结束"]
    if winner:
        parts.append(f"{winner} 胜")
    parts.append(f"比分 CT {int(getattr(story, 'score_ct', 0) or 0)}:"
                 f"{int(getattr(story, 'score_t', 0) or 0)} T")
    if str(getattr(snap, "phase", "")).lower() == "gameover":
        parts.append("比赛结束")
    else:
        parts.append("下一回合待开始")
    return " · ".join(parts)
