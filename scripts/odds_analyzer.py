#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
亚盘分析 + 诱盘检测模块。

用途
----
单场预测时若提供实时亚盘数据（盘口 + 水位），对比「模型预期让球差」与「市场盘口」：

  - 正常盘口（normal）：模型与盘口互证 → 温和融合市场信号（market_weight 0.3）。
  - 疑似分歧（suspect）：部分采信市场（0.15）。
  - 明显诱盘（trap）：模型与盘口强烈背离 + 水位失衡等诱盘信号
    → 忽略市场（0.0），完全走原模型。

判据（trap_score，0~6，越高越像诱盘）
  - 分歧分 0~3：模型让球差 vs 盘口让球差（0.8 球阈值附近开始明显）。
  - 水位分 0~2：某侧水位过低（市场强烈引导）+ 两侧水位失衡 + 市场引导方向与模型相反。
  - 信心分 0~1：模型信心越足越敢判定诱盘（信心不足时宁可信市场）。

向后兼容：不传 odds_data 时整条链路完全不变；缺盘口数据时 analyze_odds 返回 None。

盘口文本解析（parse_asian_handicap）
  主队视角让球数：主队让球为负，受让为正。
  "平手"→0.0  "主让半球"→-0.5  "受让一球"→+1.0
  "主让一球/球半"→-1.25  "受让一球球半"→+1.25  "受让半球/一球"→+0.75
  纯数字 "-0.5" / "0.75" 直接返回。
"""

import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from league_data import load_league_config  # noqa: E402

_ODDS_CONFIG_PATH = os.path.join(BASE, "references", "odds_config.json")
_ODDS_CONFIG_CACHE = {}

_DECISION_CN = {"normal": "正常盘口", "suspect": "疑似分歧", "trap": "明显诱盘"}
_CONF_CN = {"high": "高采信", "medium": "部分采信", "ignore": "忽略(走模型)"}


def load_odds_config() -> dict:
    """读取盘口分析配置；文件缺失/损坏时回退默认值。"""
    if _ODDS_CONFIG_CACHE:
        return _ODDS_CONFIG_CACHE
    cfg = {
        "weights": {"normal": 0.3, "suspect": 0.15, "trap": 0.0},
        "divergence_thresholds": {"handicap_goals": 0.8, "goals_total": 1.2},
        "elo_threshold": 15,
        "water_coefficient": 0.90,
        "trap_score_thresholds": {"low": 2.0, "high": 4.0},
        "dynamic_shrink_multipliers": {"normal": 0.6, "suspect": 1.0, "trap": 1.2},
    }
    if os.path.exists(_ODDS_CONFIG_PATH):
        try:
            with open(_ODDS_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if isinstance(v, dict):
                    cfg.setdefault(k, {}).update(v)
                else:
                    cfg[k] = v
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ odds_config.json 读取失败，使用默认配置: {e}", file=sys.stderr)
    _ODDS_CONFIG_CACHE.update(cfg)
    return cfg


def _as_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ================================================================
# 亚盘文本解析
# ================================================================

_STEP = {"平半": 0.25, "半球": 0.5, "一球": 1.0, "球半": 1.5, "两球": 2.0, "两球半": 2.5}


def _cn_handicap_value(s: str):
    """解析纯中文盘口词 → 非负数值。支持单档与组合（一球/球半=1.25、一球半=1.5 等）。"""
    if not s:
        return None
    if s == "平手":
        return 0.0
    # 口语"X球半" = X + 0.5（如 一球半=1.5、两球半=2.5）；"球半" 前缀非整球，走下方单档
    m = re.fullmatch(r"(.+)半", s)
    if m:
        base = _cn_handicap_value(m.group(1))
        if base is not None and base >= 1.0:
            return base + 0.5
    if s in _STEP:
        return _STEP[s]
    if "/" in s:
        vals = [_cn_handicap_value(p) for p in s.split("/")]
        if None in vals:
            return None
        return sum(vals) / len(vals)
    # 无斜杠组合，如 "一球球半" "半球一球" "两球两球半"
    keys = sorted(_STEP, key=len, reverse=True)
    vals = []
    i = 0
    while i < len(s):
        for k in keys:
            if s.startswith(k, i):
                vals.append(_STEP[k])
                i += len(k)
                break
        else:
            return None
    if not vals:
        return None
    return sum(vals) / len(vals)


def parse_asian_handicap(s) -> float:
    """
    亚盘文本 → 主队视角让球数（主队让球为负，受让为正）。
    解析失败返回 None。
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace(" ", "")
    if not s:
        return None
    if re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        return float(s)
    sign = 1.0
    if s.startswith("主让"):
        sign = -1.0
        s = s[2:]
    elif s.startswith("受让"):
        sign = 1.0
        s = s[2:]
    elif s.startswith("受"):
        sign = 1.0
        s = s[1:]
    val = _cn_handicap_value(s)
    if val is None:
        return None
    return sign * val


def find_odds_match(odds_data: dict, a: str, b: str) -> dict:
    """在 odds_data['matches'] 中按 {home,away} 主客对查找本场盘口数据（双向匹配）。"""
    if not isinstance(odds_data, dict):
        return None
    for m in odds_data.get("matches", []):
        if not isinstance(m, dict):
            continue
        h = str(m.get("home", "")).upper()
        aw = str(m.get("away", "")).upper()
        if (h, aw) == (a, b) or (h, aw) == (b, a):
            return m
    return None


# ================================================================
# 诱盘检测
# ================================================================

def detect_trap(la: float, lb: float, line, water_home, water_away,
                model_confidence, cfg: dict) -> tuple:
    """
    返回 (trap_score, divergence)。
    divergence = 模型让球差 - 市场让球差（主队视角；>0 表示模型认为主队比盘口更强）。
    trap_score 0~6：分歧 + 水位失衡 + 模型信心，越高越像诱盘。
    """
    model_margin = la - lb
    handicap = line if line is not None else model_margin  # 无盘口时视为模型自洽
    divergence = model_margin - (-handicap)  # 市场让球差 = -handicap

    # 1) 分歧分 (0~3)
    th = cfg.get("divergence_thresholds", {}).get("handicap_goals", 0.8)
    ad = abs(divergence)
    if ad <= 0.30:
        div_score = 0.0
    elif ad >= 1.50:
        div_score = 3.0
    else:
        div_score = (ad - 0.30) / (1.50 - 0.30) * 3.0

    # 2) 水位分 (0~2)
    water_score = 0.0
    if water_home is not None and water_away is not None:
        lo, hi = min(water_home, water_away), max(water_home, water_away)
        if lo <= 0.85:
            water_score += 1.0  # 某侧水位过低 → 市场强烈引导该侧
        if hi - lo >= 0.08:
            water_score += 0.5  # 两侧水位失衡
        # 市场"推"的方向与模型相悖 → 诱盘特征（市场把筹码引向被高估一方）
        push_home = water_home < water_away - 0.03
        push_away = water_away < water_home - 0.03
        if push_home and divergence < -0.2:
            water_score += 0.5
        elif push_away and divergence > 0.2:
            water_score += 0.5

    # 3) 信心分 (0~1)：模型信心越足越敢判定诱盘
    conf = 1.0 if model_confidence is None else float(model_confidence)
    conf_score = max(0.0, min(1.0, (conf - 0.5) * 2.0))

    trap_score = min(6.0, div_score + water_score + conf_score)
    return trap_score, divergence


def _decision(trap_score: float, cfg: dict) -> str:
    th = cfg.get("trap_score_thresholds", {})
    if trap_score >= th.get("high", 4.0):
        return "trap"
    if trap_score >= th.get("low", 2.0):
        return "suspect"
    return "normal"


def get_market_weight(trap_score: float, cfg: dict, elo_gap=None) -> float:
    """诱盘评分 + Elo 差 → 市场可信权重（0.3 / 0.15 / 0.0）。"""
    if elo_gap is not None and elo_gap < cfg.get("elo_threshold", 15):
        return 0.0  # 模型自身无优势（近均衡），不让盘口制造假优势
    th = cfg.get("trap_score_thresholds", {})
    if trap_score >= th.get("high", 4.0):
        return cfg.get("weights", {}).get("trap", 0.0)
    if trap_score >= th.get("low", 2.0):
        return cfg.get("weights", {}).get("suspect", 0.15)
    return cfg.get("weights", {}).get("normal", 0.3)


def _market_confidence(weight: float, trap_decision: str) -> str:
    if weight <= 0:
        return "ignore"
    return {"normal": "high", "suspect": "medium", "trap": "low"}.get(trap_decision, "medium")


def analyze_odds(la: float, lb: float, odds_match: dict,
                 model_confidence=1.0, elo_gap=0, cfg: dict = None) -> dict:
    """
    对单场做盘口分析（调用方已算好原始 xG la/lb）。
    无盘口数据时返回 None。
    返回含 market_factor / trap_score / trap_decision / market_weight /
          market_confidence / original_xg / final_xg 等字段。
    """
    cfg = cfg or load_odds_config()
    if not isinstance(odds_match, dict):
        return None

    line = odds_match.get("handicap_line")
    if line is None:
        line = parse_asian_handicap(odds_match.get("handicap"))
    water_home = _as_float(odds_match.get("water_home"))
    water_away = _as_float(odds_match.get("water_away"))

    if line is None and water_home is None and water_away is None:
        return None  # 无有效盘口数据

    trap_score, divergence = detect_trap(la, lb, line, water_home, water_away,
                                         model_confidence, cfg)
    trap_decision = _decision(trap_score, cfg)
    weight = get_market_weight(trap_score, cfg, elo_gap)

    # 市场因子：仅在盘口存在且采信权重 > 0 时生效（对称施加：la += f, lb -= f）
    market_factor = 0.0
    if line is not None and weight > 0:
        market_margin = -line
        model_margin = la - lb
        total = (market_margin - model_margin) * weight * cfg.get("water_coefficient", 0.9)
        market_factor = total / 2.0
    final_la = max(0.20, la + market_factor)
    final_lb = max(0.20, lb - market_factor)

    conf = _market_confidence(weight, trap_decision)
    return {
        "handicap_line": line,
        "handicap_cn": f"主让{abs(line):g}" if line is not None and line < 0
                       else (f"受让{abs(line):g}" if line is not None and line > 0
                             else ("平手" if line == 0 else "-")),
        "model_margin": round(la - lb, 3),
        "market_margin": round(-line, 3) if line is not None else None,
        "divergence": round(divergence, 3),
        "trap_score": round(trap_score, 2),
        "trap_decision": trap_decision,
        "trap_decision_cn": _DECISION_CN.get(trap_decision, trap_decision),
        "market_weight": weight,
        "market_confidence": conf,
        "market_confidence_cn": _CONF_CN.get(conf, conf),
        "market_factor": round(market_factor, 3),
        "original_xg": {"la": round(la, 3), "lb": round(lb, 3)},
        "final_xg": {"la": round(final_la, 3), "lb": round(final_lb, 3)},
    }


# ================================================================
# 动态概率收缩（诱盘判定 → 收缩乘数）
# ================================================================

def get_dynamic_shrink(league: str, trap_decision: str,
                       cfg: dict = None, elo_gap=None) -> float:
    """
    诱盘判定 → 动态概率收缩。
      normal  盘口互证 → 更自信 → 收缩减弱（×0.6）
      suspect 部分采信 → 分歧中性 → 收缩不变（×1.0）
      trap    忽略盘口 → 更谨慎 → 收缩加强（×1.2）
    Elo 差低于阈值（近均衡、模型无优势）时返回原收缩（×1.0）。
    """
    cfg = cfg or load_odds_config()
    base = load_league_config(league).get("probability_shrink", 0.26)
    if elo_gap is not None and elo_gap < cfg.get("elo_threshold", 15):
        return base
    mult = cfg.get("dynamic_shrink_multipliers", {}).get(trap_decision, 1.0)
    return max(0.02, min(0.40, base * mult))


# ================================================================
# 自测
# ================================================================

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="odds_analyzer 自测")
    ap.add_argument("--handicap", default="主让半球")
    args = ap.parse_args()
    print(f"parse_asian_handicap({args.handicap!r}) = {parse_asian_handicap(args.handicap)}")
    for s in ["平手", "主让半球", "受让一球", "主让一球/球半", "受让一球球半",
              "受让半球/一球", "主让球半", "-0.5", "0.75"]:
        print(f"  {s!r:>10} → {parse_asian_handicap(s)}")

    cfg = load_odds_config()
    print("\n场景1 正常盘口 (模型让球差 0.7 vs 主让半球 -0.5, 水位均衡):")
    r1 = analyze_odds(1.80, 1.10, {"handicap_line": -0.5, "water_home": 0.95, "water_away": 0.93},
                      model_confidence=1.0, elo_gap=120, cfg=cfg)
    print(f"  trap_score={r1['trap_score']} 判定={r1['trap_decision']} 权重={r1['market_weight']} "
          f"factor={r1['market_factor']} 原xG={r1['original_xg']} 终xG={r1['final_xg']}")
    assert r1["trap_score"] <= 2.0 and r1["market_weight"] == 0.3

    print("\n场景2 明显诱盘 (模型近均衡 vs 主让一球 -1.0, 主队水位极低):")
    r2 = analyze_odds(1.35, 1.55, {"handicap_line": -1.0, "water_home": 0.78, "water_away": 1.02},
                      model_confidence=1.0, elo_gap=90, cfg=cfg)
    print(f"  trap_score={r2['trap_score']} 判定={r2['trap_decision']} 权重={r2['market_weight']} "
          f"factor={r2['market_factor']} 原xG={r2['original_xg']} 终xG={r2['final_xg']}")
    assert r2["trap_score"] >= 4.0 and r2["market_weight"] == 0.0 and r2["market_factor"] == 0.0

    print("\n✅ 两个测试场景通过")
