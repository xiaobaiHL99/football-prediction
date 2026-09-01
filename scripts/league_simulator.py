#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联赛专用模拟器 — 完整赛季蒙特卡洛模拟

对挪超/瑞超/MLS 进行整赛季模拟，输出：
- 最终积分榜概率分布
- 争冠概率
- 欧战资格概率
- 降级概率
- 每队预测排名

Usage:
  league_simulator.py eliteserien --sims 10000
  league_simulator.py allsvenskan --sims 10000 --seed 7
  league_simulator.py mls --sims 5000 --conference
  league_simulator.py eliteserien --round 16 --sims 5000
"""

import json
import math
import random
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from league_data import (
    load_league_config,
    load_league_teams,
    load_league_context,
    load_current_table,
    load_fixtures,
    load_locked_results,
    load_intelligence,
    load_results_data,
    cn,
    classify_match,
    parse_form,
    form_to_xg_adjust,
    count_meetings,
    MEETING_FAMILIARITY_THRESHOLD,
    FAMILIARITY_UNDERDOG_BOOST,
    FAMILIARITY_FAVORITE_PENALTY,
)

# ================================================================
# 常量
# ================================================================

MAX_GOALS = 10
MAX_ELO_INTEL_DELTA = 30.0   # 单场情报Elo修正上限（v2收紧，防止过度修正）
ELO_NEGATIVE_DAMP = 0.75      # 负向Elo修正衰减系数（防守韧性补偿）
WEAK_TEAM_ELO_THRESHOLD = 1550.0  # 弱队判定阈值（均Elo低于此值时加大方差）
HOST_BONUS = 0.15                 # 东道主加成（xG），借鉴世界杯 host 逻辑

SHOCK_POINTS = [(-2.0, 0.0545), (-1.0, 0.2442), (0.0, 0.4026), (1.0, 0.2442), (2.0, 0.0545)]


# ================================================================
# 工具函数
# ================================================================

def clamp(value, low, high):
    return max(low, min(high, value))


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def bar(p, width=20):
    return "█" * round(p * width) + "·" * (width - round(p * width))


# ================================================================
# 预期进球（复用 league_predict 逻辑）
# ================================================================

def expected_goals(teams, a, b, config, league_context,
                   goal_delta_a=0.0, goal_delta_b=0.0,
                   elo_delta_a=0.0, elo_delta_b=0.0,
                   xg_adjust_a=0.0, xg_adjust_b=0.0,
                   home_adv_mult=1.0):
    """计算预期进球，包含联赛特殊因子和 elo_delta 修正"""
    avg_goals = config["avg_goals"]
    home_adv = config["home_adv"] * home_adv_mult

    # 东道主加成：球队 teams.<code>.host=true 时在 xG 上叠加（世界杯借鉴）
    host_bonus = config.get("host_bonus", HOST_BONUS)
    host_a = host_bonus if teams[a].get("host", False) else 0.0
    host_b = host_bonus if teams[b].get("host", False) else 0.0

    elo_scale = config["elo_scale"]

    # 防守韧性补偿：负向Elo修正只按比例生效
    # 因为防守体系、战术纪律能部分弥补人员缺阵
    damp_a = ELO_NEGATIVE_DAMP if elo_delta_a < 0 else 1.0
    damp_b = ELO_NEGATIVE_DAMP if elo_delta_b < 0 else 1.0

    sup = ((teams[a]["elo"] + elo_delta_a * damp_a) -
           (teams[b]["elo"] + elo_delta_b * damp_b)) / elo_scale

    base_la = avg_goals / 2 + sup / 2 + home_adv + host_a
    base_lb = avg_goals / 2 - sup / 2 + host_b

    # 特殊因子
    special_delta_a, special_delta_b = apply_special_factors(teams, a, b, league_context)

    la = max(0.20, base_la + goal_delta_a + special_delta_a + xg_adjust_a)
    lb = max(0.20, base_lb + goal_delta_b + special_delta_b + xg_adjust_b)

    return la, lb


def apply_special_factors(teams, a, b, league_context):
    """应用联赛特殊因子，返回 (delta_a, delta_b)"""
    delta_a = 0.0
    delta_b = 0.0
    factors = league_context.get("special_factors", {})

    # 人工草皮惩罚
    if "artificial_turf_penalty" in factors:
        turf_map = league_context.get("turf_map", {})
        artificial = set(turf_map.get("artificial", []))
        natural = set(turf_map.get("natural", []))
        penalty = as_float(factors["artificial_turf_penalty"].get("value", -0.10))
        if a in artificial and b in natural:
            delta_b += penalty
        elif b in artificial and a in natural:
            delta_a += penalty

    # 欧战消耗
    euro_key = next((k for k in factors if "europe" in k.lower() and "distraction" in k.lower()), None)
    if euro_key:
        europe_teams = set(league_context.get("europe_teams", []))
        euro_val = as_float(factors[euro_key].get("value", -0.08))
        if a in europe_teams:
            delta_a += euro_val
        if b in europe_teams:
            delta_b += euro_val

    # 旅行距离 (通用)
    if "travel_distance" in factors:
        region_map = {code: t.get("region", "") for code, t in teams.items()}
        travel_val = as_float(factors["travel_distance"].get("value", -0.08))
        if region_map.get(a, "") and region_map.get(b, "") and region_map.get(a) != region_map.get(b):
            delta_a += travel_val
            delta_b += travel_val

    # 北方球队主场优势（挪超）
    north_key = next((k for k in factors if "northern" in k.lower()), None)
    if north_key:
        region_map = {code: t.get("region", "") for code, t in teams.items()}
        north_val = as_float(factors[north_key].get("value", 0.06))
        if region_map.get(a) == "north" and region_map.get(b) != "north":
            delta_a += north_val
        elif region_map.get(b) == "north" and region_map.get(a) != "north":
            delta_b += north_val

    # 高温高湿 (巴甲)
    if "heat_humidity_penalty" in factors:
        region_map = {code: t.get("region", "") for code, t in teams.items()}
        heat_val = as_float(factors["heat_humidity_penalty"].get("value", 0.06))
        if region_map.get(a) == "northeast" and region_map.get(b) != "northeast":
            delta_a += heat_val

    # 高海拔 (巴甲)
    if "altitude_advantage" in factors:
        region_map = {code: t.get("region", "") for code, t in teams.items()}
        alt_val = as_float(factors["altitude_advantage"].get("value", 0.05))
        if region_map.get(a) == "centralwest" and region_map.get(b) not in ("centralwest", ""):
            delta_a += alt_val

    # 解放者杯消耗 (巴甲)
    lib_key = next((k for k in factors if "libertadores" in k.lower()), None)
    if lib_key:
        euro_teams = set(league_context.get("europe_teams", []))
        lib_val = as_float(factors[lib_key].get("value", -0.08))
        if a in euro_teams:
            delta_a += lib_val
        if b in euro_teams:
            delta_b += lib_val

    # 周中紧凑赛程 (欧罗巴)
    thu_key = next((k for k in factors if "thursday" in k.lower()), None)
    if thu_key:
        euro_teams = set(league_context.get("europe_teams", []))
        thu_val = as_float(factors[thu_key].get("value", -0.06))
        if a in euro_teams:
            delta_a += thu_val
        if b in euro_teams:
            delta_b += thu_val

    # 天气差异 (欧罗巴)
    weather_key = next((k for k in factors if "weather" in k.lower()), None)
    if weather_key:
        region_index = league_context.get("region_index", {})
        idx_a = region_index.get(teams[a].get("region", ""), 1)
        idx_b = region_index.get(teams[b].get("region", ""), 1)
        weather_val = as_float(factors[weather_key].get("value", -0.05))
        if abs(idx_a - idx_b) >= 2:
            delta_a += weather_val
            delta_b += weather_val

    # 夏季高温惩罚 (韩职)
    if "summer_heat_penalty" in factors:
        val = as_float(factors["summer_heat_penalty"].get("value", -0.04))
        delta_a += val
        delta_b += val

    # 军旅球队 (韩职 金泉尚武)
    if "military_team" in factors:
        val = as_float(factors["military_team"].get("value", -0.05))
        military_name = "金泉尚武"
        if teams[a].get("name_cn", "") == military_name:
            delta_a += val
        if teams[b].get("name_cn", "") == military_name:
            delta_b += val

    return delta_a, delta_b


# ================================================================
# 单场模拟
# ================================================================

def simulate_match(teams, home, away, config, league_context, locked,
                   elo_delta_a=0.0, elo_delta_b=0.0, table=None,
                   form_delta_a=0.0, form_delta_b=0.0,
                   familiarity_adj_a=0.0, familiarity_adj_b=0.0):
    """模拟一场比赛，返回 (home_goals, away_goals)。若有锁定结果则使用之。"""
    locked_key = frozenset((home, away))
    if locked_key in locked:
        h_team, hg, ag = locked[locked_key]
        return (hg, ag) if h_team == home else (ag, hg)

    # 比赛情境分类
    match_ctx = classify_match(
        teams, home, away,
        elo_delta_a=elo_delta_a, elo_delta_b=elo_delta_b,
        table=table, league_context=league_context
    )

    # 叠加近期状态 + 交手熟悉度修正
    match_ctx["xG_adjust_a"] += form_delta_a + familiarity_adj_a
    match_ctx["xG_adjust_b"] += form_delta_b + familiarity_adj_b

    la, lb = expected_goals(teams, home, away, config, league_context,
                            elo_delta_a=elo_delta_a, elo_delta_b=elo_delta_b,
                            xg_adjust_a=match_ctx["xG_adjust_a"],
                            xg_adjust_b=match_ctx["xG_adjust_b"],
                            home_adv_mult=match_ctx["home_adv_multiplier"])
    shock_sd = config.get("shock_sd", 0.28) * match_ctx["shock_multiplier"]

    # 弱队方差放大（阈值可被联赛 config 覆盖）
    avg_elo = (teams[home].get("elo", 1500) + teams[away].get("elo", 1500)) / 2
    _wt = config.get("weak_elo_threshold", WEAK_TEAM_ELO_THRESHOLD)
    if avg_elo < _wt:
        weak_boost = 1.0 + (_wt - avg_elo) / _wt
        shock_sd *= min(weak_boost, 1.4)

    shock = random.gauss(0.0, shock_sd)
    la = max(0.15, la + shock / 2)
    lb = max(0.15, lb - shock / 2)

    return _sample(la), _sample(lb)


# ================================================================
# 完整赛季模拟
# ================================================================

def simulate_season(teams, config, league_context, fixtures, points, gd, gf, locked,
                    team_elo_delta=None, table=None,
                    team_form_delta=None, results_data=None):
    """模拟一次完整赛季，返回 (sorted_codes, points_dict, gd_dict, gf_dict)"""
    if team_elo_delta is None:
        team_elo_delta = {}
    if team_form_delta is None:
        team_form_delta = {}
    if results_data is None:
        results_data = {"matches": []}
    sim_points = points.copy()
    sim_gd = gd.copy()
    sim_gf = gf.copy()
    codes = list(teams.keys())

    for match in fixtures:
        home = str(match["home"]).upper()
        away = str(match["away"]).upper()

        if home not in teams or away not in teams:
            continue

        eda = team_elo_delta.get(home, 0.0)
        edb = team_elo_delta.get(away, 0.0)

        # 交手熟悉度修正
        mt = count_meetings(results_data, home, away)
        fam_a = 0.0
        fam_b = 0.0
        if mt >= MEETING_FAMILIARITY_THRESHOLD:
            elo_h = teams[home].get("elo", 1500)
            elo_a = teams[away].get("elo", 1500)
            if elo_h < elo_a:
                fam_a = FAMILIARITY_UNDERDOG_BOOST
                fam_b = FAMILIARITY_FAVORITE_PENALTY
            elif elo_a < elo_h:
                fam_b = FAMILIARITY_UNDERDOG_BOOST
                fam_a = FAMILIARITY_FAVORITE_PENALTY

        x, y = simulate_match(
            teams, home, away, config, league_context, locked, eda, edb, table=table,
            form_delta_a=team_form_delta.get(home, 0.0),
            form_delta_b=team_form_delta.get(away, 0.0),
            familiarity_adj_a=fam_a, familiarity_adj_b=fam_b
        )

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

    sorted_codes = sorted(
        codes,
        key=lambda c: (sim_points[c], sim_gd[c], sim_gf[c], random.random()),
        reverse=True
    )
    return sorted_codes, sim_points, sim_gd, sim_gf


# ================================================================
# 统计聚合
# ================================================================

def run_simulation(teams, config, league_context, fixtures, current_table, locked, sims, seed=None,
                   intelligence=None, results_data=None):
    """运行蒙特卡洛模拟并统计"""
    if seed is not None:
        random.seed(seed)

    if intelligence is None:
        intelligence = {}

    if results_data is None:
        results_data = {"matches": []}

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
    n = len(codes)

    # 统计数组
    rank_counts = {c: [0] * n for c in codes}
    title_counts = {c: 0 for c in codes}

    # 欧战名额配置：挪超/瑞超/巴甲前N名
    euro_spots = config.get("europa_spots", 4)
    euro_counts = {c: 0 for c in codes}
    # 降级：挪超/瑞超后2名，巴甲后4名，MLS/欧联不降级
    relegation_spots = config.get("relegation_spots", 2 if config["name"] not in ("Major League Soccer", "UEFA Europa League") else 0)
    relegation_counts = {c: 0 for c in codes}

    # 当前积分
    points = {c: 0 for c in codes}
    gd = {c: 0 for c in codes}
    gf = {c: 0 for c in codes}
    for code, data in current_table.items():
        code = str(code).upper()
        if code in points:
            points[code] = data.get("points", 0)
            gd[code] = data.get("gd", 0)
            gf[code] = data.get("gf", 0)

    for _ in range(sims):
        sorted_codes, _, _, _ = simulate_season(
            teams, config, league_context, fixtures, points, gd, gf, locked, team_elo_delta,
            table=current_table, team_form_delta=team_form_delta, results_data=results_data
        )

        # 记录排名
        for pos, code in enumerate(sorted_codes):
            rank_counts[code][pos] += 1

        # 争冠（第1名）
        title_counts[sorted_codes[0]] += 1

        # 欧战（前N名）
        for code in sorted_codes[:euro_spots]:
            euro_counts[code] += 1

        # 降级（后M名）
        if relegation_spots > 0:
            for code in sorted_codes[-relegation_spots:]:
                relegation_counts[code] += 1

    return {
        "rank_counts": rank_counts,
        "title_counts": title_counts,
        "euro_counts": euro_counts,
        "relegation_counts": relegation_counts,
        "codes": codes,
        "n": n,
        "sims": sims,
        "relegation_spots": relegation_spots,
        "euro_spots": euro_spots,
    }


# ================================================================
# 输出
# ================================================================

def print_results(stats, teams, config):
    """打印模拟结果"""
    codes = stats["codes"]
    sims = stats["sims"]
    rank_counts = stats["rank_counts"]
    title_counts = stats["title_counts"]
    euro_counts = stats["euro_counts"]
    relegation_counts = stats["relegation_counts"]

    print(f"\n{'=' * 60}")
    print(f"  🏟 {config['name_cn']} 完整赛季模拟 ({sims} 次)")
    print(f"  赛季: {config.get('season', '2026')}  |  球队: {len(codes)}  |  轮次: {config.get('rounds', 30)}")
    print(f"{'=' * 60}")

    # 争冠概率
    print(f"\n  🏆 争冠概率 (第1名):")
    for code in sorted(codes, key=lambda c: -title_counts[c])[:5]:
        pct = title_counts[code] / sims * 100
        print(f"    {cn(teams, code):<14} {pct:5.1f}%  {bar(pct / 100)}")

    # 欧战概率
    print(f"\n  ⚽ 欧战资格概率 (前{stats['euro_spots']}名):")
    for code in sorted(codes, key=lambda c: -euro_counts[c])[:8]:
        pct = euro_counts[code] / sims * 100
        print(f"    {cn(teams, code):<14} {pct:5.1f}%  {bar(pct / 100)}")

    # 降级概率
    if stats["relegation_spots"] > 0:
        print(f"\n  ⬇ 降级概率 (后{stats['relegation_spots']}名):")
        for code in sorted(codes, key=lambda c: -relegation_counts[c])[:4]:
            pct = relegation_counts[code] / sims * 100
            print(f"    {cn(teams, code):<14} {pct:5.1f}%  {bar(pct / 100)}")

    # 预测最终排名
    print(f"\n  📊 预测最终排名:")
    # 按最频繁排名位置排序
    code_ranks = []
    for code in codes:
        most_likely = max(range(stats["n"]), key=lambda pos: rank_counts[code][pos])
        prob = rank_counts[code][most_likely] / sims * 100
        code_ranks.append((most_likely, -prob, code))
    code_ranks.sort()

    for rank, neg_prob, code in code_ranks:
        prob = -neg_prob
        pos = 1 + rank
        bar_width = min(prob / 3, 20)
        print(f"    {pos:2d}. {cn(teams, code):<14} 第{pos}名 ({prob:.1f}%)  {'█' * int(bar_width)}")

    print()


# ================================================================
# 主函数
# ================================================================

def main():
    p = argparse.ArgumentParser(description="联赛完整赛季模拟器")
    p.add_argument("league", choices=["eliteserien", "allsvenskan", "mls", "brasileirao", "eredivisie", "europa_league", "champions_league", "ucl_qualifying", "kleague", "veikkausliiga", "jleague", "libertadores", "ligue2", "laliga", "epl", "championship", "ligue1", "seriea", "bundesliga"],
                   help="联赛名称")
    p.add_argument("--sims", type=int, default=10000, help="模拟次数 (默认 10000)")
    p.add_argument("--seed", type=int, default=None, help="随机种子")
    p.add_argument("--round", type=int, default=None, help="当前轮次 (仅显示用)")
    p.add_argument("--conference", action="store_true",
                   help="MLS 模式：按分区输出东/西部前7")
    args = p.parse_args()

    config = load_league_config(args.league)
    teams = load_league_teams(args.league)
    league_context = load_league_context(args.league)
    current_table = load_current_table(args.league)
    fixtures = load_fixtures(args.league)
    locked = load_locked_results(args.league)
    intelligence = load_intelligence(args.league)
    results_data = load_results_data(args.league)

    if not current_table:
        print("⚠️ 未找到当前积分榜，请先创建 data/live/<league>/table.json")
        return

    if not fixtures:
        print("⚠️ 未找到剩余赛程，请先创建 data/live/<league>/fixtures.json")
        return

    stats = run_simulation(
        teams, config, league_context, fixtures, current_table,
        locked, args.sims, args.seed, intelligence, results_data
    )
    print_results(stats, teams, config)

    # MLS 分区统计（如果启用）
    if args.conference and args.league == "mls":
        conf_map = league_context.get("conference_map", {})
        for conf_name, conf_codes in conf_map.items():
            print(f"\n  📍 {conf_name.upper()} 分区排名:")
            conf_teams = [c for c in codes_in_conference(conf_codes, stats)]
            for code in conf_teams[:7]:
                avg_rank = sum(
                    pos * stats["rank_counts"][code][pos]
                    for pos in range(stats["n"])
                ) / stats["sims"]
                print(f"    {cn(teams, code):<16} 预期排名 {avg_rank:.1f}")
        print()


def codes_in_conference(conf_codes, stats):
    """按分区排序球队"""
    codes = [c for c in conf_codes if c in stats["rank_counts"]]
    return sorted(codes, key=lambda c: -stats["title_counts"].get(c, 0))


if __name__ == "__main__":
    main()
