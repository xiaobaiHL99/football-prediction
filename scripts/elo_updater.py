#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Elo 自动更新器 — 根据真实赛果动态更新球队评级

从 data/live/<league>/results.json 读取已有赛果，
按标准 Elo 公式更新 references/<league>/teams.json 中的 elo 值。

Usage:
  python3 scripts/elo_updater.py eliteserien              # 常规更新
  python3 scripts/elo_updater.py kleague --dry-run        # 预览不动文件
  python3 scripts/elo_updater.py allsvenskan --k 36       # 自定义K值
  python3 scripts/elo_updater.py allsvenskan --reset      # 重置追踪，重新处理所有比赛
"""

import json
import os
import sys
import argparse
import hashlib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 联赛默认K值（可调）
DEFAULT_K = {
    "eliteserien": 30,
    "allsvenskan": 28,
    "kleague": 28,
    "mls": 32,
    "brasileirao": 28,
    "europa_league": 24,
    "champions_league": 24,
    "ucl_qualifying": 24,
    "veikkausliiga": 26,
    "jleague": 28,
    "libertadores": 28,
    "ligue2": 28,
    "laliga": 28,
    "eredivisie": 28,
    "epl": 28,
    "ligue1": 28,
    "seriea": 28,
    "bundesliga": 28,
}

# 净胜球 → K值乘数
GD_MULTIPLIER = {
    0: 0.6,    # 平局K值衰减（平局信息量小于胜负）
    1: 1.0,    # 1球小胜 = 基准K
    2: 1.5,    # 2球差距
    3: 2.0,    # 3球差距
    4: 2.5,    # 4+球差距
}


def load_teams(league: str) -> dict:
    """加载球队数据"""
    path = os.path.join(BASE, "references", league, "teams.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_teams(league: str, data: dict):
    """写回球队数据"""
    path = os.path.join(BASE, "references", league, "teams.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✅ 已更新 {path}")


def load_results(league: str) -> list:
    """加载赛果"""
    path = os.path.join(BASE, "data", "live", league, "results.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("matches", [])


def load_tracker(league: str) -> dict:
    """加载处理追踪"""
    path = os.path.join(BASE, "data", "live", league, "elo_tracker.json")
    if not os.path.exists(path):
        return {"processed": 0}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_tracker(league: str, tracker: dict):
    """保存处理追踪"""
    path = os.path.join(BASE, "data", "live", league, "elo_tracker.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2)


def expected_score(rating_a: float, rating_b: float) -> float:
    """标准 Elo 预期得分：A队对B队的期望值 (0~1)"""
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


def get_k_multiplier(goal_diff: int) -> float:
    """根据净胜球返回K值乘数"""
    gd = abs(goal_diff)
    if gd >= 4:
        return GD_MULTIPLIER[4]
    return GD_MULTIPLIER.get(gd, 1.0)


def match_result_to_score(hg: int, ag: int) -> float:
    """比分 → 主队实际得分"""
    if hg > ag:
        return 1.0
    elif hg == ag:
        return 0.5
    else:
        return 0.0


def update_elo(teams: dict, home: str, away: str, hg: int, ag: int, k_base: int):
    """对一场比赛更新两队 Elo，返回变化量"""
    home = str(home).upper()
    away = str(away).upper()

    if home not in teams:
        print(f"  ⚠️ 未找到主队 {home}，跳过")
        return 0, 0
    if away not in teams:
        print(f"  ⚠️ 未找到客队 {away}，跳过")
        return 0, 0

    elo_h = teams[home].get("elo", 1500)
    elo_a = teams[away].get("elo", 1500)

    exp_h = expected_score(elo_h, elo_a)
    score_h = match_result_to_score(hg, ag)

    goal_diff = abs(hg - ag)
    k = k_base * get_k_multiplier(goal_diff)

    delta = round(k * (score_h - exp_h))
    # 防止单场波动过大（上限±40）
    delta = max(-40, min(40, delta))

    new_h = elo_h + delta
    new_a = elo_a - delta

    teams[home]["elo"] = new_h
    teams[away]["elo"] = new_a

    return delta, -delta


def get_league_names(teams_data: dict) -> dict:
    """球队代码 → 中文名"""
    return {code: t.get("name_cn", code) for code, t in teams_data.items()}


def fmt(teams_data: dict, code: str) -> str:
    """格式化球队名"""
    return teams_data.get(code, {}).get("name_cn", code)


def main():
    p = argparse.ArgumentParser(description="Elo 自动更新器")
    p.add_argument("league", help="联赛代码 (eliteserien/allsvenskan/kleague 等)")
    p.add_argument("--k", type=int, default=None, help="K 值（覆盖默认）")
    p.add_argument("--dry-run", action="store_true", help="仅预览，不写文件")
    p.add_argument("--reset", action="store_true", help="重置追踪，重新处理所有比赛")
    args = p.parse_args()

    league = args.league
    k_base = args.k or DEFAULT_K.get(league, 30)

    # 加载数据
    data = load_teams(league)
    teams = data["teams"]
    names = get_league_names(teams)

    results = load_results(league)
    tracker = load_tracker(league)

    if not results:
        print(f"⚠️ {league} 没有赛果数据 (data/live/{league}/results.json)")
        return

    # 决定处理哪些比赛
    start_idx = 0 if args.reset else tracker.get("processed", 0)
    new_matches = results[start_idx:]

    if not new_matches:
        print(f"📭 {league} 无新赛果需处理（已处理 {start_idx} 场）")
        return

    print(f"\n{'=' * 50}")
    print(f"  {league} Elo 更新 ({k_base=})")
    print(f"  已处理 {start_idx} 场，新增 {len(new_matches)} 场")
    if args.dry_run:
        print(f"  🔍 DRY RUN 模式 — 不会写文件")
    print(f"{'=' * 50}\n")

    total_delta = {}
    for code in teams:
        total_delta[code] = 0

    for i, m in enumerate(new_matches):
        home = m["home"]
        away = m["away"]
        hg = m["hg"]
        ag = m["ag"]

        old_h = teams[home]["elo"]
        old_a = teams[away]["elo"]
        dh, da = update_elo(teams, home, away, hg, ag, k_base)

        total_delta[home] += dh
        total_delta[away] += da

        # 显示
        result_icon = "H" if hg > ag else ("D" if hg == ag else "A")
        print(f"  {start_idx + i + 1:3d}. {fmt(teams, home):<10} {hg}-{ag} {fmt(teams, away):<10} "
              f"({result_icon})  "
              f"{fmt(teams, home)} {old_h}→{teams[home]['elo']} ({dh:+d})  "
              f"{fmt(teams, away)} {old_a}→{teams[away]['elo']} ({da:+d})")

    # 汇总
    print(f"\n  {'=' * 40}")
    print(f"  累积变化汇总:")
    sorted_deltas = sorted(total_delta.items(), key=lambda x: -abs(x[1]))
    for code, delta in sorted_deltas:
        if delta != 0:
            print(f"    {fmt(teams, code):<12} {delta:+d}  → 当前 Elo: {teams[code]['elo']}")

    # 写文件
    if not args.dry_run:
        new_tracker = {"processed": start_idx + len(new_matches)}
        save_teams(league, data)
        save_tracker(league, new_tracker)
        print(f"\n  📝 追踪已更新: {new_tracker['processed']} 场已处理")
    else:
        print(f"\n  🔍 DRY RUN — 未写入任何文件")

    print()


if __name__ == "__main__":
    main()
