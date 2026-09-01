#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日复盘 + 自动调参 — 预测台账的复盘与调参闭环。

用法:
  python3 scripts/daily_review.py review [--date 2026-08-02] [--league kleague]
  python3 scripts/daily_review.py tune   [--league kleague] [--dry-run]

数据流:
  预测 → data/reviews/predictions.json（台账，由 league_predict.py --record 写入）
  review: 台账(按 date) × data/live/<league>/results.json → 指标
          → reports/reviews/<date>-review.md + data/reviews/reviews.json（趋势）
  tune:   台账(匹配到结果) → 坐标下降微调 references/league_config.json
          （写回前备份 league_config.backup.json）→ data/reviews/tuning_log.json

约束（已在计划中说明）:
  - results.json 无日期 → 按 {home, away} 主客对匹配、每条目消费一次。
  - 过拟合防护: 最少样本门槛 + 时间序列留出验证（老样本训练 / 最新 20% 验证）+ 参数 clamp + 写回前备份。
  - 只写 data/reviews/ 与 references/league_config.json；不改动 data/live/ 既有快照。
"""

import argparse
import contextlib
import datetime
import json
import math
import os
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import ledger
import league_data
import league_predict as lp

MIN_ENTRIES = 30          # 每联赛调参最少样本
MIN_ENTRIES_GLOBAL = 50   # 全局因子调参最少样本
APPLY_LL_IMPROVE = 0.01   # 验证集 log-loss 至少改善这么多才写回
LOG_LOSS_EPS = 1e-6

_TEAMS_CACHE = {}
_RESULTS_CACHE = {}


# ================================================================
# 通用工具
# ================================================================

def outcome_of(m: dict) -> str:
    """由赛果 dict 判定实际方向 home/draw/away"""
    hg, ag = int(m["hg"]), int(m["ag"])
    return "home" if hg > ag else ("away" if hg < ag else "draw")


def entry_metrics(entry: dict, result: dict) -> dict:
    """单场复盘指标：方向/比分是否命中、log-loss、Brier"""
    pred = entry["prediction"]
    pw, pd, pl = pred["pw"], pred["pd"], pred["pl"]
    actual = outcome_of(result)
    dir_hit = pred["direction"] == actual
    score_hit = list(pred.get("main_score", [])) == [int(result["hg"]), int(result["ag"])]
    prob = {"home": pw, "draw": pd, "away": pl}[actual]
    log_loss = -math.log(max(prob, LOG_LOSS_EPS))
    yk = {"home": 0.0, "draw": 0.0, "away": 0.0}
    yk[actual] = 1.0
    brier = (pw - yk["home"]) ** 2 + (pd - yk["draw"]) ** 2 + (pl - yk["away"]) ** 2
    return {"actual": actual, "dir_hit": dir_hit, "score_hit": score_hit,
            "log_loss": log_loss, "brier": brier}


def _split_train_val(pairs, val_ratio=0.2, min_val=3):
    """时间序列切分：老样本训练 / 最新样本验证"""
    n = len(pairs)
    n_val = max(min_val, int(round(n * val_ratio)))
    n_val = min(n_val, n // 3)
    n_train = n - n_val
    return pairs[:n_train], pairs[n_train:]


def _rerun(entry: dict):
    """用台账里保存的输入快照重新跑一遍模型（参数已被调用方覆盖）。返回 (pw, pd, pl)"""
    league = entry["league"]
    if league not in _TEAMS_CACHE:
        _TEAMS_CACHE[league] = league_data.load_league_teams(league)
    if league not in _RESULTS_CACHE:
        _RESULTS_CACHE[league] = league_data.load_results_data(league)
    teams = _TEAMS_CACHE[league]
    home, away = entry["home"], entry["away"]
    for code, elo in (entry.get("model_inputs") or {}).get("elo", {}).items():
        if code in teams:
            teams[code] = dict(teams[code])  # 浅拷贝避免污染缓存
            teams[code]["elo"] = elo
    odds_match = (entry.get("model_inputs") or {}).get("odds_match")
    odds_data = {"matches": [odds_match]} if isinstance(odds_match, dict) else None
    e = lp.build_match_entry(teams, home, away, league,
                             entry.get("intelligence_snapshot") or {},
                             locked={}, neutral=False, current_table={},
                             results_data=_RESULTS_CACHE[league],
                             date=entry.get("date"), odds_data=odds_data)
    pred = e["prediction"]
    return pred["pw"], pred["pd"], pred["pl"]


def _avg_log_loss(pairs) -> float:
    """对给定 (entry, result) 列表重跑模型并算平均 log-loss"""
    total = 0.0
    for entry, result in pairs:
        pw, pd, pl = _rerun(entry)
        prob = {"home": pw, "draw": pd, "away": pl}[outcome_of(result)]
        total += -math.log(max(prob, LOG_LOSS_EPS))
    return total / max(len(pairs), 1)


@contextlib.contextmanager
def _config_override(league: str, params: dict):
    """临时覆盖某联赛的 league_config 参数（in-memory，不落盘）"""
    orig_lp = lp.load_league_config
    orig_ld = league_data.load_league_config

    def patched(lg):
        cfg = dict(orig_lp(lg))
        if lg == league:
            cfg.update(params)
        return cfg

    lp.load_league_config = patched
    league_data.load_league_config = patched
    try:
        yield
    finally:
        lp.load_league_config = orig_lp
        league_data.load_league_config = orig_ld


# ================================================================
# review — 每日复盘
# ================================================================

_DIR_CN = {"home": "押主队", "away": "押客队", "draw": "押平局"}
ELO_BUCKETS = (("近均衡(<50)", 0, 50), ("中等(50-150)", 50, 150), ("悬殊(>150)", 150, float("inf")))
GOAL_BUCKETS = (("小(<2.5)", 0, 2.5), ("中(2.5-3.4)", 2.5, 3.5), ("大(≥3.5)", 3.5, float("inf")))
BUCKET_MIN_N = 15        # 方向偏误/强项判定的最少样本
BUCKET_MIN_N_GOALS = 10  # 进球低估判定的最少样本
BUCKET_BIAS_MAX = 0.40   # 命中率 ≤ 此值 → 偏误
BUCKET_STRONG_MIN = 0.60 # 命中率 ≥ 此值 → 强项
BUCKET_GOAL_DIFF = 0.5   # 实际均球-预测均球 ≥ 此值 → 进球低估


def _collect_all_matched_pairs():
    """遍历全量台账 × 各联赛 results.json，收集所有已匹配 (entry, result)。"""
    by_league = defaultdict(list)
    for e in ledger.load_ledger()["predictions"]:
        by_league[e.get("league", "?")].append(e)
    pairs = []
    for league, entries in by_league.items():
        results_data = league_data.load_results_data(league)
        for entry, result in ledger.match_results(entries, results_data):
            if result is not None:
                pairs.append((entry, result))
    return pairs


def _bucket_stats(all_pairs) -> dict:
    """按情境分桶统计方向命中率与进球差（跨日期累计样本）。

    返回 {"total": n, "groups": {组名: {桶名: {n, dir_hit, dir_hit_rate, avg_log_loss,
            pred_avg_goals, actual_avg_goals, goal_diff, flag}}}}。flag 为 "" 表示常态。
    """
    groups = defaultdict(lambda: defaultdict(list))

    def add(group, name, rec):
        groups[group][name].append(rec)

    total = 0
    for entry, result in all_pairs:
        pred = entry.get("prediction") or {}
        mi = entry.get("model_inputs") or {}
        dir_ = pred.get("direction")
        if dir_ not in _DIR_CN:
            continue
        actual = outcome_of(result)
        rec = {"dir_hit": dir_ == actual,
               "ll": entry_metrics(entry, result)["log_loss"],
               "actual_goals": int(result["hg"]) + int(result["ag"])}
        la, lb = pred.get("la"), pred.get("lb")
        if la is not None and lb is not None:
            rec["pred_goals"] = la + lb
        else:
            ms = pred.get("main_score")
            rec["pred_goals"] = sum(ms) if ms else None
        elo = mi.get("elo") or {}
        home, away = entry.get("home"), entry.get("away")
        rec["elo_gap"] = abs(elo[home] - elo[away]) if (home in elo and away in elo) else None

        total += 1
        add("方向桶", _DIR_CN[dir_], rec)
        if rec["elo_gap"] is not None:
            for name, lo, hi in ELO_BUCKETS:
                if lo <= rec["elo_gap"] < hi:
                    add("Elo差桶", name, rec)
                    break
        if rec["pred_goals"] is not None:
            for name, lo, hi in GOAL_BUCKETS:
                if lo <= rec["pred_goals"] < hi:
                    add("预测总进球桶", name, rec)
                    break
        if rec["elo_gap"] is not None and rec["elo_gap"] < 50:
            add("钻取·近均衡", f"近均衡×{_DIR_CN[dir_]}", rec)
            if rec["pred_goals"] is not None:
                gb = "小球" if rec["pred_goals"] < 2.5 else ("大球" if rec["pred_goals"] >= 3.5 else "中球")
                add("钻取·近均衡", f"近均衡×预测{gb}", rec)

    out = {}
    for group, named in sorted(groups.items()):
        out[group] = {}
        for name, recs in named.items():
            n = len(recs)
            hit = sum(1 for r in recs if r["dir_hit"])
            rate = hit / n
            ll = sum(r["ll"] for r in recs) / n
            pn = sum(1 for r in recs if r["pred_goals"] is not None)
            pred_avg = sum(r["pred_goals"] for r in recs if r["pred_goals"] is not None) / pn if pn else None
            actual_avg = sum(r["actual_goals"] for r in recs) / n
            goal_diff = actual_avg - pred_avg if pred_avg is not None else None
            flags = []
            if n < BUCKET_MIN_N:
                flags.append(f"样本不足(需≥{BUCKET_MIN_N})")
            elif rate <= BUCKET_BIAS_MAX:
                flags.append("⚠️偏误")
            elif rate >= BUCKET_STRONG_MIN:
                flags.append("✅强项")
            if goal_diff is not None and goal_diff >= BUCKET_GOAL_DIFF and n >= BUCKET_MIN_N_GOALS:
                flags.append("⚠️进球低估")
            out[group][name] = {
                "n": n, "dir_hit": hit, "dir_hit_rate": round(rate, 3),
                "avg_log_loss": round(ll, 3),
                "pred_avg_goals": round(pred_avg, 2) if pred_avg is not None else None,
                "actual_avg_goals": round(actual_avg, 2),
                "goal_diff": round(goal_diff, 2) if goal_diff is not None else None,
                "flag": "；".join(flags),
            }
    return {"total": total, "groups": out}


def _render_bucket_md(buckets) -> list:
    """渲染「分桶校准」Markdown 段（追加在趋势段之后）。"""
    lines = [f"## 分桶校准（累计 {buckets['total']} 场已完赛）", "",
             "跨全部日期的累计已完赛样本，按情境分桶统计方向命中率与进球差。", ""]
    for group in ("方向桶", "Elo差桶", "预测总进球桶", "钻取·近均衡"):
        named = buckets["groups"].get(group)
        if not named:
            continue
        lines += [f"### {group}", "",
                  "| 桶 | 场次 | 命中率 | avg log-loss | 预测均球 | 实际均球 | 差值 | 标志 |",
                  "|----|----:|:----:|:----:|:----:|:----:|:----:|:----|"]
        for name, b in named.items():
            rate = f"{b['dir_hit_rate'] * 100:.1f}%" if b["dir_hit_rate"] is not None else "-"
            pred = f"{b['pred_avg_goals']:.1f}" if b["pred_avg_goals"] is not None else "-"
            diff = f"{b['goal_diff']:+.1f}" if b["goal_diff"] is not None else "-"
            lines.append(f"| {name} | {b['n']} | {rate} | {b['avg_log_loss']:.3f} | {pred} | "
                         f"{b['actual_avg_goals']:.1f} | {diff} | {b['flag']} |")
        lines.append("")
    flags = []
    for group, named in buckets["groups"].items():
        for name, b in named.items():
            for f in ("⚠️偏误", "✅强项", "⚠️进球低估"):
                if f in b["flag"]:
                    flags.append(f"{f}: {group}·{name}(n={b['n']})")
    if flags:
        lines.append(f"> 结论：{'；'.join(flags)}")
    lines.append(f"> 门槛：命中率 ≤{BUCKET_BIAS_MAX * 100:.0f}% 且 n≥{BUCKET_MIN_N} → ⚠️偏误；"
                 f"≥{BUCKET_STRONG_MIN * 100:.0f}% → ✅强项；进球差 ≥+{BUCKET_GOAL_DIFF:.1f} 且 n≥{BUCKET_MIN_N_GOALS} → ⚠️进球低估；"
                 "样本不足不下结论。")
    return lines


def _write_bucket_stats(buckets):
    """累计分桶统计落盘 data/reviews/bucket_stats.json，供后续规则落地消费。"""
    path = os.path.join(BASE, "data", "reviews", "bucket_stats.json")
    from datetime import datetime
    data = {"_meta": {"updated_at": datetime.now().isoformat(timespec="seconds"),
                      "n_matched": buckets.get("total", 0)},
            "buckets": buckets.get("groups", {})}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cmd_review(args):
    date = args.date or (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    entries_all = ledger.load_ledger()["predictions"]
    entries = ledger.filter_by_date(entries_all, date, args.league)
    if not entries:
        print(f"📭 {date} 无预测记录（台账为空或该日无预测）")
        return

    by_league = defaultdict(list)
    for e in entries:
        by_league[e.get("league", "?")].append(e)

    rows = []
    league_stats = {}
    overall = {"n": 0, "matched": 0, "pending": 0, "dir_hit": 0, "score_hit": 0,
               "ll_sum": 0.0, "brier_sum": 0.0}
    dir_cn = {"home": "主胜", "draw": "平局", "away": "客胜"}

    for league in sorted(by_league):
        results_data = league_data.load_results_data(league)
        pairs = ledger.match_results(by_league[league], results_data)
        st = {"n": len(pairs), "matched": 0, "pending": 0, "dir_hit": 0, "score_hit": 0,
              "ll_sum": 0.0, "brier_sum": 0.0}
        for entry, result in pairs:
            row = {
                "league": league,
                "home": entry.get("home_cn", entry.get("home")),
                "away": entry.get("away_cn", entry.get("away")),
                "dir": dir_cn.get(entry["prediction"]["direction"], entry["prediction"]["direction"]),
                "main": entry["prediction"].get("main_score"),
            }
            if result is None:
                row["status"] = "待结果"
                st["pending"] += 1
                overall["pending"] += 1
            else:
                m = entry_metrics(entry, result)
                row.update(status="已完赛", actual=[int(result["hg"]), int(result["ag"])],
                           dir_hit="✅" if m["dir_hit"] else "❌",
                           score_hit="✅" if m["score_hit"] else "❌",
                           ll=round(m["log_loss"], 3), brier=round(m["brier"], 3))
                st["matched"] += 1
                st["dir_hit"] += int(m["dir_hit"])
                st["score_hit"] += int(m["score_hit"])
                st["ll_sum"] += m["log_loss"]
                st["brier_sum"] += m["brier"]
                overall["matched"] += 1
                overall["dir_hit"] += int(m["dir_hit"])
                overall["score_hit"] += int(m["score_hit"])
                overall["ll_sum"] += m["log_loss"]
                overall["brier_sum"] += m["brier"]
            rows.append(row)
        league_stats[league] = st
        overall["n"] += st["n"]

    # 分桶校准（跨全部日期累计样本，验证情境偏误）
    buckets = _bucket_stats(_collect_all_matched_pairs())
    report = _render_review_md(date, rows, league_stats, overall, buckets)
    os.makedirs(os.path.join(BASE, "reports", "reviews"), exist_ok=True)
    report_path = os.path.join(BASE, "reports", "reviews", f"{date}-review.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    _append_review_summary(date, league_stats, overall)
    _write_bucket_stats(buckets)

    # 控制台摘要
    m = overall
    print(f"📋 复盘 {date}")
    print(f"  预测 {m['n']} 场 | 已完赛 {m['matched']} | 待结果 {m['pending']}")
    if m["matched"]:
        print(f"  方向命中率 {m['dir_hit']}/{m['matched']} = {m['dir_hit'] / m['matched'] * 100:.1f}%")
        print(f"  比分命中率 {m['score_hit']}/{m['matched']} = {m['score_hit'] / m['matched'] * 100:.1f}%")
        print(f"  平均 log-loss {m['ll_sum'] / m['matched']:.3f} | 平均 Brier {m['brier_sum'] / m['matched']:.3f}")
    print(f"  报告: {os.path.relpath(report_path, BASE)}")

    # 分桶校准控制台小结（只列达到门槛的异常桶）
    if buckets.get("total"):
        print(f"\n📊 分桶校准（累计 {buckets['total']} 场已完赛）")
        flagged = []
        for group, named in buckets["groups"].items():
            for name, b in named.items():
                if b["flag"]:
                    flagged.append((group, name, b))
        if not flagged:
            print("  暂无达到门槛的偏误/强项桶（样本继续积累）")
        for group, name, b in flagged[:6]:
            rate = f"{b['dir_hit_rate'] * 100:.1f}%" if b["dir_hit_rate"] is not None else "-"
            diff = f"{b['goal_diff']:+.1f}" if b["goal_diff"] is not None else "-"
            print(f"  {b['flag']} | {group}·{name}: {b['dir_hit']}/{b['n']}={rate} | 实际-预测均球 {diff}")

    # 诱盘判定复盘（当日预测若含盘口分析则追加评估段）
    review_trap_decisions(league=args.league, date=date)


def _render_review_md(date, rows, league_stats, overall, buckets=None) -> str:
    m = overall
    lines = [f"# 复盘报告 {date}", ""]
    lines.append("## 总体")
    lines.append("")
    if m["matched"]:
        lines.append(f"- 预测场次: {m['n']}（已完赛 {m['matched']}，待结果 {m['pending']}）")
        lines.append(f"- 方向命中率: **{m['dir_hit'] / m['matched'] * 100:.1f}%** ({m['dir_hit']}/{m['matched']})")
        lines.append(f"- 比分命中率: {m['score_hit'] / m['matched'] * 100:.1f}% ({m['score_hit']}/{m['matched']})")
        lines.append(f"- 平均 log-loss: **{m['ll_sum'] / m['matched']:.3f}**（越低越好，1.0≈随机猜胜平负）")
        lines.append(f"- 平均 Brier: {m['brier_sum'] / m['matched']:.3f}（越低越好，0.667≈随机）")
    else:
        lines.append(f"- 预测场次: {m['n']}（全部待结果）")
    lines.append("")
    lines.append("## 按联赛")
    lines.append("")
    lines.append("| 联赛 | 场次 | 已完赛 | 方向命中 | 比分命中 | avg log-loss | avg Brier |")
    lines.append("|------|-----:|------:|:------:|:------:|:----:|:----:|")
    for lg in sorted(league_stats):
        s = league_stats[lg]
        if s["matched"]:
            lines.append(f"| {lg} | {s['n']} | {s['matched']} | "
                         f"{s['dir_hit'] / s['matched'] * 100:.1f}% | {s['score_hit'] / s['matched'] * 100:.1f}% | "
                         f"{s['ll_sum'] / s['matched']:.3f} | {s['brier_sum'] / s['matched']:.3f} |")
        else:
            lines.append(f"| {lg} | {s['n']} | {s['matched']} | - | - | - | - |")
    lines.append("")
    lines.append("## 逐场明细")
    lines.append("")
    lines.append("| 主队 | 客队 | 预测方向 | 预测比分 | 实际比分 | 方向 | 比分 | log-loss |")
    lines.append("|------|------|:------:|:------:|:------:|:----:|:----:|:----:|")
    for r in rows:
        if r["status"] == "待结果":
            lines.append(f"| {r['home']} | {r['away']} | {r['dir']} | {r['main'][0]}-{r['main'][1]} | 待赛 | - | - | - |")
        else:
            lines.append(f"| {r['home']} | {r['away']} | {r['dir']} | {r['main'][0]}-{r['main'][1]} | "
                         f"{r['actual'][0]}-{r['actual'][1]} | {r['dir_hit']} | {r['score_hit']} | {r['ll']} |")
    lines.append("")
    lines.append("## 趋势")
    lines.append("")
    lines.extend(_render_trend_md(date))
    lines.append("")
    if buckets and buckets.get("total"):
        lines.extend(_render_bucket_md(buckets))
    lines.append("> 说明: log-loss 为实际赛果对应预测概率的负对数；Brier 为三分量概率误差平方和。"
                 "results.json 无日期，按 {home,away} 主客对匹配、每条目消费一次。")
    lines.append("")
    return "\n".join(lines)


def _render_trend_md(current_date: str) -> list:
    path = os.path.join(BASE, "data", "reviews", "reviews.json")
    if not os.path.exists(path):
        return ["（暂无历史复盘）"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return ["（趋势文件损坏）"]
    items = [r for r in data.get("reviews", []) if r.get("date") != current_date]
    items = items[-14:]
    if not items:
        return ["（暂无历史复盘）"]
    lines = ["| 日期 | 场次 | 方向命中率 | avg log-loss |", "|------|-----:|:------:|:----:|"]
    for r in items:
        mm = r.get("matched", 0)
        dhit = r.get("dir_hit", 0)
        lines.append(f"| {r.get('date', '')} | {r.get('n', 0)} | "
                     f"{dhit / mm * 100:.1f}%" if mm else f"| {r.get('date','')} | {r.get('n',0)} | - | - |")
    return lines


def _append_review_summary(date, league_stats, overall):
    path = os.path.join(BASE, "data", "reviews", "reviews.json")
    data = {"_meta": {}, "reviews": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"_meta": {}, "reviews": []}
    from datetime import datetime
    data.setdefault("reviews", [])
    # 去重：同日期覆盖
    data["reviews"] = [r for r in data["reviews"] if r.get("date") != date]
    data["reviews"].append({
        "date": date,
        "n": overall["n"],
        "matched": overall["matched"],
        "pending": overall["pending"],
        "dir_hit": overall["dir_hit"],
        "score_hit": overall["score_hit"],
        "avg_log_loss": round(overall["ll_sum"] / overall["matched"], 4) if overall["matched"] else None,
        "avg_brier": round(overall["brier_sum"] / overall["matched"], 4) if overall["matched"] else None,
        "leagues": {lg: {
            "n": s["n"], "matched": s["matched"],
            "dir_hit_rate": round(s["dir_hit"] / s["matched"], 3) if s["matched"] else None,
            "avg_log_loss": round(s["ll_sum"] / s["matched"], 4) if s["matched"] else None,
        } for lg, s in league_stats.items()},
    })
    data["_meta"]["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ================================================================
# tune — 自动调参
# ================================================================

def _candidates(knob: str, base: float) -> list:
    if knob == "avg_goals":
        return [base * f for f in (0.93, 0.96, 1.04, 1.07)]
    if knob == "home_adv":
        return [max(0.10, min(0.80, base * f)) for f in (0.75, 0.88, 1.12, 1.25)]
    if knob == "probability_shrink":
        return [max(0.02, min(0.30, base + d)) for d in (-0.04, -0.02, 0.02, 0.04)]
    if knob == "shock_sd":
        return [max(0.15, min(0.50, base * f)) for f in (0.88, 0.94, 1.06, 1.12)]
    return []


def _tune_league(league: str, pairs, dry_run: bool):
    pairs = sorted(pairs, key=lambda pr: pr[0].get("date", ""))
    n = len(pairs)
    if n < MIN_ENTRIES:
        print(f"  ⏳ {league}: 已匹配结果 {n} 条 < {MIN_ENTRIES}，样本不足，继续积累")
        return
    train, val = _split_train_val(pairs)
    cfg = league_data.load_league_config(league)
    base_params = {
        "avg_goals": cfg["avg_goals"],
        "home_adv": cfg["home_adv"],
        "probability_shrink": cfg.get("probability_shrink", 0.10),
        "shock_sd": cfg.get("shock_sd", 0.28),
    }
    with _config_override(league, base_params):
        base_train_ll = _avg_log_loss(train)
        base_val_ll = _avg_log_loss(val)

    best_params = dict(base_params)
    best_train_ll = base_train_ll
    for knob in ("avg_goals", "home_adv", "probability_shrink", "shock_sd"):
        best_c = best_params[knob]
        for cand in _candidates(knob, best_params[knob]):
            params = dict(best_params)
            params[knob] = cand
            with _config_override(league, params):
                ll_train = _avg_log_loss(train)
            if ll_train < best_train_ll - 1e-9:
                best_train_ll = ll_train
                best_c = cand
        best_params[knob] = best_c

    with _config_override(league, best_params):
        best_val_ll = _avg_log_loss(val)

    improvement = base_val_ll - best_val_ll
    changes = {k: round(v, 4) for k, v in best_params.items() if abs(v - base_params[k]) > 1e-9}
    print(f"  {league}: 样本 {n} (train {len(train)} / val {len(val)}) | "
          f"val log-loss {base_val_ll:.3f} → {best_val_ll:.3f} (改善 {improvement:+.3f})")
    if not changes:
        print("    → 未发现更优参数")
        return
    print(f"    → 建议变更: {changes}")
    if improvement < APPLY_LL_IMPROVE:
        print(f"    → 验证集改善 {improvement:.3f} < {APPLY_LL_IMPROVE}，不写回（防过拟合）")
        return
    if dry_run:
        print("    → (dry-run) 未写回")
        return
    _write_league_config(league, changes)
    _log_tuning("league", league, {k: round(v, 4) for k, v in base_params.items()},
                changes, base_val_ll, best_val_ll, n)
    print("    → ✅ 已写回 references/league_config.json（原值已备份）")


def _write_league_config(league: str, changes: dict):
    path = os.path.join(BASE, "references", "league_config.json")
    backup = os.path.join(BASE, "references", "league_config.backup.json")
    if not os.path.exists(backup):
        import shutil
        shutil.copyfile(path, backup)
    with open(path, encoding="utf-8") as f:
        configs = json.load(f)
    for k, v in changes.items():
        configs[league][k] = v
    with open(path, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)


def _tune_global(pairs, dry_run: bool):
    n = len(pairs)
    if n < MIN_ENTRIES_GLOBAL:
        print(f"  ⏳ 全局因子: 总样本 {n} < {MIN_ENTRIES_GLOBAL}，跳过（当前值不变）")
        return
    pairs = sorted(pairs, key=lambda pr: pr[0].get("date", ""))
    train, val = _split_train_val(pairs)
    base_s = league_data.SPIRIT_XG_STEP
    base_f = league_data.FORM_XG_MAX

    def eval_(s, f, data):
        old_s, old_f = league_data.SPIRIT_XG_STEP, league_data.FORM_XG_MAX
        league_data.SPIRIT_XG_STEP = s
        league_data.FORM_XG_MAX = f
        try:
            return _avg_log_loss(data)
        finally:
            league_data.SPIRIT_XG_STEP = old_s
            league_data.FORM_XG_MAX = old_f

    base_train_ll = eval_(base_s, base_f, train)
    base_val_ll = eval_(base_s, base_f, val)
    best = (base_s, base_f)
    best_train = base_train_ll
    for s in (0.10, 0.12, 0.15, 0.18, 0.20):
        for f in (0.08, 0.10, 0.12):
            ll = eval_(s, f, train)
            if ll < best_train - 1e-9:
                best_train = ll
                best = (s, f)
    best_val_ll = eval_(best[0], best[1], val)
    improvement = base_val_ll - best_val_ll
    print(f"  全局: 样本 {n} (train {len(train)} / val {len(val)}) | "
          f"SPIRIT_XG_STEP {base_s}→{best[0]} | FORM_XG_MAX {base_f}→{best[1]} | "
          f"val log-loss {base_val_ll:.3f} → {best_val_ll:.3f} (改善 {improvement:+.3f})")
    if (best[0] == base_s and best[1] == base_f) or improvement < APPLY_LL_IMPROVE:
        print("    → 不写回（无变化或改善不足）")
        return
    if dry_run:
        print("    → (dry-run) 未写回 model_overrides.json")
        return
    _write_model_overrides(best[0], best[1])
    _log_tuning("global", "*",
                {"SPIRIT_XG_STEP": base_s, "FORM_XG_MAX": base_f},
                {"SPIRIT_XG_STEP": best[0], "FORM_XG_MAX": best[1]},
                base_val_ll, best_val_ll, n)
    print("    → ✅ 已写回 references/model_overrides.json")


def _write_model_overrides(spirit_step: float, form_max: float):
    path = os.path.join(BASE, "references", "model_overrides.json")
    from datetime import datetime
    data = {
        "_meta": {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "note": "自动调参写回的全局模型因子覆盖；league_data 在导入时应用（可手动改回后删除本文件）。",
        },
        "overrides": {"SPIRIT_XG_STEP": spirit_step, "FORM_XG_MAX": form_max},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _log_tuning(kind, league, before, after, before_ll, after_ll, n):
    path = os.path.join(BASE, "data", "reviews", "tuning_log.json")
    log = {"_meta": {}, "entries": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                log = json.load(f)
        except Exception:
            log = {"_meta": {}, "entries": []}
    from datetime import datetime
    log.setdefault("entries", []).append({
        "time": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "league": league,
        "before": before,
        "after": after,
        "before_val_ll": round(before_ll, 4),
        "after_val_ll": round(after_ll, 4),
        "n": n,
    })
    log["_meta"]["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def cmd_tune(args):
    print("🔧 自动调参（基于预测台账 + 已完赛结果）")
    print("=" * 60)
    all_entries = ledger.load_ledger()["predictions"]
    by_league = defaultdict(list)
    for e in all_entries:
        by_league[e.get("league", "?")].append(e)

    matched = defaultdict(list)   # league -> [(entry, result)]
    all_pairs = []
    for league, entries in by_league.items():
        if args.league and league != args.league:
            continue
        results_data = league_data.load_results_data(league)
        for entry, result in ledger.match_results(entries, results_data):
            if result is not None:
                matched[league].append((entry, result))
                all_pairs.append((entry, result))

    print("全局因子（战意幅度/状态幅度，跨联赛共享）:")
    _tune_global(all_pairs, args.dry_run)
    print()
    leagues = [args.league] if args.league else sorted(matched)
    print("每联赛参数（avg_goals / home_adv / probability_shrink / shock_sd）:")
    for league in leagues:
        if league not in matched:
            print(f"  ⏳ {league}: 无已匹配结果的预测记录")
            continue
        _tune_league(league, matched[league], args.dry_run)
    print("=" * 60)
    print("提示: 调参基于已积累样本，刚起步时样本不足属正常；每天预测并 `--record` 后，"
          "样本会逐渐积累（参考 references/daily_review.md）。")


# ================================================================
# trap — 诱盘判定复盘
# ================================================================

def _market_direction(handicap_line):
    """盘口 → 市场倾向方向（主队视角让球：主让为负，受让为正）。"""
    if handicap_line is None:
        return None
    margin = -handicap_line  # 市场预期的让球差
    if margin > 0.25:
        return "home"
    if margin < -0.25:
        return "away"
    return "draw"


def review_trap_decisions(league=None, date=None):
    """诱盘判定复盘：评估「忽略盘口走模型」是否有效。写入 data/reviews/trap_log.json。"""
    entries_all = ledger.load_ledger()["predictions"]
    entries = [e for e in entries_all
               if isinstance(e.get("prediction", {}).get("odds"), dict)
               and (not league or e.get("league") == league)
               and (not date or e.get("date") == date)]
    if not entries:
        return
    print()
    print("-" * 60)

    log_path = os.path.join(BASE, "data", "reviews", "trap_log.json")
    log = {"_meta": {}, "entries": []}
    if os.path.exists(log_path):
        try:
            with open(log_path, encoding="utf-8") as f:
                log = json.load(f)
        except Exception:
            log = {"_meta": {}, "entries": []}
    log.setdefault("entries", [])

    summary = {"n": 0, "normal": 0, "suspect": 0, "trap": 0,
               "model_hit": 0, "market_hit": 0,
               "trap_n": 0, "trap_model_hit": 0, "trap_market_hit": 0,
               "ignored_model_hit": 0, "ignored_n": 0}

    for e in entries:
        league = e.get("league", "?")
        results_data = league_data.load_results_data(league)
        pairs = ledger.match_results([e], results_data)
        if not pairs or pairs[0][1] is None:
            continue  # 待结果
        entry, result = pairs[0]
        actual = outcome_of(result)
        odds = entry["prediction"]["odds"]
        model_dir = entry["prediction"]["direction"]
        handicap = odds.get("handicap_line")
        market_dir = _market_direction(handicap)
        model_hit = model_dir == actual
        market_hit = market_dir is not None and market_dir == actual

        rec = {
            "date": entry.get("date"),
            "league": league,
            "home": entry.get("home"), "away": entry.get("away"),
            "handicap_line": handicap,
            "trap_score": odds.get("trap_score"),
            "trap_decision": odds.get("trap_decision"),
            "market_weight": odds.get("market_weight"),
            "model_direction": model_dir,
            "market_direction": market_dir,
            "actual": actual,
            "model_hit": model_hit,
            "market_hit": market_hit,
        }
        # 去重：同主客对 + 同日期覆盖
        log["entries"] = [x for x in log["entries"]
                          if not (x.get("home") == entry.get("home")
                                  and x.get("away") == entry.get("away")
                                  and x.get("date") == entry.get("date"))]
        log["entries"].append(rec)

        summary["n"] += 1
        summary[odds.get("trap_decision", "normal")] += 1
        summary["model_hit"] += int(model_hit)
        summary["market_hit"] += int(bool(market_hit))
        if odds.get("market_weight", 0.3) <= 0:
            summary["ignored_n"] += 1
            summary["ignored_model_hit"] += int(model_hit)
        if odds.get("trap_decision") == "trap":
            summary["trap_n"] += 1
            summary["trap_model_hit"] += int(model_hit)
            summary["trap_market_hit"] += int(bool(market_hit))

    from datetime import datetime
    log["_meta"]["updated_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    s = summary
    n = max(s["n"], 1)
    print(f"📊 诱盘判定复盘（含盘口分析的预测，{s['n']} 场已完赛）")
    print(f"  判定分布: 正常 {s['normal']} | 疑似 {s['suspect']} | 明显诱盘 {s['trap']}")
    print(f"  模型方向命中 {s['model_hit']}/{s['n']} | 若采信市场方向命中 {s['market_hit']}/{s['n']}")
    if s["trap_n"]:
        eff = "有效" if s["trap_model_hit"] >= s["trap_market_hit"] else "无效（市场更准）"
        print(f"  诱盘判定场次: 模型命中 {s['trap_model_hit']}/{s['trap_n']} vs 市场命中 "
              f"{s['trap_market_hit']}/{s['trap_n']} → 忽略盘口{eff}")
    if s["ignored_n"]:
        print(f"  忽略盘口(权重0)场次: 模型命中 {s['ignored_model_hit']}/{s['ignored_n']}")
    print(f"  明细: {os.path.relpath(log_path, BASE)}")


def main():
    p = argparse.ArgumentParser(description="每日复盘 + 自动调参")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("review", help="复盘某日预测")
    pr.add_argument("--date", default=None, help="比赛日期 YYYY-MM-DD（默认昨天）")
    pr.add_argument("--league", default=None, help="联赛过滤（可选）")
    pr.set_defaults(func=cmd_review)

    pt = sub.add_parser("tune", help="自动调参")
    pt.add_argument("--league", default=None, help="仅调某联赛（默认全部）")
    pt.add_argument("--dry-run", action="store_true", help="只建议、不写回文件")
    pt.set_defaults(func=cmd_tune)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
