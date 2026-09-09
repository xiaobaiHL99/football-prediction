#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联赛预测引擎 — 挪超/瑞超/MLS

基于世界杯预测模型逻辑，适配联赛特征：
- Elo + 泊松 + 蒙特卡洛
- 联赛特殊因子（人工草皮、旅行距离、欧战消耗）
- 单场预测 + 积分榜模拟

Usage:
  league_predict.py match BOD MOL --league eliteserien
  league_predict.py table eliteserien --sims 10000
  league_predict.py title eliteserien --sims 10000
  league_predict.py relegation eliteserien --sims 10000
  league_predict.py europe eliteserien --sims 10000
  league_predict.py simulate eliteserien --round 16 --sims 10000
"""

import json
import math
import random
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

# ================================================================
# 导入联赛数据模块
# ================================================================

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from league_data import (
    load_league_config,
    load_league_teams,
    load_league_context,
    load_current_table,
    load_fixtures,
    load_intelligence,
    load_intelligence_for_match,
    load_locked_results,
    load_results_data,
    resolve_team,
    cn,
    classify_match,
    parse_form,
    form_to_xg_adjust,
    count_meetings,
    compute_tactical_delta,
    MEETING_FAMILIARITY_THRESHOLD,
    FAMILIARITY_UNDERDOG_BOOST,
    FAMILIARITY_FAVORITE_PENALTY,
    load_global_rules,
)

# 导入增强版情报处理模块
try:
    from enhanced_intelligence import process_enhanced_intelligence
    ENHANCED_INTELLIGENCE_AVAILABLE = True
except ImportError:
    ENHANCED_INTELLIGENCE_AVAILABLE = False
    print("⚠️ 增强版情报模块不可用，使用基础情报处理", file=sys.stderr)

# 亚盘分析 + 诱盘检测（可选；无 odds_data 时完全不生效，向后兼容）
from odds_analyzer import (
    find_odds_match,
    analyze_odds,
    get_dynamic_shrink,
)

# ================================================================
# 常量
# ================================================================

MAX_GOALS = 10
MAX_ELO_INTEL_DELTA = 30.0      # 单场Elo修正上限（v2收紧）
MAX_GOAL_INTEL_DELTA = 0.35
ELO_NEGATIVE_DAMP = 0.75        # 负向Elo修正衰减（防守韧性补偿）
WEAK_TEAM_ELO_THRESHOLD = 1550.0  # 弱队方差放大阈值
HOST_BONUS = 0.15                 # 东道主加成（xG），借鉴世界杯 host 逻辑

# 状态冲击点（复用世界杯逻辑）
SHOCK_POINTS = [(-2.0, 0.0545), (-1.0, 0.2442), (0.0, 0.4026), (1.0, 0.2442), (2.0, 0.0545)]


# ================================================================
# 核心工具函数
# ================================================================

def clamp(value, low, high):
    return max(low, min(high, value))


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def bar(p, width=20):
    return "█" * round(p * width) + "·" * (width - round(p * width))


def _pmf(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _sample(lam):
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= random.random()
        if p <= L:
            return k - 1


def _global_rule(name: str) -> dict:
    """Return one optional global rule without breaking older snapshots."""
    return load_global_rules().get(name, {})


def _score_probs_to_outcomes(score_probs: list) -> tuple:
    """Rebuild 1X2 probabilities after scoreline-level calibration."""
    pw = pd = pl = 0.0
    for (home_goals, away_goals), prob in score_probs:
        if home_goals > away_goals:
            pw += prob
        elif home_goals == away_goals:
            pd += prob
        else:
            pl += prob
    return pw, pd, pl


def qualify_open_match(tactical: dict) -> tuple:
    """Require explicit pre-match evidence before treating a fixture as open."""
    if tactical.get("expected_pattern") != "open":
        return tactical, []

    evidence = tactical.get("open_eligibility", {}) or {}
    checks = (
        ("双方攻击完整", evidence.get("attack_ready", {}).get("a") is True and evidence.get("attack_ready", {}).get("b") is True),
        ("双方转换威胁有效", evidence.get("transition_threat", {}).get("a") is True and evidence.get("transition_threat", {}).get("b") is True),
        ("无关键攻击伤停", evidence.get("no_key_attacking_absences", {}).get("a") is True and evidence.get("no_key_attacking_absences", {}).get("b") is True),
        ("无首轮或首回合试探", evidence.get("first_leg_or_opener_cautious") is False),
    )
    failed = [label for label, passed in checks if not passed]
    if not failed:
        return tactical, ["开放局准入通过"]

    downgraded = dict(tactical)
    downgraded["expected_pattern"] = "balanced"
    return downgraded, [f"开放局降级为均衡局({ '、'.join(failed) })"]


def apply_open_match_tail(score_probs: list, tactical: dict) -> tuple:
    """Lift high-scoring scorelines only for an evidence-qualified open matchup."""
    rule = _global_rule("OPEN_MATCH_TAIL_BOOST")
    if not rule.get("enabled") or tactical.get("expected_pattern") != "open":
        return score_probs, []

    high_mult = as_float(rule.get("high_score_multiplier"), 1.0)
    btts_high_mult = as_float(rule.get("very_high_score_multiplier"), high_mult)
    adjusted = []
    for (home_goals, away_goals), prob in score_probs:
        multiplier = 1.0
        if home_goals + away_goals >= 3:
            multiplier *= high_mult
        if home_goals >= 2 and away_goals >= 2:
            multiplier *= btts_high_mult
        adjusted.append(((home_goals, away_goals), prob * multiplier))

    total = sum(prob for _, prob in adjusted)
    if total <= 0:
        return score_probs, []
    normalized = [(score, prob / total) for score, prob in adjusted]
    normalized.sort(key=lambda item: -item[1])
    return normalized, [f"开放局高比分尾部({high_mult:.2f}x/双方进球{btts_high_mult:.2f}x)"]


def apply_defensive_absence_scoring_floor(la: float, lb: float, teams: dict,
                                         a: str, b: str, tactical: dict) -> tuple:
    """Keep a viable underdog attack above a conservative xG floor.

    The rule only operates on explicit pre-match defensive-absence counts; it
    never infers availability from narrative notes.
    """
    rule = _global_rule("DEFENSIVE_ABSENCE_SCORING_FLOOR")
    if not rule.get("enabled"):
        return la, lb, []

    elo_a = teams[a].get("elo", 1500)
    elo_b = teams[b].get("elo", 1500)
    if elo_a == elo_b:
        return la, lb, []
    favorite, underdog = (a, b) if elo_a > elo_b else (b, a)
    favorite_side = "a" if favorite == a else "b"
    underdog_side = "a" if underdog == a else "b"
    defensive_absences = tactical.get("defensive_absences", {}) or {}
    absence_count = as_float(defensive_absences.get(favorite_side), 0)
    underdog_style = tactical.get(f"style_{underdog_side}", "hybrid")
    eligible_styles = set(rule.get("eligible_styles", []))
    if absence_count < as_float(rule.get("min_defensive_absences"), float("inf")):
        return la, lb, []
    if underdog_style not in eligible_styles:
        return la, lb, []

    floor = as_float(rule.get("underdog_xg_floor"), 0.0)
    if underdog == a and la < floor:
        return floor, lb, [f"{cn(teams, a)}对手防线缺{absence_count:.0f}人，进球底线{floor:.2f}"]
    if underdog == b and lb < floor:
        return la, floor, [f"{cn(teams, b)}对手防线缺{absence_count:.0f}人，进球底线{floor:.2f}"]
    return la, lb, []


def apply_away_favorite_draw_protection(pw: float, pd: float, pl: float,
                                         teams: dict, a: str, b: str,
                                         intel: dict, tactical: dict) -> tuple:
    """Protect the draw when an away favorite carries at least two risks."""
    rule = _global_rule("AWAY_FAVORITE_DRAW_PROTECTION")
    if not rule.get("enabled") or teams[b].get("elo", 1500) <= teams[a].get("elo", 1500):
        return pw, pd, pl, []

    away_intel = (intel.get("teams", {}) or {}).get(b, {})
    away_intel = away_intel if isinstance(away_intel, dict) else {}
    match_risks = intel.get("away_favorite_risks", {}) or {}
    injuries = away_intel.get("injury_count", match_risks.get("injuries", 0))
    away_streak = away_intel.get("away_streak", match_risks.get("away_streak", 0))
    line_drop = match_risks.get("line_drop", 0.0)
    home_spirit = (tactical.get("fighting_spirit", {}) or {}).get("a", {})
    home_level = home_spirit.get("level", "normal") if isinstance(home_spirit, dict) else home_spirit
    triggers = rule.get("triggers", {})
    risks = sum((
        as_float(injuries) >= as_float(triggers.get("injuries"), float("inf")),
        as_float(away_streak) >= as_float(triggers.get("away_streak"), float("inf")),
        as_float(line_drop) >= as_float(triggers.get("line_drop"), float("inf")),
        bool(triggers.get("opponent_high_spirit")) and home_level in ("high", "desperate"),
    ))
    if risks < 2:
        return pw, pd, pl, []

    draw_boost = as_float(rule.get("draw_boost"), 0.0)
    favorite_penalty = abs(as_float(rule.get("favorite_penalty"), 0.0))
    transfer = min(draw_boost, favorite_penalty, pl)
    if transfer <= 0:
        return pw, pd, pl, []
    pl -= transfer
    pd += transfer
    return pw, pd, pl, [f"客强队多重风险防平({risks}项)"]


# ================================================================
# 预期进球（含联赛特殊因子）
# ================================================================

def expected_goals(teams: dict, a: str, b: str, league: str, league_context: dict,
                   goal_delta_a: float = 0.0, goal_delta_b: float = 0.0,
                   context_boost_a: float = 0.0, context_boost_b: float = 0.0,
                   openness: float = 0.0,
                   elo_delta_a: float = 0.0, elo_delta_b: float = 0.0,
                   xg_adjust_a: float = 0.0, xg_adjust_b: float = 0.0,
                   home_adv_mult: float = 1.0,
                   tactical: dict = None,
                   date: str = None) -> tuple:
    """
    计算预期进球，包含联赛特殊因子 + 战术对位因子
    elo_delta_a/b: 团队级 Elo 修正（球员缺阵等），影响净胜球期望
    goal_delta_a/b: 单场 xG 修正（伤停/状态等），与 elo_delta 正交叠加
    tactical: 战术对位信息字典，传给 compute_tactical_delta()
    """
    config = load_league_config(league)
    avg_goals = config["avg_goals"]
    home_adv = config["home_adv"] * home_adv_mult

    # 东道主加成：球队 teams.<code>.host=true 时在 xG 上叠加（世界杯借鉴）
    host_bonus = config.get("host_bonus", HOST_BONUS)
    host_a = host_bonus if teams[a].get("host", False) else 0.0
    host_b = host_bonus if teams[b].get("host", False) else 0.0

    elo_scale = config["elo_scale"]

    # 基础 Elo 换算（含 elo_delta 修正）
    # v2: 防守韧性补偿——负向修正只按比例生效
    damp_a = ELO_NEGATIVE_DAMP if elo_delta_a < 0 else 1.0
    damp_b = ELO_NEGATIVE_DAMP if elo_delta_b < 0 else 1.0

    elo_a = teams[a]["elo"]
    elo_b = teams[b]["elo"]
    sup = ((elo_a + elo_delta_a * damp_a) -
           (elo_b + elo_delta_b * damp_b)) / elo_scale

    base_la = avg_goals / 2 + sup / 2 + home_adv + host_a
    base_lb = avg_goals / 2 - sup / 2 + host_b

    # 联赛特殊因子
    special_delta_a, special_delta_b = apply_special_factors(
        teams, a, b, league, league_context, config=config, date=date
    )

    # 战术对位因子
    tact_a, tact_b = compute_tactical_delta(tactical)

    la = max(0.20, base_la + goal_delta_a + special_delta_a + xg_adjust_a + context_boost_a + openness + tact_a)
    lb = max(0.20, base_lb + goal_delta_b + special_delta_b + xg_adjust_b + context_boost_b + openness + tact_b)

    return la, lb


def apply_special_factors(teams: dict, a: str, b: str, league: str, league_context: dict,
                          config: dict = None, date: str = None) -> tuple:
    """
    应用联赛特殊因子
    返回 (delta_a, delta_b)
    """
    delta_a = 0.0
    delta_b = 0.0

    factors = league_context.get("special_factors", {})

    # ---- 开幕轮主场加成 ----
    if config and date:
        opening_bonus = config.get("opening_round_bonus", 0.0)
        season_start = config.get("season_start", "")
        if opening_bonus and season_start:
            try:
                match_dt = datetime.strptime(date, "%Y-%m-%d")
                start_dt = datetime.strptime(season_start, "%Y-%m-%d")
                if 0 <= (match_dt - start_dt).days <= 7:
                    delta_a += opening_bonus
            except ValueError:
                pass

    # ---- 人工草皮惩罚 ----
    if "artificial_turf_penalty" in factors:
        turf_map = league_context.get("turf_map", {})
        artificial = set(turf_map.get("artificial", []))
        natural = set(turf_map.get("natural", []))

        # 主队人工草皮，客队天然草皮 → 客队不适
        if a in artificial and b in natural:
            delta_b += factors["artificial_turf_penalty"]["value"]
        elif b in artificial and a in natural:
            delta_a += factors["artificial_turf_penalty"]["value"]

    # ---- 欧战消耗 ----
    # 当比赛本身就是欧战时跳过（欧战分心只适用于国内联赛）
    EURO_COMPETITIONS = {"champions_league", "europa_league"}
    is_euro_match = league in EURO_COMPETITIONS if league else False
    euro_key = next((k for k in factors if "europe" in k.lower() and "distraction" in k.lower()), None)
    if euro_key and not is_euro_match:
        europe_teams = set(league_context.get("europe_teams", []))
        euro_val = as_float(factors[euro_key].get("value", -0.08))
        if a in europe_teams:
            delta_a += euro_val
        if b in europe_teams:
            delta_b += euro_val

    # ---- 旅行距离 (跨区/跨海岸) ----
    if "travel_distance" in factors:
        region_map = {code: t.get("region", "") for code, t in teams.items()}
        reg_a = region_map.get(a, "")
        reg_b = region_map.get(b, "")
        if reg_a and reg_b and reg_a != reg_b:
            delta_a += factors["travel_distance"]["value"]
            delta_b += factors["travel_distance"]["value"]

    # ---- 北方球队主场优势 (挪超) ----
    if "northern_advantage" in factors:
        region_map = {code: t.get("region", "") for code, t in teams.items()}
        if region_map.get(a) == "north" and region_map.get(b) != "north":
            delta_a += factors["northern_advantage"]["value"]
        elif region_map.get(b) == "north" and region_map.get(a) != "north":
            delta_b += factors["northern_advantage"]["value"]

    # ---- 夏季高进球 (挪超) ----
    if "june_july_high_scoring" in factors:
        # 由外部传入月份判断，这里暂不自动应用
        pass

    # ---- 高温高湿惩罚 (巴甲) ----
    if "heat_humidity_penalty" in factors:
        region_map = {code: t.get("region", "") for code, t in teams.items()}
        if region_map.get(a) == "northeast" and region_map.get(b) != "northeast":
            delta_a += factors["heat_humidity_penalty"]["value"]

    # ---- 高海拔优势 (巴甲) ----
    if "altitude_advantage" in factors:
        region_map = {code: t.get("region", "") for code, t in teams.items()}
        if region_map.get(a) == "centralwest" and region_map.get(b) not in ("centralwest", ""):
            delta_a += factors["altitude_advantage"]["value"]

    # ---- 欧罗巴周中紧凑赛程 ----
    if "thursday_schedule_compact" in factors:
        europe_teams = set(league_context.get("europe_teams", []))
        if a in europe_teams:
            delta_a += factors["thursday_schedule_compact"]["value"]
        if b in europe_teams:
            delta_b += factors["thursday_schedule_compact"]["value"]

    # ---- 欧罗巴天气差异 ----
    if "weather_difference" in factors:
        region_index = league_context.get("region_index", {})
        idx_a = region_index.get(teams[a].get("region", ""), 1)
        idx_b = region_index.get(teams[b].get("region", ""), 1)
        if abs(idx_a - idx_b) >= 2:
            delta_a += factors["weather_difference"]["value"]
            delta_b += factors["weather_difference"]["value"]

    # ---- 夏季高温惩罚 (韩职) ----
    if "summer_heat_penalty" in factors:
        val = as_float(factors["summer_heat_penalty"].get("value", -0.04))
        delta_a += val
        delta_b += val

    # ---- 军旅球队 (韩职 金泉尚武) ----
    if "military_team" in factors:
        val = as_float(factors["military_team"].get("value", -0.05))
        military_name = "金泉尚武"
        if teams[a].get("name_cn", "") == military_name:
            delta_a += val
        if teams[b].get("name_cn", "") == military_name:
            delta_b += val

    return delta_a, delta_b


# ================================================================
# 比分分布
# ================================================================

def outcome_probs(la: float, lb: float, shock_sd: float = 0.28,
                  avg_elo: float = None, shock_mult: float = 1.0,
                  weak_threshold: float = None) -> tuple:
    """
    计算胜平负概率 + 比分分布
    复用世界杯逻辑
    avg_elo: 若低于弱队阈值则放大方差（弱队对局更随机）；
             阈值可由调用方按联赛覆盖（weak_threshold），默认用全局常量
    shock_mult: 情境分类方差乘数（弱弱对话>1, 实力悬殊<1）
    """
    # 弱队方差放大
    effective_sd = shock_sd * shock_mult
    _wt = weak_threshold if weak_threshold is not None else WEAK_TEAM_ELO_THRESHOLD
    if avg_elo is not None and avg_elo < _wt:
        boost = 1.0 + (_wt - avg_elo) / _wt
        effective_sd *= min(boost, 1.4)

    pw = pd = pl = 0.0
    score_probs = defaultdict(float)

    for z, weight in SHOCK_POINTS:
        sla = max(0.15, la + z * effective_sd / 2)
        slb = max(0.15, lb - z * effective_sd / 2)

        for i in range(MAX_GOALS + 1):
            for j in range(MAX_GOALS + 1):
                p = weight * _pmf(i, sla) * _pmf(j, slb)
                score_probs[(i, j)] += p
                if i > j:
                    pw += p
                elif i == j:
                    pd += p
                else:
                    pl += p

    best = sorted(score_probs.items(), key=lambda x: -x[1])
    return pw, pd, pl, best


def outcome_direction(pw: float, pd: float, pl: float) -> str:
    """返回 'home' / 'draw' / 'away'"""
    return max((("home", pw), ("draw", pd), ("away", pl)), key=lambda x: x[1])[0]


def directional_score(score_probs: list, direction: str) -> tuple:
    """从比分分布中取出与方向一致的最高概率比分"""
    for score, prob in score_probs:
        i, j = score
        if direction == "home" and i > j:
            return score, prob
        elif direction == "draw" and i == j:
            return score, prob
        elif direction == "away" and i < j:
            return score, prob
    return score_probs[0]


def apply_probability_shrink(pw: float, pd: float, pl: float, league: str,
                             shrink_override: float = None) -> tuple:
    """向联赛基线收缩。shrink_override 非空时用它替代联赛配置（诱盘动态收缩用）。"""
    config = load_league_config(league)
    shrink = config.get("probability_shrink", 0.10) if shrink_override is None else shrink_override
    draw_base = config.get("draw_base", 0.26)
    nw = (1 - draw_base) / 2
    nd = draw_base
    nl = (1 - draw_base) / 2

    return (
        pw * (1 - shrink) + nw * shrink,
        pd * (1 - shrink) + nd * shrink,
        pl * (1 - shrink) + nl * shrink
    )


# ================================================================
# 校准规则（基于累计分桶统计的偏误，软调整模型输出）
# 规则定义在 references/rules.json；删除该文件即关闭全部规则。
# ================================================================

_RULES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "references", "rules.json")
_RULES_CACHE = None


def _load_calibration_rules():
    global _RULES_CACHE
    if _RULES_CACHE is None:
        try:
            with open(_RULES_PATH, encoding="utf-8") as f:
                _RULES_CACHE = json.load(f).get("rules", [])
        except Exception:
            _RULES_CACHE = []
    return _RULES_CACHE


def _rule_matches(when: dict, ctx: dict) -> bool:
    """检查规则 when 条件是否匹配当前情境 ctx（所有条件同时满足）。"""
    for key, val in when.items():
        if key == "pred_goals_lt" and not (ctx.get("pred_goals", float("inf")) < val):
            return False
        if key == "elo_gap_lt" and not (ctx.get("elo_gap", float("inf")) < val):
            return False
        if key == "direction" and ctx.get("direction") != val:
            return False
        # 未知条件视为不匹配（保守，不阻断）
        if key not in ("pred_goals_lt", "elo_gap_lt", "direction"):
            return False
    return True


def _shift_probability(pw, pd, pl, target, amount):
    """把 amount 概率从其他方向按比例转移给 target，并归一化。"""
    vals = [pw, pd, pl]
    idx = {"home": 0, "draw": 1, "away": 2}[target]
    others = [i for i in range(3) if i != idx]
    osum = sum(vals[i] for i in others)
    if osum <= 1e-12:
        return pw, pd, pl
    vals[idx] += amount
    for i in others:
        vals[i] -= amount * (vals[i] / osum)
    vals = [max(v, 0.0) for v in vals]
    s = sum(vals)
    if s <= 0:
        return pw, pd, pl
    return tuple(v / s for v in vals)


def apply_rule_xg(la, lb, ctx):
    """xG 层校准规则（进球低估上调 la/lb）。返回 (la, lb, 命中的规则 label 列表)。"""
    labels = []
    for rule in _load_calibration_rules():
        do = rule.get("do", {})
        if "la_up" not in do:
            continue
        if not _rule_matches(rule.get("when", {}), ctx):
            continue
        la += do.get("la_up", 0.0)
        lb += do.get("lb_up", 0.0)
        labels.append(rule.get("label", rule["id"]))
    return la, lb, labels


def apply_rule_prob(pw, pd, pl, ctx):
    """概率层校准规则（方向偏误收缩）。返回 (pw, pd, pl, 命中的规则 label 列表)。"""
    labels = []
    for rule in _load_calibration_rules():
        do = rule.get("do", {})
        if "shift_to" not in do:
            continue
        if not _rule_matches(rule.get("when", {}), ctx):
            continue
        pw, pd, pl = _shift_probability(pw, pd, pl, do["shift_to"], do.get("amount", 0.03))
        labels.append(rule.get("label", rule["id"]))
    return pw, pd, pl, labels


# ================================================================
# 单场预测
# ================================================================

def build_match_entry(teams: dict, a: str, b: str, league: str,
                      intelligence: dict = None, locked: dict = None,
                      neutral: bool = False, current_table: dict = None,
                      results_data: dict = None,
                      date: str = None, odds_data: dict = None) -> dict:
    """
    计算单场预测并返回结构化条目（供打印与预测台账记录复用）。
    确定性：无随机数，相同输入必得相同输出。

    Returns
    -------
    dict — 含 date / league / 双方 / prediction（概率·方向·比分分布）/ model_inputs（模型输入快照）
    """
    if intelligence is None:
        intelligence = {}
    if locked is None:
        locked = {}
    if current_table is None:
        current_table = {}
    if results_data is None:
        results_data = {"matches": []}

    league_context = load_league_context(league)
    config = load_league_config(league)

    # 使用增强版情报处理（如果可用）
    enhanced_intel = {}
    if ENHANCED_INTELLIGENCE_AVAILABLE:
        try:
            enhanced_intel = process_enhanced_intelligence(league, a, b)
            
            # 将增强版情报转换为 match_intelligence 期望的格式
            if enhanced_intel:
                # 创建 matches 数组格式
                match_data = {
                    "teams": [a, b],
                    "confidence": 1.0,
                    "goal_delta": enhanced_intel.get("goal_delta", {}),
                    "elo_delta": enhanced_intel.get("elo_delta", {}),
                    "form_delta": enhanced_intel.get("form_delta", {}),
                    "factors": enhanced_intel.get("factors", []),
                    "notes": enhanced_intel.get("notes", [])
                }
                
                # 合并到 intelligence
                intelligence.setdefault("matches", []).append(match_data)
                
                # 调试信息（默认关闭避免刷屏；设 FB_INTEL_DEBUG=1 开启）
                if os.environ.get("FB_INTEL_DEBUG") == "1":
                    print(f"  🔍 增强版情报处理结果:", file=sys.stderr)
                    print(f"    目标修正: {enhanced_intel.get('goal_delta', {})}", file=sys.stderr)
                    print(f"    状态修正: {enhanced_intel.get('form_delta', {})}", file=sys.stderr)
                    print(f"    关键球员影响: {enhanced_intel.get('key_player_impact', {})}", file=sys.stderr)
                    print(f"    历史交锋影响: {enhanced_intel.get('h2h_impact', {})}", file=sys.stderr)
                    print(f"    特殊因素影响: {enhanced_intel.get('special_factors_impact', {})}", file=sys.stderr)
        except Exception as e:
            print(f"⚠️ 增强版情报处理失败: {e}", file=sys.stderr)

    # 匹配情报
    intel = match_intelligence(intelligence, a, b)
    goal_delta = intel.get("goal_delta", {})
    goal_boost = intel.get("goal_boost", {})
    context_openness = intel.get("context_openness", {})
    openness = max(context_openness.get(a, 0.0), context_openness.get(b, 0.0))
    elo_delta = intel.get("elo_delta", {})
    form_delta = intel.get("form_delta", {})
    tactical_adjust = intel.get("tactical_adjust", {"underdog_boost": 0.0, "favorite_penalty": 0.0})
    tactical_matchup = intel.get("tactical_matchup", {})

    # 交手次数 + 熟悉度修正
    meeting_count = count_meetings(results_data, a, b)
    familiarity_adj_a = 0.0
    familiarity_adj_b = 0.0
    familiarity_labels = []
    if meeting_count >= MEETING_FAMILIARITY_THRESHOLD:
        elo_a = teams[a].get("elo", 1500)
        elo_b = teams[b].get("elo", 1500)
        if elo_a < elo_b:
            familiarity_adj_a = FAMILIARITY_UNDERDOG_BOOST
            familiarity_adj_b = FAMILIARITY_FAVORITE_PENALTY
        elif elo_b < elo_a:
            familiarity_adj_b = FAMILIARITY_UNDERDOG_BOOST
            familiarity_adj_a = FAMILIARITY_FAVORITE_PENALTY
        familiarity_labels.append(f"本赛季第{meeting_count}次交手")
        if abs(elo_a - elo_b) > 50:
            familiarity_labels.append("弱队熟悉度加成(+)")

    # 战术反制判断：弱队使用克制战术
    tactical_adj_a = 0.0
    tactical_adj_b = 0.0
    tactical_labels = []
    if tactical_adjust.get("underdog_boost", 0.0) != 0.0 or tactical_adjust.get("favorite_penalty", 0.0) != 0.0:
        elo_a = teams[a].get("elo", 1500)
        elo_b = teams[b].get("elo", 1500)
        if elo_a < elo_b:
            tactical_adj_a = tactical_adjust["underdog_boost"]
            tactical_adj_b = tactical_adjust["favorite_penalty"]
        else:
            tactical_adj_b = tactical_adjust["underdog_boost"]
            tactical_adj_a = tactical_adjust["favorite_penalty"]
        desc = tactical_adjust.get("description", "战术反制")
        tactical_labels.append(f"战术克制({desc})")

    # 比赛情境分类
    normalized_table = {str(k).upper(): v for k, v in current_table.items()}
    match_ctx = classify_match(
        teams, a, b,
        elo_delta_a=elo_delta.get(a, 0.0),
        elo_delta_b=elo_delta.get(b, 0.0),
        table=normalized_table,
        league_context=league_context,
        intel=intel,
        league=league
    )
    # 中立场：取消主场优势（修复 --neutral 原为无效参数的问题）
    if neutral:
        match_ctx["home_adv_multiplier"] = 0.0

    # 叠加 form/familiarity/tactical 到 xG_adjust
    total_adj_a = (form_delta.get(a, 0.0) + familiarity_adj_a + tactical_adj_a)
    total_adj_b = (form_delta.get(b, 0.0) + familiarity_adj_b + tactical_adj_b)
    match_ctx["xG_adjust_a"] += total_adj_a
    match_ctx["xG_adjust_b"] += total_adj_b

    # 收集额外标签
    extra_labels = []
    fd_a = form_delta.get(a, 0.0)
    fd_b = form_delta.get(b, 0.0)
    if abs(fd_a) > 0.03:
        status_a = "火热" if fd_a > 0 else "低迷"
        extra_labels.append(f"{cn(teams, a)}状态{status_a}({fd_a:+.2f})")
    if abs(fd_b) > 0.03:
        status_b = "火热" if fd_b > 0 else "低迷"
        extra_labels.append(f"{cn(teams, b)}状态{status_b}({fd_b:+.2f})")
    # 战意因素标签（level ≠ normal 才显示）
    spirit = tactical_matchup.get("fighting_spirit", {})
    if isinstance(spirit, dict):
        SPIRIT_CN = {"desperate": "背水一战", "high": "高涨", "low": "偏低", "dead_rubber": "无欲无求"}
        for side, code in (("a", a), ("b", b)):
            sp_entry = spirit.get(side)
            lvl = sp_entry.get("level", "normal") if isinstance(sp_entry, dict) else (sp_entry if isinstance(sp_entry, str) else "normal")
            if lvl in SPIRIT_CN:
                note = sp_entry.get("note", "") if isinstance(sp_entry, dict) else ""
                suffix = f"({note})" if note else ""
                extra_labels.append(f"{cn(teams, code)}战意{SPIRIT_CN[lvl]}{suffix}")
    # 东道主加成标签（teams.<code>.host=true 时显示）
    if teams[a].get("host", False):
        extra_labels.append(f"{cn(teams, a)}东道主加成(+)")
    if teams[b].get("host", False):
        extra_labels.append(f"{cn(teams, b)}东道主加成(+)")
    # 开幕轮主场加成标签
    if date and config.get("opening_round_bonus", 0.0) > 0:
        season_start = config.get("season_start", "")
        if season_start:
            try:
                match_dt = datetime.strptime(date, "%Y-%m-%d")
                start_dt = datetime.strptime(season_start, "%Y-%m-%d")
                if 0 <= (match_dt - start_dt).days <= 7:
                    bonus = config["opening_round_bonus"]
                    extra_labels.append(f"开幕轮主场加成({bonus:+.2f})")
            except ValueError:
                pass
    extra_labels.extend(familiarity_labels)
    extra_labels.extend(tactical_labels)
    extra_labels.extend(intel.get("pattern_labels", []))

    # 检查是否有锁定结果
    locked_key = frozenset((a, b))
    if locked_key in locked:
        home, hg, ag = locked[locked_key]
        if home == a:
            la, lb = float(hg), float(ag)
        else:
            la, lb = float(ag), float(hg)
        pw, pd, pl = (1.0, 0.0, 0.0) if la > lb else ((0.0, 1.0, 0.0) if la == lb else (0.0, 0.0, 1.0))
        all_scores = [((int(la), int(lb)), 1.0)]
        xg_labels, prob_labels = [], []
    else:
        la_orig, lb_orig = expected_goals(
            teams, a, b, league, league_context,
            goal_delta.get(a, 0.0), goal_delta.get(b, 0.0),
            goal_boost.get(a, 0.0), goal_boost.get(b, 0.0),
            openness,
            elo_delta.get(a, 0.0), elo_delta.get(b, 0.0),
            xg_adjust_a=match_ctx["xG_adjust_a"],
            xg_adjust_b=match_ctx["xG_adjust_b"],
            home_adv_mult=match_ctx["home_adv_multiplier"],
            tactical=tactical_matchup,
            date=date
        )
        la, lb = la_orig, lb_orig
        odds_block = None
        odds_match = None
        shrink_override = None
        if odds_data is not None:
            odds_match = find_odds_match(odds_data, a, b)
            if odds_match is not None:
                elo_gap = abs(teams[a].get("elo", 1500) - teams[b].get("elo", 1500))
                odds_block = analyze_odds(
                    la_orig, lb_orig, odds_match,
                    model_confidence=intel.get("confidence", 1.0),
                    elo_gap=elo_gap,
                )
                if odds_block is not None:
                    la, lb = odds_block["final_xg"]["la"], odds_block["final_xg"]["lb"]
                    shrink_override = get_dynamic_shrink(league, odds_block["trap_decision"], elo_gap=elo_gap)
        # 校准规则（xG 层：进球低估上调）
        la, lb, xg_labels = apply_rule_xg(la, lb, {
            "pred_goals": la + lb,
            "elo_gap": abs(teams[a].get("elo", 1500) - teams[b].get("elo", 1500)),
        })
        la, lb, defensive_floor_labels = apply_defensive_absence_scoring_floor(
            la, lb, teams, a, b, tactical_matchup
        )
        xg_labels.extend(defensive_floor_labels)
        open_rule = _global_rule("OPEN_MATCH_TAIL_BOOST")
        if open_rule.get("enabled") and tactical_matchup.get("expected_pattern") == "open":
            tail_goal_boost = as_float(open_rule.get("tail_goal_boost"), 0.0)
            la += tail_goal_boost
            lb += tail_goal_boost
            xg_labels.append(f"开放局总xG+{tail_goal_boost * 2:.2f}")
        pw, pd, pl, all_scores = outcome_probs(
            la, lb, config.get("shock_sd", 0.28),
            avg_elo=(teams[a].get("elo", 1500) + teams[b].get("elo", 1500)) / 2,
            shock_mult=match_ctx["shock_multiplier"],
            weak_threshold=config.get("weak_elo_threshold")
        )
        all_scores, tail_labels = apply_open_match_tail(all_scores, tactical_matchup)
        pw, pd, pl = _score_probs_to_outcomes(all_scores)
        xg_labels.extend(tail_labels)

        # 欧战分心平局概率上调（2026-09-03复盘改进）
        # 当双方都有欧战分心时，平局概率上调5-8%
        draw_boost = match_ctx.get("draw_boost", 0.0)
        if draw_boost > 0:
            # 从胜/负方向各扣一部分给平局
            reduction = draw_boost / 2
            if pw > reduction and pl > reduction:
                pw -= reduction
                pl -= reduction
                pd += draw_boost
                # 重新归一化（虽然概率总和应该仍是1，但浮点误差可能导致微小偏差）
                total = pw + pd + pl
                if abs(total - 1.0) > 0.001:
                    pw /= total
                    pd /= total
                    pl /= total

        # 冷门预警处理（2026-09-04复盘改进）
        # 当往绩+状态+伤停三重因素叠加时，热门胜率下调
        upset_warning = match_ctx.get("upset_warning", False)
        upset_penalty = match_ctx.get("upset_penalty", 0.0)
        if upset_warning and upset_penalty != 0:
            # 从胜率最高的一方扣减，分配给平局和另一方
            if pw > pl:  # 主队是热门
                pw += upset_penalty  # upset_penalty是负值
                distribution = -upset_penalty / 2
                pd += distribution * 0.6
                pl += distribution * 0.4
            elif pl > pw:  # 客队是热门
                pl += upset_penalty
                distribution = -upset_penalty / 2
                pd += distribution * 0.6
                pw += distribution * 0.4
            # 归一化
            total = pw + pd + pl
            if abs(total - 1.0) > 0.001:
                pw /= total
                pd /= total
                pl /= total

        pw, pd, pl = apply_probability_shrink(pw, pd, pl, league, shrink_override)
        # 校准规则（概率层：方向偏误收缩）
        pw, pd, pl, prob_labels = apply_rule_prob(pw, pd, pl, {
            "direction": outcome_direction(pw, pd, pl),
            "pred_goals": la + lb,
            "elo_gap": abs(teams[a].get("elo", 1500) - teams[b].get("elo", 1500)),
        })
        pw, pd, pl, away_draw_labels = apply_away_favorite_draw_protection(
            pw, pd, pl, teams, a, b, intel, tactical_matchup
        )
        prob_labels.extend(away_draw_labels)

    direction = outcome_direction(pw, pd, pl)
    main_score, main_prob = directional_score(all_scores, direction)
    modal_score, modal_prob = all_scores[0]

    na, nb = cn(teams, a), cn(teams, b)
    direction_text = {"home": f"{na}占优", "draw": "平局倾向", "away": f"{nb}占优"}[direction]

    all_labels = list(match_ctx.get("labels", []))
    all_labels.extend(extra_labels)
    all_labels.extend(xg_labels)
    all_labels.extend(prob_labels)

    return {
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "league": league,
        "league_name_cn": config.get("name_cn", league),
        "home": a, "away": b,
        "home_cn": na, "away_cn": nb,
        "prediction": {
            "la": round(la, 3), "lb": round(lb, 3),
            "pw": round(pw, 4), "pd": round(pd, 4), "pl": round(pl, 4),
            "direction": direction,
            "direction_text": direction_text,
            "main_score": list(main_score),
            "main_prob": round(main_prob, 4),
            "modal_score": list(modal_score),
            "modal_prob": round(modal_prob, 4),
            "score_top5": [{"score": list(s), "prob": round(p, 4)} for s, p in all_scores[:5]],
            "labels": all_labels,
            "odds": odds_block,   # None = 无盘口数据；含盘口分析/诱盘判定的完整块
        },
        "model_inputs": {
            "elo": {a: teams[a].get("elo", 1500), b: teams[b].get("elo", 1500)},
            "elo_delta": elo_delta,
            "form_delta": form_delta,
            "confidence": intel.get("confidence", 1.0),
            "goal_delta": goal_delta,
            "goal_boost": goal_boost,
            "context_openness": context_openness,
            "tactical_adjust": tactical_adjust,
            "tactical_matchup": tactical_matchup,
            "shock_mult": match_ctx.get("shock_multiplier", 1.0),
            "home_adv_mult": match_ctx.get("home_adv_multiplier", 1.0),
            "shock_sd": config.get("shock_sd", 0.28),
            "meeting_count": meeting_count,
            "odds_match": odds_match,   # 本场盘口数据（诱盘复盘 / 复现预测用；无则 None）
        },
        "intelligence_snapshot": intelligence,
        "factors": intel.get("factors", []),
        "notes": intel.get("notes", []),
    }


def cmd_match(teams: dict, a: str, b: str, league: str,
              intelligence: dict = None, locked: dict = None,
              neutral: bool = False, current_table: dict = None,
              results_data: dict = None,
              date: str = None, record: bool = True,
              odds_data: dict = None):
    """
    单场预测命令。
    预测默认写入 data/reviews/predictions.json（预测台账）；record=False 时跳过（仅情景试验/纯试算用）。
    odds_data: {'matches': [{home, away, handicap_line/handicap, water_home, water_away, ...}]}，
               提供时进行盘口分析 + 诱盘检测（可选，缺省完全走原模型）。
    """
    entry = build_match_entry(teams, a, b, league, intelligence, locked,
                              neutral, current_table, results_data, date, odds_data)
    pred = entry["prediction"]
    la, lb = pred["la"], pred["lb"]
    pw, pd, pl = pred["pw"], pred["pd"], pred["pl"]
    na, nb = entry["home_cn"], entry["away_cn"]
    main_score, main_prob = pred["main_score"], pred["main_prob"]
    modal_score, modal_prob = pred["modal_score"], pred["modal_prob"]
    all_labels = pred["labels"]
    elo_delta = entry["model_inputs"]["elo_delta"]

    # 输出
    print(f"\n⚽ {na} vs {nb}")
    print(f"  联赛: {entry['league_name_cn']}")
    print(f"  预期进球: {na} {la:.2f} - {lb:.2f} {nb}")
    print(f"  主预测赛果: {pred['direction_text']}")
    print(f"  主预测比分: {na} {main_score[0]}-{main_score[1]} {nb}  ({main_prob * 100:.1f}%)")
    if modal_score != main_score:
        print(f"  单一最高概率比分: {na} {modal_score[0]}-{modal_score[1]} {nb}  ({modal_prob * 100:.1f}%)")

    # 盘口分析状态（可选）
    if pred.get("odds"):
        o = pred["odds"]
        ox = o["original_xg"]
        print(f"\n  📊 盘口分析: 盘口 {o['handicap_cn']} | 模型让球差 {o['model_margin']:+.2f} | "
              f"市场让球差 {o['market_margin']:+.2f} | 分歧 {o['divergence']:+.2f}")
        print(f"     诱盘评分 {o['trap_score']:.1f} → {o['trap_decision_cn']} | "
              f"市场权重 {o['market_weight']:.2f} | 采信: {o['market_confidence_cn']}")
        if o["market_factor"] != 0.0:
            print(f"     xG {na} {ox['la']:.2f} - {ox['lb']:.2f} {nb} → 盘口调整 "
                  f"{na} {la:.2f} - {lb:.2f} {nb} (factor {o['market_factor']:+.3f})")
        else:
            print(f"     xG {na} {ox['la']:.2f} - {ox['lb']:.2f} {nb}（未做盘口调整）")

    # 特殊因子说明
    league_context = load_league_context(league)
    render_special_factors(teams, a, b, league, league_context)

    # 情报说明
    render_intelligence_notes(teams, a, b, {"notes": entry["notes"], "factors": entry["factors"]})

    # 情境分类标签
    if all_labels:
        print(f"  情境分类: {'; '.join(all_labels)}")

    # 分桶校准提示（2026-09-01 复盘 n=91；两个已证实的系统性偏误直接告知使用者）
    _elo_map = entry["model_inputs"].get("elo", {})
    _elo_diff = abs(_elo_map.get(a, 1500) - _elo_map.get(b, 1500))
    if _elo_diff < 50:
        print("  📊 校准提示: 近均衡场次(累计29场)方向命中率仅41.4%、实际总进球比预测低0.6"
              " → 谨慎对待大分差/大球预期，平局价值上调")
    elif _elo_diff > 150:
        print("  📊 校准提示: 悬殊场次(累计28场)方向可靠(71.4%)但实际总进球比预测高0.5"
              " → 强队方向可信赖，总进球倾向被低估")

    # elo_delta 修正说明
    eda = elo_delta.get(a, 0.0)
    edb = elo_delta.get(b, 0.0)
    if eda != 0.0 or edb != 0.0:
        parts = []
        if eda != 0.0:
            parts.append(f"{cn(teams, a)} {'+' if eda > 0 else ''}{eda:.0f} Elo")
        if edb != 0.0:
            parts.append(f"{cn(teams, b)} {'+' if edb > 0 else ''}{edb:.0f} Elo")
        print(f"  球员可用性修正: {'; '.join(parts)}")

    # 概率条
    print(f"\n  {na}胜  {pw * 100:5.1f}%  {bar(pw)}")
    print(f"  平局      {pd * 100:5.1f}%  {bar(pd)}")
    print(f"  {nb}胜  {pl * 100:5.1f}%  {bar(pl)}")

    print("\n  比分分布 Top5:")
    for item in pred["score_top5"]:
        i, j = item["score"]
        print(f"    {na} {i}-{j} {nb}   {item['prob'] * 100:4.1f}%")
    print()

    if record:
        from ledger import append_entry
        append_entry(entry)
        print("  ✅ 已记录预测到台账: data/reviews/predictions.json")


def _warn_form_missed(intelligence: dict, code: str) -> None:
    """form 信息存在于非标准位置时提醒，避免状态修正静默失效。"""
    if not isinstance(intelligence, dict):
        return
    v = intelligence.get("teams", {}).get(code)
    if isinstance(v, str) and v:
        print(f"  ⚠️ {code} 的近期状态应放 intelligence.teams.{code}.form "
              f"（当前误放成字符串 \"{v}\"），状态修正未生效", file=sys.stderr)
        return
    if isinstance(v, dict):
        for k in ("recent_form", "form_string", "recent", "form5", "status"):
            val = v.get(k)
            if isinstance(val, str) and val:
                print(f"  ⚠️ {code} 的近期状态用了非标准键 \"{k}\"，应改为 "
                      f"intelligence.teams.{code}.form（值: \"{val}\"），状态修正未生效", file=sys.stderr)
                return


def match_intelligence(intelligence: dict, a: str, b: str) -> dict:
    """解析赛前情报（含团队级 elo_delta、近期状态、战术反制）"""
    empty = {
        "confidence": 1.0,
        "goal_delta": {a: 0.0, b: 0.0},
        "goal_boost": {a: 0.0, b: 0.0},
        "context_types": {a: "normal", b: "normal"},
        "context_openness": {a: 0.0, b: 0.0},
        "factors": [],
        "notes": [],
        "elo_delta": {a: 0.0, b: 0.0},
        "form_delta": {a: 0.0, b: 0.0},
        "tactical_adjust": {"underdog_boost": 0.0, "favorite_penalty": 0.0, "description": ""},
        "tactical_matchup": {}
    }

    result = None

    for item in intelligence.get("matches", []):
        if not isinstance(item, dict):
            continue
        codes = [str(c).upper() for c in item.get("teams", []) if c]
        if len(codes) != 2 or set(codes) != {a, b}:
            continue

        confidence = clamp(as_float(item.get("confidence"), 1.0), 0.0, 1.0)

        goal_delta = {}
        for code in (a, b):
            raw = as_float(item.get("goal_delta", {}).get(code, 0.0))
            goal_delta[code] = clamp(raw, -MAX_GOAL_INTEL_DELTA, MAX_GOAL_INTEL_DELTA) * confidence

        # 从 context 中提取 goal_boost
        ctx = item.get("competition_context", {})
        raw_boost = ctx.get("goal_boost", {})
        goal_boost = {}
        for code in (a, b):
            goal_boost[code] = clamp(as_float(raw_boost.get(code, 0.0)), -0.20, 0.20)

        # 解析近期状态（从 match-level teams 中提取）
        match_teams = item.get("teams", {})
        form_delta = {}
        for code in (a, b):
            form_str = ""
            # match_teams 可能是 dict {code: {form:...}} 或 list [code1, code2]
            if isinstance(match_teams, dict) and code in match_teams:
                v = match_teams[code]
                if isinstance(v, dict) and v.get("form"):
                    form_str = v["form"]
            if not form_str:
                v = intelligence.get("teams", {}).get(code, {})
                if isinstance(v, dict) and v.get("form"):
                    form_str = v["form"]
            if not form_str:
                # 防御：form 放在非标准位置 → 提醒，避免状态修正静默失效
                _warn_form_missed(intelligence, code)
            form_score, form_n = parse_form(form_str)
            form_delta[code] = form_to_xg_adjust(form_score, form_n)

        # 解析战术反制 + 战术对位
        tactical_raw = item.get("tactical", {})
        tactical_adjust = {
            "underdog_boost": clamp(as_float(tactical_raw.get("underdog_boost", 0.0)), 0.0, 0.12) if tactical_raw.get("counter_strategy") else 0.0,
            "favorite_penalty": clamp(as_float(tactical_raw.get("favorite_penalty", 0.0)), -0.12, 0.0) if tactical_raw.get("counter_strategy") else 0.0,
            "description": tactical_raw.get("description", "") if tactical_raw.get("counter_strategy") else ""
        }
        # 扩展战术对位信息（用于 compute_tactical_delta）
        tactical_matchup = {
            "style_a": tactical_raw.get("style_a", "hybrid"),
            "style_b": tactical_raw.get("style_b", "hybrid"),
            "matchup": tactical_raw.get("matchup", ""),
            "expected_pattern": tactical_raw.get("expected_pattern", "balanced"),
            "ineffective_possession": tactical_raw.get("ineffective_possession", False),
            "physical_mismatch": tactical_raw.get("physical_mismatch", 0),
            "derby_boost": tactical_raw.get("derby_boost", False),
            "fighting_spirit": tactical_raw.get("fighting_spirit", {}) or {},
            "defensive_absences": tactical_raw.get("defensive_absences", {}) or {},
            "open_eligibility": tactical_raw.get("open_eligibility", {}) or {},
        }
        tactical_matchup, pattern_labels = qualify_open_match(tactical_matchup)

        # 解析 context_openness
        raw_openness = item.get("context_openness", {})
        context_openness = {}
        for code in (a, b):
            context_openness[code] = clamp(as_float(raw_openness.get(code, 0.0)), -0.30, 0.30)

        result = {
            "confidence": confidence,
            "goal_delta": goal_delta,
            "goal_boost": goal_boost,
            "context_types": {a: "normal", b: "normal"},
            "context_openness": context_openness,
            "factors": item.get("factors", [])[:6],
            "notes": item.get("notes", []),
            "elo_delta": {a: 0.0, b: 0.0},
            "form_delta": form_delta,
            "tactical_adjust": tactical_adjust,
            "tactical_matchup": tactical_matchup,
            "pattern_labels": pattern_labels,
            # 轮换风险：从顶层next_match字段读取（2026-09-09新增）
            "next_match": intelligence.get("next_match", {})
        }
        break

    if result is None:
        # 格式 A：从 intelligence["teams"] 中直接提取 form
        teams_data = intelligence.get("teams", {})
        form_delta = {}
        for code in (a, b):
            entry = teams_data.get(code, {})
            form_str = entry.get("form", "") if isinstance(entry, dict) else ""
            if not form_str:
                # 防御：form 放在非标准位置 → 提醒，避免状态修正静默失效
                _warn_form_missed(intelligence, code)
            form_score, form_n = parse_form(form_str)
            form_delta[code] = form_to_xg_adjust(form_score, form_n)

        result = dict(empty)
        result["form_delta"] = form_delta

    # 团队级 elo_delta
    teams_data = intelligence.get("teams", {})
    for code in (a, b):
        entry = teams_data.get(code, {})
        # 防御：teams.<code> 可能是字符串等非 dict（如 form 误放）→ 视为空条目
        if not isinstance(entry, dict):
            entry = {}
        raw = as_float(entry.get("elo_delta", 0.0))
        conf = clamp(as_float(entry.get("confidence", 1.0)), 0.0, 1.0)
        result["elo_delta"][code] = clamp(raw, -MAX_ELO_INTEL_DELTA, MAX_ELO_INTEL_DELTA) * conf
        team_notes = entry.get("notes", "")
        if team_notes and team_notes not in result.get("notes", []):
            result.setdefault("notes", []).append(team_notes)

    return result


def render_special_factors(teams: dict, a: str, b: str, league: str, league_context: dict):
    """渲染特殊因子影响"""
    factors = league_context.get("special_factors", {})
    applied = []

    turf_map = league_context.get("turf_map", {})
    artificial = set(turf_map.get("artificial", []))

    if "artificial_turf_penalty" in factors:
        if a in artificial and b not in artificial:
            applied.append(f"{cn(teams, a)} 人工草皮主场，客队适应成本高")
        elif b in artificial and a not in artificial:
            applied.append(f"{cn(teams, b)} 人工草皮主场，客队适应成本高")

    if "european_qualification_distraction" in factors:
        europe = set(league_context.get("europe_teams", []))
        if a in europe:
            applied.append(f"{cn(teams, a)} 周中欧战消耗")
        if b in europe:
            applied.append(f"{cn(teams, b)} 周中欧战消耗")

    travel_key = next((k for k in factors if "travel" in k.lower()), None)
    if travel_key:
        region_map = {code: t.get("region", "") for code, t in teams.items()}
        reg_a = region_map.get(a, "")
        reg_b = region_map.get(b, "")
        if reg_a and reg_b and reg_a != reg_b:
            applied.append("跨区域长途旅行，双方均有消耗")

    # ---- 高温高湿 (巴甲) ----
    if "heat_humidity_penalty" in factors:
        region_map = {code: t.get("region", "") for code, t in teams.items()}
        if region_map.get(a) == "northeast" and region_map.get(b) != "northeast":
            applied.append(f"{cn(teams, a)} 东北部主场高温高湿")
        elif region_map.get(b) == "northeast" and region_map.get(a) != "northeast":
            applied.append(f"{cn(teams, b)} 东北部主场高温高湿")

    # ---- 解放者杯消耗 (巴甲) ----
    euro_key = next((k for k in factors if "libertadores" in k.lower()), None)
    if euro_key:
        euro_teams = set(league_context.get("europe_teams", []))
        if a in euro_teams:
            applied.append(f"{cn(teams, a)} 周中解放者杯消耗")
        if b in euro_teams:
            applied.append(f"{cn(teams, b)} 周中解放者杯消耗")

    # ---- 欧罗巴紧凑赛程 ----
    if "thursday_schedule_compact" in factors:
        euro_teams = set(league_context.get("europe_teams", []))
        hit_a = a in euro_teams
        hit_b = b in euro_teams
        desc = factors["thursday_schedule_compact"].get("description", "周中紧凑赛程")
        if hit_a and hit_b:
            applied.append(f"双方周中双线作战：{desc}")
        elif hit_a:
            applied.append(f"{cn(teams, a)} 周中双线作战：{desc}")
        elif hit_b:
            applied.append(f"{cn(teams, b)} 周中双线作战：{desc}")
        else:
            applied.append("周中紧凑赛程")

    # ---- 天气差异 (欧罗巴) ----
    if "weather_difference" in factors:
        region_index = league_context.get("region_index", {})
        idx_a = region_index.get(teams[a].get("region", ""), 1)
        idx_b = region_index.get(teams[b].get("region", ""), 1)
        if abs(idx_a - idx_b) >= 2:
            applied.append("南北欧天气差异，客队适应成本高")

    # ---- 夏季高温 (韩职) ----
    if "summer_heat_penalty" in factors:
        applied.append("夏季高温高湿，比赛节奏下降")

    # ---- 军旅球队 (韩职) ----
    if "military_team" in factors:
        military_name = "金泉尚武"
        if teams[a].get("name_cn", "") == military_name:
            applied.append(f"{cn(teams, a)} 军旅球队，阵容轮换频繁")
        if teams[b].get("name_cn", "") == military_name:
            applied.append(f"{cn(teams, b)} 军旅球队，阵容轮换频繁")

    if applied:
        print("  特殊因子: " + "；".join(applied))


def render_intelligence_notes(teams: dict, a: str, b: str, intel: dict):
    """渲染情报说明"""
    notes = intel.get("notes", [])
    factors = intel.get("factors", [])

    if notes or factors:
        print("  赛前情报:")
        for note in notes[:3]:
            print(f"    - {note}")
        for factor in factors[:3]:
            if isinstance(factor, dict):
                summary = factor.get("summary") or factor.get("note")
                if summary:
                    print(f"    - {summary}")


# ================================================================
# 积分榜模拟
# ================================================================

def cmd_table(teams: dict, league: str, current_table: dict, fixtures: list,
              locked: dict = None, sims: int = 10000, seed: int = None,
              intelligence: dict = None, results_data: dict = None):
    """
    模拟最终积分榜
    """
    if seed is not None:
        random.seed(seed)

    if locked is None:
        locked = {}

    if intelligence is None:
        intelligence = {}

    if results_data is None:
        results_data = {"matches": []}

    league_context = load_league_context(league)
    config = load_league_config(league)
    euro_spots = config.get("europa_spots", 4)
    rel_spots = config.get("relegation_spots", 2 if config["name"] not in ("Major League Soccer", "UEFA Europa League") else 0)

    codes = list(teams.keys())

    # 解析团队级 elo_delta
    team_elo_delta = {}
    teams_data = intelligence.get("teams", {})
    for code in codes:
        entry = teams_data.get(code, {})
        raw = as_float(entry.get("elo_delta", 0.0))
        conf = clamp(as_float(entry.get("confidence", 1.0)), 0.0, 1.0)
        team_elo_delta[code] = clamp(raw, -MAX_ELO_INTEL_DELTA, MAX_ELO_INTEL_DELTA) * conf

    # 预提取所有球队的近期状态
    team_form_delta = {}
    for code in codes:
        form_str = ""
        if code in teams_data:
            form_str = teams_data[code].get("form", "")
        # 也检查 match 级别的 teams（格式 B）
        if not form_str:
            for match in intelligence.get("matches", []):
                match_teams = match.get("teams", {})
                if code in match_teams:
                    fs = match_teams[code].get("form", "")
                    if fs:
                        form_str = fs
                        break
        form_score, form_n = parse_form(form_str)
        team_form_delta[code] = form_to_xg_adjust(form_score, form_n)

    # 预提取战术对位信息
    tactical_lookup = {}
    for match in intelligence.get("matches", []):
        if not isinstance(match, dict):
            continue
        codes_in_match = [str(c).upper() for c in match.get("teams", []) if c]
        if len(codes_in_match) != 2:
            continue
        key = frozenset(codes_in_match)
        raw = match.get("tactical", {})
        if raw:
            tactical_lookup[key] = {
                "style_a": raw.get("style_a", "hybrid"),
                "style_b": raw.get("style_b", "hybrid"),
                "matchup": raw.get("matchup", ""),
                "expected_pattern": raw.get("expected_pattern", "balanced"),
                "ineffective_possession": raw.get("ineffective_possession", False),
                "physical_mismatch": raw.get("physical_mismatch", 0),
                "derby_boost": raw.get("derby_boost", False),
                "fighting_spirit": raw.get("fighting_spirit", {}) or {},
            }

    # 统计各队最终排名概率
    rank_counts = {c: [0] * len(codes) for c in codes}

    # 争冠/欧战/降级统计
    title_counts = {c: 0 for c in codes}
    top_euro_counts = {c: 0 for c in codes}  # 欧战资格
    relegation_counts = {c: 0 for c in codes}  # 降级

    # 构建当前积分
    points = {c: 0 for c in codes}
    gd = {c: 0 for c in codes}
    gf = {c: 0 for c in codes}

    for code, data in current_table.items():
        code = str(code).upper()
        if code in points:
            points[code] = data.get("points", 0)
            gd[code] = data.get("gd", 0)
            gf[code] = data.get("gf", 0)

    print(f"\n🏟  {config['name_cn']} 最终积分榜模拟 ({sims} 次)")
    print("=" * 50)

    for _ in range(sims):
        # 复制当前积分
        sim_points = points.copy()
        sim_gd = gd.copy()
        sim_gf = gf.copy()

        # 模拟剩余比赛
        for match in fixtures:
            home = str(match["home"]).upper()
            away = str(match["away"]).upper()

            # 检查锁定
            locked_key = frozenset((home, away))
            if locked_key in locked:
                h_team, hg, ag = locked[locked_key]
                if h_team == home:
                    x, y = hg, ag
                else:
                    x, y = ag, hg
            else:
                # 比赛情境分类
                match_ctx = classify_match(
                    teams, home, away,
                    elo_delta_a=team_elo_delta.get(home, 0.0),
                    elo_delta_b=team_elo_delta.get(away, 0.0),
                    table=current_table,
                    league_context=league_context
                )

                # ---- 叠加近期状态修正 ----
                match_ctx["xG_adjust_a"] += team_form_delta.get(home, 0.0)
                match_ctx["xG_adjust_b"] += team_form_delta.get(away, 0.0)

                # ---- 叠加交手熟悉度修正 ----
                mt = count_meetings(results_data, home, away)
                if mt >= MEETING_FAMILIARITY_THRESHOLD:
                    elo_h = teams[home].get("elo", 1500)
                    elo_a = teams[away].get("elo", 1500)
                    if elo_h < elo_a:
                        match_ctx["xG_adjust_a"] += FAMILIARITY_UNDERDOG_BOOST
                        match_ctx["xG_adjust_b"] += FAMILIARITY_FAVORITE_PENALTY
                    elif elo_a < elo_h:
                        match_ctx["xG_adjust_b"] += FAMILIARITY_UNDERDOG_BOOST
                        match_ctx["xG_adjust_a"] += FAMILIARITY_FAVORITE_PENALTY
                # 模拟比分（含 elo_delta 修正 + 情境调整）
                la, lb = expected_goals(teams, home, away, league, league_context,
                                        elo_delta_a=team_elo_delta.get(home, 0.0),
                                        elo_delta_b=team_elo_delta.get(away, 0.0),
                                        xg_adjust_a=match_ctx["xG_adjust_a"],
                                        xg_adjust_b=match_ctx["xG_adjust_b"],
                                        home_adv_mult=match_ctx["home_adv_multiplier"],
                                        tactical=tactical_lookup.get(frozenset((home, away)), {}))
                # 应用冲击（含情境方差 + 弱队方差放大）
                shock_sd = config.get("shock_sd", 0.28) * match_ctx["shock_multiplier"]
                avg_elo = (teams[home].get("elo", 1500) + teams[away].get("elo", 1500)) / 2
                _wt = config.get("weak_elo_threshold", WEAK_TEAM_ELO_THRESHOLD)
                if avg_elo < _wt:
                    boost = 1.0 + (_wt - avg_elo) / _wt
                    shock_sd *= min(boost, 1.4)
                shock = random.gauss(0.0, shock_sd)
                la = max(0.15, la + shock / 2)
                lb = max(0.15, lb - shock / 2)
                x, y = _sample(la), _sample(lb)

            sim_gf[home] += x
            sim_gf[away] += y
            sim_gd[home] += x - y
            sim_gd[away] += y - x

            if x > y:
                sim_points[home] += 3
            elif x < y:
                sim_points[away] += 3
            else:
                sim_points[home] += 1
                sim_points[away] += 1

        # 排名
        sorted_codes = sorted(
            codes,
            key=lambda c: (sim_points[c], sim_gd[c], sim_gf[c], random.random()),
            reverse=True
        )

        # 记录
        for pos, code in enumerate(sorted_codes):
            rank_counts[code][pos] += 1

        # 争冠 (第1名)
        title_counts[sorted_codes[0]] += 1

        # 欧战 (前N名)
        for code in sorted_codes[:euro_spots]:
            top_euro_counts[code] += 1

        # 降级 (后M名)
        if rel_spots > 0:
            for code in sorted_codes[-rel_spots:]:
                relegation_counts[code] += 1

    # 输出
    print(f"\n  争冠概率 (第1名):")
    for code in sorted(codes, key=lambda c: -title_counts[c])[:5]:
        print(f"    {cn(teams, code):<12} {title_counts[code] / sims * 100:5.1f}%")

    print(f"\n  欧战资格概率 (前{euro_spots}名):")
    for code in sorted(codes, key=lambda c: -top_euro_counts[c])[:8]:
        print(f"    {cn(teams, code):<12} {top_euro_counts[code] / sims * 100:5.1f}%")

    if rel_spots > 0:
        print(f"\n  降级概率 (后{rel_spots}名):")
        for code in sorted(codes, key=lambda c: -relegation_counts[c])[:6]:
            print(f"    {cn(teams, code):<12} {relegation_counts[code] / sims * 100:5.1f}%")

    print(f"\n  预测最终排名 (概率最高位次):")
    for code in codes:
        most_likely = max(range(len(codes)), key=lambda pos: rank_counts[code][pos])
        prob = rank_counts[code][most_likely] / sims * 100
        print(f"    {cn(teams, code):<12} 第{most_likely + 1}名  ({prob:.1f}%)")

    print()


# ================================================================
# 主函数
# ================================================================

def main():
    p = argparse.ArgumentParser(description="联赛预测引擎 — 挪超/瑞超/MLS")
    p.add_argument("cmd", choices=["match", "table", "title", "relegation", "europe", "simulate"])
    p.add_argument("target", nargs="?", help="球队名/联赛名")
    p.add_argument("opponent", nargs="?", help="对手名 (仅match)")
    p.add_argument("--league", choices=["eliteserien", "allsvenskan", "mls", "brasileirao", "eredivisie", "europa_league", "champions_league", "ucl_qualifying", "kleague", "veikkausliiga", "jleague", "libertadores", "ligue2", "laliga", "epl", "championship", "ligue1", "seriea", "bundesliga", "saudi_pro_league"],
                   required=True, help="联赛")
    p.add_argument("--sims", type=int, default=10000, help="模拟次数")
    p.add_argument("--seed", type=int, default=None, help="随机种子")
    p.add_argument("--neutral", action="store_true", help="中立场（取消主场优势）")
    p.add_argument("--host", default=None, help="东道主球队（单场预测用，标记其获得东道主加成）")
    p.add_argument("--round", type=int, default=None, help="当前轮次")
    p.add_argument("--date", default=None, help="比赛日期 YYYY-MM-DD（用于预测台账记录）")
    p.add_argument("--no-record", action="store_true", help="跳过写预测台账（仅情景试验/纯试算用；默认必入台账）")
    p.add_argument("--odds", default=None,
                   help="盘口数据 JSON 文件路径（{'matches':[{home,away,handicap_line/handicap,water_home,water_away}]}），"
                        "用于盘口分析 + 诱盘检测（可选）")

    args = p.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    # 加载数据
    config = load_league_config(args.league)
    teams = load_league_teams(args.league)
    league_context = load_league_context(args.league)
    current_table = load_current_table(args.league)
    fixtures = load_fixtures(args.league)
    intelligence = load_intelligence(args.league)
    locked = load_locked_results(args.league)
    results_data = load_results_data(args.league)

    if args.cmd == "match":
        if not args.target or not args.opponent:
            p.error("match 需要两个球队名")
        a = resolve_team(teams, args.target)
        b = resolve_team(teams, args.opponent)
        if args.host:
            host_code = resolve_team(teams, args.host)
            if host_code not in (a, b):
                p.error(f"--host {args.host} 必须是本场比赛两队之一")
            teams[host_code]["host"] = True
        odds_data = None
        if args.odds:
            with open(args.odds, encoding="utf-8") as f:
                odds_data = json.load(f)
        
        # 加载特定比赛的情报
        match_intelligence = load_intelligence_for_match(args.league, a, b)
        
        cmd_match(teams, a, b, args.league, match_intelligence, {}, args.neutral, current_table,
                  results_data, date=args.date, record=not args.no_record, odds_data=odds_data)

    elif args.cmd == "table":
        if not current_table:
            print("⚠️ 未找到当前积分榜，请先创建 data/live/{league}/table.json")
            return
        if not fixtures:
            print("⚠️ 未找到剩余赛程，请先创建 data/live/{league}/fixtures.json")
            return
        cmd_table(teams, args.league, current_table, fixtures, locked, args.sims, args.seed, intelligence, results_data)

    elif args.cmd == "title":
        # 简化版：只输出争冠概率
        if not current_table or not fixtures:
            print("⚠️ 需要积分榜和赛程数据")
            return
        cmd_table(teams, args.league, current_table, fixtures, locked, args.sims, args.seed, intelligence)

    elif args.cmd == "relegation":
        # 同上
        if not current_table or not fixtures:
            print("⚠️ 需要积分榜和赛程数据")
            return
        cmd_table(teams, args.league, current_table, fixtures, locked, args.sims, args.seed, intelligence)

    elif args.cmd == "europe":
        if not current_table or not fixtures:
            print("⚠️ 需要积分榜和赛程数据")
            return
        cmd_table(teams, args.league, current_table, fixtures, locked, args.sims, args.seed, intelligence)

    elif args.cmd == "simulate":
        if not current_table or not fixtures:
            print("⚠️ 需要积分榜和赛程数据")
            return
        cmd_table(teams, args.league, current_table, fixtures, locked, args.sims, args.seed, intelligence)


if __name__ == "__main__":
    main()