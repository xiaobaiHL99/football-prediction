#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用规则引擎 — 让全局校准规则适配所有联赛，同时用约束防止误触发

设计目标
--------
1. **通用性**：规则默认对所有联赛生效，不再依赖硬编码白名单。
2. **证据门槛**：规则通过 `requires` 声明必须存在的结构化证据；证据不足时
   自动降级或直接不触发，绝不从文字描述猜测。
3. **强度缩放**：效果 = 规则基础强度 × 联赛样本强度 × 证据完整度。
   样本不足的联赛先以较低强度运行，累积样本后自动提升。
4. **硬上限守卫**：所有规则叠加后的总位移受全局上限约束（xG 总位移、
   单边概率转移、平局上下界），避免多条规则共振导致概率失真。

调用方只需提供三类输入：`league`、结构化 `evidence`、以及规则自身配置。
"""

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from league_data import load_global_rules, load_rule_engine_config  # noqa: E402

_GUARD_CACHE = None
_SAMPLE_CACHE = None


# ================================================================
# 配置与样本
# ================================================================

def guard_config() -> dict:
    """Return the rule-engine guard block with safe defaults."""
    global _GUARD_CACHE
    if _GUARD_CACHE is None:
        cfg = load_rule_engine_config()
        _GUARD_CACHE = {
            "enabled": cfg.get("enabled", True),
            "thin_sample_threshold": cfg.get("thin_sample_threshold", 15),
            "thin_sample_league_strength": cfg.get("thin_sample_league_strength", 0.7),
            "default_league_strength": cfg.get("default_league_strength", 1.0),
            "league_strength_overrides": cfg.get("league_strength_overrides", {}) or {},
            "evidence_factor_enabled": cfg.get("evidence_factor_enabled", True),
            "min_evidence_ratio": cfg.get("min_evidence_ratio", 0.5),
            "max_total_xg_shift": cfg.get("max_total_xg_shift", 0.60),
            "max_prob_transfer_per_side": cfg.get("max_prob_transfer_per_side", 0.10),
            "draw_bounds": cfg.get("draw_bounds", [0.08, 0.45]),
            "min_side_prob": cfg.get("min_side_prob", 0.02),
        }
    return _GUARD_CACHE


def evaluated_sample_counts() -> dict:
    """Count ledger entries per league that already have a matched result."""
    global _SAMPLE_CACHE
    if _SAMPLE_CACHE is not None:
        return _SAMPLE_CACHE
    counts = {}
    try:
        import ledger
        from league_data import load_results_data
        by_league = {}
        for entry in ledger.load_ledger().get("predictions", []):
            by_league.setdefault(entry.get("league", "?"), []).append(entry)
        for league, entries in by_league.items():
            results = load_results_data(league)
            pairs = ledger.match_results(entries, results)
            counts[league] = sum(1 for _, result in pairs if result is not None)
    except Exception:
        counts = {}
    _SAMPLE_CACHE = counts
    return counts


def league_strength(league: str) -> float:
    """Scale rule effects by how much evidence-backed sample a league has."""
    guards = guard_config()
    overrides = guards.get("league_strength_overrides", {})
    if league in overrides:
        return _as_float(overrides.get(league), guards.get("default_league_strength", 1.0))
    counts = evaluated_sample_counts()
    if counts.get(league, 0) >= int(guards.get("thin_sample_threshold", 15)):
        return _as_float(guards.get("default_league_strength"), 1.0)
    return _as_float(guards.get("thin_sample_league_strength"), 0.7)


# ================================================================
# 证据与强度
# ================================================================

def _as_float(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_present(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (dict, list, tuple, str)):
        return len(value) > 0
    return True


def evidence_ratio(rule: dict, evidence: dict) -> float:
    """Share of the rule's declared required evidence that is actually present."""
    required = rule.get("requires") or []
    if not required:
        return 1.0
    present = sum(1 for key in required if _is_present((evidence or {}).get(key)))
    return present / float(len(required))


def rule_strength(rule_name: str, rule: dict, league: str, evidence: dict) -> float:
    """Effective multiplier for one rule: base x league maturity x evidence quality."""
    guards = guard_config()
    if not guards.get("enabled", True):
        return 0.0
    if not rule_applies(rule, league):
        return 0.0

    base = _as_float(rule.get("strength"), 1.0)
    strength = base * league_strength(league)
    if guards.get("evidence_factor_enabled", True):
        ratio = evidence_ratio(rule, evidence)
        if ratio < _as_float(guards.get("min_evidence_ratio"), 0.5):
            return 0.0
        strength *= ratio
    return strength


def rule_applies(rule: dict, league: str) -> bool:
    """`leagues` may be absent, the string "all", or an explicit allow-list."""
    leagues = rule.get("leagues")
    if not leagues:
        return True
    if isinstance(leagues, str):
        return leagues.strip().lower() in ("all", "*")
    return league in leagues


def global_rule(name: str) -> dict:
    """Fetch one global rule definition."""
    return load_global_rules().get(name, {})


# ================================================================
# 硬上限守卫
# ================================================================

def _clamp(value, low, high):
    return max(low, min(high, value))


def apply_xg_guards(la: float, lb: float, base_la: float, base_lb: float) -> tuple:
    """Cap the aggregate xG displacement produced by all rules."""
    guards = guard_config()
    cap = _as_float(guards.get("max_total_xg_shift"), 0.60)
    total_now = la + lb
    total_base = base_la + base_lb
    shift = total_now - total_base
    if abs(shift) > cap and total_now > 0:
        excess = shift - cap if shift > 0 else shift + cap
        la -= excess * (la / total_now)
        lb -= excess * (lb / total_now)
    return max(0.05, la), max(0.05, lb)


def apply_prob_guards(pw: float, pd: float, pl: float,
                      base_pw: float, base_pd: float, base_pl: float) -> tuple:
    """Cap per-side probability transfer, keep the draw inside bounds, renorm."""
    guards = guard_config()
    max_transfer = _as_float(guards.get("max_prob_transfer_per_side"), 0.10)
    pw = _clamp(pw, base_pw - max_transfer, base_pw + max_transfer)
    pl = _clamp(pl, base_pl - max_transfer, base_pl + max_transfer)

    draw_low, draw_high = guards.get("draw_bounds", [0.08, 0.45])
    pd = _clamp(pd, _as_float(draw_low, 0.08), _as_float(draw_high, 0.45))

    min_side = _as_float(guards.get("min_side_prob"), 0.02)
    pw = max(pw, min_side)
    pl = max(pl, min_side)

    total = pw + pd + pl
    if total <= 0:
        return base_pw, base_pd, base_pl
    return pw / total, pd / total, pl / total


def scale_multiplier(multiplier: float, strength: float) -> float:
    """Scale a scoreline multiplier toward 1.0 by rule strength."""
    return 1.0 + (multiplier - 1.0) * strength


def scale_delta(delta: float, strength: float) -> float:
    """Scale a probability or xG delta by rule strength."""
    return delta * strength
