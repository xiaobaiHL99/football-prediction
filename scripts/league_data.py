#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联赛数据加载器 — 统一加载球队、配置、情报、积分榜
"""

import json
import math
import os
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_league_config(league: str) -> dict:
    """加载联赛配置"""
    path = os.path.join(BASE, "references", "league_config.json")
    with open(path, encoding="utf-8") as f:
        configs = json.load(f)
    if league not in configs:
        raise SystemExit(f"❌ 未找到联赛配置: {league}")
    return configs[league]

def load_league_teams(league: str) -> dict:
    """加载联赛球队数据"""
    path = os.path.join(BASE, "references", league, "teams.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["teams"]

def load_league_context(league: str) -> dict:
    """加载联赛特殊因子"""
    path = os.path.join(BASE, "references", league, "context.json")
    if not os.path.exists(path):
        return {"special_factors": {}, "europe_teams": [], "turf_map": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_current_table(league: str) -> dict:
    """加载当前积分榜"""
    path = os.path.join(BASE, "data", "live", league, "table.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_fixtures(league: str) -> list:
    """加载剩余赛程"""
    path = os.path.join(BASE, "data", "live", league, "fixtures.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_intelligence(league: str) -> dict:
    """加载赛前情报"""
    path = os.path.join(BASE, "data", "live", league, "intelligence.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_intelligence_for_match(league: str, home: str, away: str) -> dict:
    """加载特定比赛的情报"""
    # 先尝试加载特定比赛的情报文件
    specific_path = os.path.join(BASE, "data", "live", league, f"intelligence_{home.lower()}_{away.lower()}.json")
    if os.path.exists(specific_path):
        with open(specific_path, encoding="utf-8") as f:
            return json.load(f)
    
    # 再尝试加载主队_客队格式
    specific_path2 = os.path.join(BASE, "data", "live", league, f"intelligence_{home}_{away}.json")
    if os.path.exists(specific_path2):
        with open(specific_path2, encoding="utf-8") as f:
            return json.load(f)
    
    # 最后加载默认情报文件
    return load_intelligence(league)

def load_locked_results(league: str) -> dict:
    """加载已锁定结果（组件锁表）"""
    path = os.path.join(BASE, "data", "live", league, "results.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    idx = {}
    for m in data.get("matches", []):
        idx[frozenset((m["home"], m["away"]))] = (m["home"], m["hg"], m["ag"])
    return idx


def load_results_data(league: str) -> dict:
    """加载原始 results.json 数据（用于交手次数统计等）"""
    path = os.path.join(BASE, "data", "live", league, "results.json")
    if not os.path.exists(path):
        return {"matches": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def _load_model_override_data() -> dict:
    """Load the shared overrides document without forcing callers to parse JSON."""
    path = os.path.join(BASE, "references", "model_overrides.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_model_overrides() -> dict:
    """Load numeric constant overrides used by this module."""
    return _load_model_override_data().get("overrides", {})


def load_global_rules() -> dict:
    """Load structured, optional global calibration rules."""
    return _load_model_override_data().get("global_rules", {})

def resolve_team(teams: dict, key: str) -> str:
    """解析球队名称 → 代码"""
    k = str(key).strip()
    kl = k.lower()
    for code, t in teams.items():
        if code.lower() == kl or t["name"].lower() == kl or t.get("name_cn", "") == k:
            return code
    for code, t in teams.items():
        if kl and kl in t["name"].lower() or (k and k in t.get("name_cn", "")):
            return code
    raise SystemExit(f"❌ 未找到球队: {key}")

def cn(teams: dict, code: str) -> str:
    """获取中文名"""
    return teams[code].get("name_cn", code)


# 球员缺席 → Elo 修正的冲击系数
# 采用递减原则：多人缺席时边际影响递减
# v2: 系数下调，避免一两个伤员拉平整队实力差距
# v3: 添加主帅停赛角色（2026-09-03复盘改进）
ABSENT_IMPACT = {
    "star": 18.0,
    "goalkeeper_star": 15.0,
    "starter": 7.0,
    "rotation": 2.0,
    "manager": 12.0,  # 主帅停赛：临场指挥受限，战术调整能力下降
}
ABSENT_DIMINISHING = 0.85  # 每多一种类别乘一次
ABSENT_MAX_TOTAL = 45.0   # 单队伤病总扣分上限（避免极端情况，含主帅停赛）


def compute_elo_delta(absent_players: list, full_squad: bool = True) -> float:
    """
    将结构化球员缺席数据转为 Elo 修正值（负值 = 实力下降）。

    Parameters
    ----------
    absent_players : list of dict
        每项描述一类缺席球员：
        {"role": "star"|"starter"|"rotation"|"goalkeeper_star"|"manager", "count": int}
    full_squad : bool
        若 True 且 absent_players 为空 → 0.0（无调整）。
        若 False 且 absent_players 为空 → +5（对手有缺阵，本队受益）。

    Returns
    -------
    float
        elo_delta，适用于 intelligence.json 的 teams.<code>.elo_delta

    Example
    -------
    >>> compute_elo_delta([
    ...     {"role": "star", "count": 1},
    ...     {"role": "starter", "count": 2}
    ... ])
    -38.25
    >>> compute_elo_delta([{"role": "manager", "count": 1}])
    -12.0
    """
    if not absent_players:
        return 5.0 if not full_squad else 0.0

    categories = [p for p in absent_players if p.get("role") in ABSENT_IMPACT]
    if not categories:
        return 0.0

    total = sum(ABSENT_IMPACT[p["role"]] * max(p.get("count", 1), 1) for p in categories)
    n_cats = len(set(p["role"] for p in categories))
    if n_cats > 1:
        total *= ABSENT_DIMINISHING ** (n_cats - 1)

    return -min(total, ABSENT_MAX_TOTAL)


# ================================================================
# 比赛情境分类器 — 根据两队特征动态调整模型参数
# ================================================================

# 情境判定阈值
WEAK_MATCH_THRESHOLD = 1550.0    # 弱队对话：均Elo低于此值
HUGE_ELO_GAP = 150.0             # 实力悬殊：Elo差超过此值
HUGE_GAP_SHOCK_FLOOR = 1.0       # 实力悬殊不再压缩方差(<1 会让大热更自信)，地板提回中性
INJURY_CRISIS_THRESHOLD = -15.0  # 伤病危机：elo_delta低于此值
RELEGATION_ZONE = 4              # 倒数N名为保级区
DEBATED_ZONE = 6                 # 德比/保级判定用后半段排名

# ---- 近期状态因子 ----
FORM_XG_MAX = 0.10               # 5连胜 = +0.10 xG, 5连败 = -0.10 xG
FORM_DECAY_FACTOR = 0.85         # 位置衰减（最新比赛权重最高）

# ---- 交手熟悉度因子 ----
MEETING_FAMILIARITY_THRESHOLD = 3   # ≥3次交手触发熟悉度修正
FAMILIARITY_UNDERDOG_BOOST = 0.04   # 弱队因熟悉度获xG加成
FAMILIARITY_FAVORITE_PENALTY = -0.03  # 强队因套路被摸透受xG罚

# ---- 往绩魔咒因子（2026-09-04复盘改进）----
# 当往绩出现连续3次以上同一方向时，自动调整概率
H2H_CURSE_THRESHOLD = 3            # ≥3次连续同一方向触发魔咒修正
H2H_CURSE_BOOST_HOME = 0.08        # 主队往绩占优时+8%胜率
H2H_CURSE_BOOST_AWAY = 0.06        # 客队往绩占优时+6%胜率（客场衰减）
H2H_CURSE_MAX_STREAK = 5           # 最大连续次数（超过按5次算）

# ---- 进球荒因子（2026-09-04复盘改进）----
# 连续3场以上没进球的球队，胜率下调
GOAL_DROUGHT_THRESHOLD = 3         # ≥3场连续没进球触发修正
GOAL_DROUGHT_PENALTY = -0.12       # 进球荒胜率下调12%

# ---- 平局基础概率调整（2026-09-04复盘改进）----
# 实力接近的比赛平局概率上调
CLOSE_MATCH_DRAW_BOOST = 0.04      # 实力接近时平局+4%
CLOSE_MATCH_ELO_THRESHOLD = 80     # Elo差<80视为实力接近

# ---- 冷门预警因子（2026-09-04复盘改进）----
# 当往绩+状态+伤停三重因素叠加时，自动触发冷门预警
UPSET_WARNING_THRESHOLD = 3        # ≥3个因素叠加触发冷门预警
UPSET_WARNING_PENALTY = -0.08      # 冷门预警时热门胜率-8%

# ---- 9月6日复盘新增：强队低迷+防守韧性场景修正 ----

# 场景1：强队2轮以上不胜/0球 → 胜率下调8-12%
# form连续2+场无胜或进球荒触发
FORM_DROUGHT_THRESHOLD = 2         # 连续≥2场无胜或0球触发
FORM_DROUGHT_PENALTY = -0.10       # 强队状态低迷胜率-10%

# 场景2：升班马+主场+防守型 → 平局概率上调4-6%
# 需要在intelligence中显式标注 promoted_home_defensive
PROMOTED_HOME_DRAW_BOOST = 0.05    # 升班马主场防守型平局+5%

# 场景3：强队客场+对手魔鬼主场 → 客胜概率下调6-8%
# 需要在intelligence中显式标注 fortress_venue
FORTRESS_AWAY_PENALTY = -0.07      # 对手魔鬼主场客胜-7%

# 场景4：强队近期被逆转 → 状态信心修正-10%
# form最近一场为负且前一场为胜（逆转模式）触发
REVERSAL_CONFIDENCE_PENALTY = -0.10  # 被逆转信心-10%


# ================================================================
# 近期状态 + 战术因子工具函数
# ================================================================

def _warn_bad_form(form_str: str) -> None:
    """form 字符串非空但无法解析时提示，避免状态修正被静默中性化。"""
    try:
        print(f"  ⚠️ 近期状态 form 无法解析: \"{form_str}\" "
              f"（应为 \"胜-胜-平-负-胜\" 或 \"W-W-L-W-D\"，最新一场在最后；已按中性处理，请检查格式）",
              file=sys.stderr)
    except Exception:
        pass


def parse_form(form_str: str):
    """
    解析近期状态字符串，返回 (状态分数, 有效结果数)。

    支持格式:
      "胜-胜-负-胜-平" (中文)
      "W-W-L-W-D"      (英文)
      "胜" / "负" / "平" / "W" / "L" / "D" (单场简写，新赛季首轮合法输入)
      "胜胜胜" / "WWL"  (无分隔符连写，按逐字拆分)
    最新比赛权重最高，按 FORM_DECAY_FACTOR 指数衰减。
    1.0 = 全胜, 0.0 = 全负, 0.5 = 全平或居中。
    返回 (score, n)；n = 有效结果数（无法解析时 n=0）。
    """
    if not form_str or not isinstance(form_str, str):
        return 0.5, 0

    result_map = {
        "胜": 1.0, "W": 1.0, "w": 1.0,
        "平": 0.5, "D": 0.5, "d": 0.5,
        "负": 0.0, "L": 0.0, "l": 0.0,
    }

    compact = form_str.replace(" ", "").replace("-", "").replace("_", "")

    # 单场简写（新赛季首轮只有一场比赛）：合法输入，直接返回，不警告
    if compact in result_map:
        return result_map[compact], 1

    # 无分隔符连写（如 "胜胜胜平负" / "WWLDW"）：每个字符都是合法状态 token
    if compact and all(ch in result_map for ch in compact) and len(compact) >= 2:
        parts = list(compact)
        scores = [result_map[p] for p in parts]
    else:
        parts = [s.strip() for s in form_str.replace(" ", "").split("-")]
        scores = [result_map[p] for p in parts if p in result_map]

        # 非空字符串却无法解析成 ≥2 个状态 token → 警告（防"状态修正静默失效"）
        if not parts or len(parts) < 2 or not scores or len(scores) != len(parts):
            _warn_bad_form(form_str)
            return 0.5, 0

    # 最新比赛（最后一项）权重最高，倒序加权
    total_weight = 0.0
    weighted_sum = 0.0
    for i, s in enumerate(reversed(scores)):
        w = FORM_DECAY_FACTOR ** i
        weighted_sum += s * w
        total_weight += w

    return weighted_sum / total_weight, len(scores)


# 少样本状态信号折减（2026-08-31 复盘改进）：
# 新赛季早期 form 只有 1-2 场时信号方差大，按有效结果数折减 xG 调整幅度
FORM_SHORT_SAMPLE_WEIGHT = {1: 0.45, 2: 0.70}


def form_to_xg_adjust(form_score: float, n_results: int = None) -> float:
    """
    将 form_score [0,1] 映射到 [-FORM_XG_MAX, +FORM_XG_MAX] xG 调整值。
    0.5 → 0.0 (无调整), 1.0 → +0.10, 0.0 → -0.10
    n_results: 有效状态结果数；1-2 场时按 FORM_SHORT_SAMPLE_WEIGHT 折减
    （None = 不折减，兼容旧调用方式）。
    """
    adj = (form_score - 0.5) * 2.0 * FORM_XG_MAX
    if n_results is not None and n_results in FORM_SHORT_SAMPLE_WEIGHT:
        adj *= FORM_SHORT_SAMPLE_WEIGHT[n_results]
    return adj


def count_meetings(results_data: dict, a: str, b: str) -> int:
    """
    统计两队本赛季在 results.json 中已交锋次数。
    results_data: {"matches": [{"home": "...", "away": "...", ...}, ...]}
    返回 int。
    """
    matches = results_data.get("matches", []) if results_data else []
    count = 0
    for m in matches:
        h = str(m.get("home", "")).upper()
        wa = str(m.get("away", "")).upper()
        if {h, wa} == {a.upper(), b.upper()}:
            count += 1
    return count


# ================================================================
# 战术对位因子（Tactical Matchup Factors）
# ================================================================

TACTICAL_STYLES = {
    "high_press": "高位逼抢 — 前场高强度压迫，迫使对手失误",
    "possession": "传控 — 控球主导，短传渗透，耐心组织",
    "counter_attack": "防守反击 — 收缩防守，快速转换进攻",
    "direct": "长传冲吊 — 直接长传找前锋，第二点争抢",
    "defensive_deep": "深度防守/大巴 — 全员退守，压缩空间",
    "physical": "身体对抗型 — 强调对抗、定位球、空中球",
    "hybrid": "混合型 — 无明显单一风格，根据对手调整",
}

# 战术克制矩阵：(style_a, style_b) → (delta_a, delta_b) xG
# 正值 = 该风格获 xG 加成，负值 = 受压制
TACTICAL_MATRIX = {
    ("high_press", "possession"): (0.20, -0.20),         # 高位逼抢克制传控出球
    ("high_press", "defensive_deep"): (-0.15, 0.15),     # 高位逼抢难破大巴
    ("possession", "defensive_deep"): (0.15, -0.15),     # 传控可缓慢渗透大巴
    ("possession", "counter_attack"): (-0.20, 0.20),     # 传控怕反击
    ("counter_attack", "high_press"): (0.25, -0.25),     # 反击克制高位逼抢
    ("counter_attack", "possession"): (0.20, -0.20),     # 反击克制传控
    ("counter_attack", "defensive_deep"): (-0.10, 0.10), # 反击怕大巴
    ("direct", "possession"): (0.15, -0.15),             # 长传绕过传控中场
    ("direct", "defensive_deep"): (-0.10, 0.10),         # 长传难破密集防守
    ("direct", "high_press"): (0.10, -0.10),             # 长传破解高位逼抢
    ("physical", "possession"): (0.15, -0.15),           # 身体对抗破坏传控节奏
    ("physical", "counter_attack"): (-0.08, 0.08),        # 身体队怕快速反击
    ("physical", "high_press"): (-0.10, 0.10),            # 身体队出球慢被压迫
    ("defensive_deep", "high_press"): (0.10, -0.10),     # 大巴应对高位逼抢
}

# 开放性修正
OPENNESS_BOOST = 0.20         # 对攻战双方 xG 加成
OPENNESS_PENALTY = -0.15      # 保守战双方 xG 惩罚

# 无效控球惩罚
INEFFECTIVE_POSSESSION_PENALTY = -0.25      # 传控队面对大巴无法渗透
INEFFECTIVE_POSSESSION_HYBRID_FACTOR = 0.7  # 混合型折减系数

# 身体对抗差异
PHYSICAL_MISMATCH_STEP = 0.12  # 每级差异 xG

# 德比加成
DERBY_BOOST_HOME = 0.18
DERBY_BOOST_AWAY = 0.08

# 战意因素（Fighting Spirit）——战意是预测中最重要的人为因子，幅度高于普通战术/状态因子
# 等级 → 数值：desperate=+2, high=+1, normal=0, low=-1, dead_rubber=-2
SPIRIT_LEVELS = {
    "desperate": 2,      # 背水一战（保级生死、总比分落后、末轮定生死）
    "high": 1,           # 战意高涨（争冠、欧战资格、德比、复仇、新帅首秀）
    "normal": 0,         # 正常（默认）
    "low": -1,           # 战意偏低（已达标、保级无忧、双线保留）
    "dead_rubber": -2,   # 无欲无求/放弃（赛季末无意义、全力备战更关键赛事）
}
SPIRIT_XG_STEP = 0.15    # 每级战意差异 ±0.15 xG（战意权重最高，接近软截断上限）

# 软截断参数
TACTICAL_CLAMP_SOFT = 0.25
TACTICAL_CLAMP_HARD = 0.35


def compute_fighting_spirit_delta(spirit: dict) -> tuple:
    """
    根据 per-team 战意等级计算 xG 修正值 (delta_a, delta_b)。

    spirit 字典结构（从 intelligence.json matches[].tactical.fighting_spirit 读取）：
    {
        "a": {"level": "high", "note": "争冠冲刺，主场必须取胜"},
        "b": {"level": "dead_rubber", "note": "中游无欲无求"}
    }
    兼容简写：{"a": "high", "b": "normal"}

    level 枚举与修正（SPIRIT_XG_STEP = 0.15，战意权重最高）：
      desperate(+0.30) / high(+0.15) / normal(0) / low(-0.15) / dead_rubber(-0.30)
    """
    if not spirit or not isinstance(spirit, dict):
        return (0.0, 0.0)

    deltas = {"a": 0.0, "b": 0.0}
    for side in ("a", "b"):
        entry = spirit.get(side)
        if isinstance(entry, dict):
            lvl = entry.get("level", "normal")
        elif isinstance(entry, str):
            lvl = entry
        else:
            lvl = "normal"
        deltas[side] = SPIRIT_LEVELS.get(lvl, 0) * SPIRIT_XG_STEP

    return (deltas["a"], deltas["b"])


def compute_tactical_delta(tactical: dict) -> tuple:
    """
    根据战术对位信息计算 xG 修正值 (delta_a, delta_b)。

    tactical 字典结构（从 intelligence.json matches[].tactical 读取）：
    {
        "style_a": "high_press" | "possession" | ...,
        "style_b": "possession" | ...,
        "matchup": "克制关系描述（可选）",
        "expected_pattern": "open" | "cautious" | "balanced",
        "ineffective_possession": false | {"team": "A", "possession_pct": 75},
        "physical_mismatch": -2~2,
        "derby_boost": false,
        "fighting_spirit": {"a": {"level": "high"}, "b": {"level": "normal"}}
    }

    Returns (delta_a, delta_b) — 主客队 xG 修正值。
    总修正限制在 ±0.35 以内（软截断：超过 ±0.25 部分对数衰减）。
    """
    if not tactical or not isinstance(tactical, dict):
        return (0.0, 0.0)

    style_a = tactical.get("style_a", "hybrid")
    style_b = tactical.get("style_b", "hybrid")
    expected_pattern = tactical.get("expected_pattern", "balanced")
    ineffective = tactical.get("ineffective_possession", False)
    phys_mismatch = tactical.get("physical_mismatch", 0)
    derby = tactical.get("derby_boost", False)

    delta_a = 0.0
    delta_b = 0.0

    # 1) 战术克制矩阵
    key = (style_a, style_b)
    key_rev = (style_b, style_a)
    if key in TACTICAL_MATRIX:
        da, db = TACTICAL_MATRIX[key]
        delta_a += da
        delta_b += db
    elif key_rev in TACTICAL_MATRIX:
        db, da = TACTICAL_MATRIX[key_rev]
        delta_a += da
        delta_b += db

    # 混合型风格效果折半
    if style_a == "hybrid":
        delta_a *= 0.5
    if style_b == "hybrid":
        delta_b *= 0.5

    # 2) 对攻/保守预期
    if expected_pattern == "open":
        delta_a += OPENNESS_BOOST
        delta_b += OPENNESS_BOOST
    elif expected_pattern == "cautious":
        delta_a += OPENNESS_PENALTY
        delta_b += OPENNESS_PENALTY

    # 3) 无效控球惩罚
    if isinstance(ineffective, dict):
        ineffective_team = ineffective.get("team", "")
        if ineffective_team == "A":
            factor = INEFFECTIVE_POSSESSION_HYBRID_FACTOR if style_a == "hybrid" else 1.0
            delta_a += INEFFECTIVE_POSSESSION_PENALTY * factor
        elif ineffective_team == "B":
            factor = INEFFECTIVE_POSSESSION_HYBRID_FACTOR if style_b == "hybrid" else 1.0
            delta_b += INEFFECTIVE_POSSESSION_PENALTY * factor
        else:
            # 自动判断
            if style_a in ("possession", "hybrid") and style_b in ("defensive_deep", "counter_attack"):
                factor = INEFFECTIVE_POSSESSION_HYBRID_FACTOR if style_a == "hybrid" else 1.0
                delta_a += INEFFECTIVE_POSSESSION_PENALTY * factor
            if style_b in ("possession", "hybrid") and style_a in ("defensive_deep", "counter_attack"):
                factor = INEFFECTIVE_POSSESSION_HYBRID_FACTOR if style_b == "hybrid" else 1.0
                delta_b += INEFFECTIVE_POSSESSION_PENALTY * factor
    elif ineffective is True:
        # 布尔值：自动判断
        if style_a in ("possession", "hybrid") and style_b in ("defensive_deep", "counter_attack"):
            factor = INEFFECTIVE_POSSESSION_HYBRID_FACTOR if style_a == "hybrid" else 1.0
            delta_a += INEFFECTIVE_POSSESSION_PENALTY * factor
        if style_b in ("possession", "hybrid") and style_a in ("defensive_deep", "counter_attack"):
            factor = INEFFECTIVE_POSSESSION_HYBRID_FACTOR if style_b == "hybrid" else 1.0
            delta_b += INEFFECTIVE_POSSESSION_PENALTY * factor

    # 4) 身体对抗差异
    if phys_mismatch > 0:
        delta_a += min(phys_mismatch, 2) * PHYSICAL_MISMATCH_STEP
        delta_b -= min(phys_mismatch, 2) * PHYSICAL_MISMATCH_STEP * 0.5
    elif phys_mismatch < 0:
        delta_b += min(abs(phys_mismatch), 2) * PHYSICAL_MISMATCH_STEP
        delta_a -= min(abs(phys_mismatch), 2) * PHYSICAL_MISMATCH_STEP * 0.5

    # 5) 德比/特殊战意加成
    if derby:
        delta_a += DERBY_BOOST_HOME
        delta_b += DERBY_BOOST_AWAY

    # 6) 战意因素（per-team 显式标注，权重最高）
    spirit = tactical.get("fighting_spirit", {})
    spirit_a, spirit_b = compute_fighting_spirit_delta(spirit)
    delta_a += spirit_a
    delta_b += spirit_b

    # 软截断
    def soft_clamp(val):
        s, h = TACTICAL_CLAMP_SOFT, TACTICAL_CLAMP_HARD
        if val > s:
            return s + math.log(1 + val - s) * (h - s) / max(math.log(1 + h - s), 0.001)
        elif val < -s:
            return -s - math.log(1 - val - s) * (h - s) / max(math.log(1 + h - s), 0.001)
        return val

    return (soft_clamp(delta_a), soft_clamp(delta_b))


def classify_match(teams: dict, a: str, b: str, elo_delta_a: float = 0.0,
                   elo_delta_b: float = 0.0, table: dict = None,
                   league_context: dict = None, intel: dict = None,
                   league: str = None) -> dict:
    """
    对比赛进行分类，返回动态调整因子。

    Parameters
    ----------
    teams : dict — 球队数据（含elo等）
    a, b : str — 主客队代码
    elo_delta_a/b : float — 当前Elo修正
    table : dict, optional — 当前积分榜（用于保级/排名判定）
    league_context : dict, optional — 联赛上下文（欧战球队等）

    Returns
    -------
    dict — 包含以下调整因子:
        xG_adjust_a / b : 额外xG调整
        shock_multiplier : 方差乘数 (>1 = 更随机)
        home_adv_multiplier : 主场优势乘数
        labels : [str] — 分类标签（用于报告展示）
    """
    elo_a = teams[a].get("elo", 1500)
    elo_b = teams[b].get("elo", 1500)
    avg_elo = (elo_a + elo_b) / 2
    elo_gap = abs(elo_a - elo_b)

    result = {
        "xG_adjust_a": 0.0,
        "xG_adjust_b": 0.0,
        "shock_multiplier": 1.0,
        "home_adv_multiplier": 1.0,
        "labels": [],
    }

    # 1) 弱弱对话：均Elo低于阈值 → 加大随机性 + 略升xG
    # 阈值可被联赛覆盖（context.json 的 weak_elo_threshold），
    # 解决英冠等整体Elo偏低联赛被整体误判为"弱弱对话"的问题
    weak_thr = WEAK_MATCH_THRESHOLD
    if league_context and isinstance(league_context, dict):
        weak_thr = league_context.get("weak_elo_threshold", WEAK_MATCH_THRESHOLD)
    if avg_elo < weak_thr:
        boost = 1.0 + (weak_thr - avg_elo) / weak_thr
        result["shock_multiplier"] = min(boost, 1.4)
        # 弱队比赛进球更随机，xG上浮
        weak_xg_boost = min(0.12, (weak_thr - avg_elo) / weak_thr * 0.3)
        result["xG_adjust_a"] += weak_xg_boost
        result["xG_adjust_b"] += weak_xg_boost
        result["labels"].append("弱弱对话(方差放大)")

    # 2) 伤病危机：一方核心缺阵 → 加强防守韧性补偿
    # 受伤一方会踢得更保守，降低对手xG
    for code, delta in [(a, elo_delta_a), (b, elo_delta_b)]:
        if delta < INJURY_CRISIS_THRESHOLD:
            # 受伤越重越保守，压低对手xG
            opponent_adj = abs(delta) / 200.0  # 每-10 Elo约降低对手0.05 xG
            opponent_adj = min(opponent_adj, 0.15)  # 上限0.15
            if code == a:
                result["xG_adjust_b"] -= opponent_adj
            else:
                result["xG_adjust_a"] -= opponent_adj
            result["labels"].append(f"{code}伤病保守(-{abs(delta):.0f}Elo)")

    # 3) 实力悬殊：Elo差太大 → 压缩冷门空间
    if elo_gap > HUGE_ELO_GAP:
        # 小幅降低弱队方差
        gap_factor = min(1.0, (elo_gap - HUGE_ELO_GAP) / HUGE_ELO_GAP * 0.15)
        result["shock_multiplier"] = max(result["shock_multiplier"] - gap_factor, HUGE_GAP_SHOCK_FLOOR)
        result["labels"].append("实力悬殊(方差中性)")

    # 4) 保级关键战（需要积分榜数据）
    if table:
        n_teams = len(table)
        # 获取两队排名
        sorted_teams = sorted(table.keys(), key=lambda c: table[c].get("points", 0), reverse=True)
        rank_a = sorted_teams.index(a) + 1 if a in sorted_teams else 99
        rank_b = sorted_teams.index(b) + 1 if b in sorted_teams else 99
        # 两队都在保级区附近（倒数DEBATED_ZONE名）
        if rank_a > n_teams - RELEGATION_ZONE and rank_b > n_teams - RELEGATION_ZONE:
            # 保级战主队战意加成
            result["home_adv_multiplier"] += 0.15
            result["xG_adjust_a"] += 0.08
            result["labels"].append("保级关键战(主队战意+)")
        # 其中一队在争冠区（前三）
        elif rank_a <= 3:
            result["xG_adjust_a"] += 0.05
            result["labels"].append("争冠队主场(+)")
        elif rank_b <= 3:
            result["xG_adjust_b"] += 0.05
            result["labels"].append("争冠队客场(+)")

        # 6) 副班长殊死战（通用）：主/客队倒数后2名 vs 非同样处境对手
        # 垫底球队拼死一搏时不可用常规Elo衡量
        bottom_n = max(2, round(n_teams * 0.15))  # 至少后2名，或后15%
        # 6a) 主队副班长殊死战：主队倒数 vs 客队不在倒数区
        if rank_a > n_teams - bottom_n and rank_b <= n_teams - bottom_n:
            # 大幅压低对手xG（铁桶阵+拼抢强度翻倍+球迷第12人）
            result["xG_adjust_b"] -= 0.40
            # 主队进攻大幅加成（冒险压上+定位球+尊严之战）
            result["xG_adjust_a"] += 0.32
            # 方差大幅升高（拼死局不可预测）
            result["shock_multiplier"] = max(result["shock_multiplier"] * 1.40, result["shock_multiplier"] + 0.20)
            result["labels"].append("副班长殊死战(主队拼死一搏)")
        # 6b) 客队副班长殊死战：客队倒数 vs 主队不在倒数区
        # 主队越强越能破解大巴，所以死守效果递减
        elif rank_b > n_teams - bottom_n and rank_a <= n_teams - bottom_n:
            is_top_home = rank_a <= 3  # 强队更擅破大巴
            home_xg_penalty = -0.25 if is_top_home else -0.45
            away_xg_boost = 0.10 if is_top_home else 0.20
            shock_boost = 1.20 if is_top_home else 1.40
            result["xG_adjust_a"] += home_xg_penalty
            result["xG_adjust_b"] += away_xg_boost
            result["shock_multiplier"] = max(result["shock_multiplier"] * shock_boost, result["shock_multiplier"] + (0.10 if is_top_home else 0.20))
            result["labels"].append(f"副班长殊死战(客队死守{'被破' if is_top_home else '反击'})")

    # 5) 欧战双线消耗（需要league_context）
    # 注意：当比赛本身就是欧战(champions_league/europa_league)时，跳过此逻辑
    # 因为"欧战分心"是指国内联赛中因欧战任务分心，而非欧战本身
    EURO_COMPETITIONS = {"champions_league", "europa_league"}
    is_euro_match = league in EURO_COMPETITIONS if league else False
    if league_context and not is_euro_match:
        euro_teams = set(league_context.get("europe_teams", []))
        a_euro = a in euro_teams
        b_euro = b in euro_teams
        if a_euro:
            result["xG_adjust_a"] -= 0.04  # 额外体能惩罚
            result["labels"].append(f"{a}欧战双线(-)")
        if b_euro:
            result["xG_adjust_b"] -= 0.04
            result["labels"].append(f"{b}欧战双线(-)")
        # 双方都有欧战分心 → 平局概率上调5-8%（2026-09-03复盘改进）
        if a_euro and b_euro:
            result["draw_boost"] = 0.06  # 6%平局概率上调
            result["labels"].append("双方欧战分心(平局+6%)")

    # ---- 2026-09-04复盘新增因子 ----

    # 7) 往绩魔咒因子：当往绩出现连续3次以上同一方向时
    # 传递方式：intelligence.json 的 matches[].h2h_curse 字段
    # 或在 teams 中标注 h2h_advantage
    h2h_curse = intel.get("h2h_curse", {}) if intel else {}
    curse_streak = h2h_curse.get("streak", 0)
    curse_favor = h2h_curse.get("favor", "")  # "home" or "away"
    if curse_streak >= H2H_CURSE_THRESHOLD:
        capped_streak = min(curse_streak, H2H_CURSE_MAX_STREAK)
        curse_boost = 0.02 * (capped_streak - H2H_CURSE_THRESHOLD + 1)  # 递增
        if curse_favor == "home":
            result["xG_adjust_a"] += min(curse_boost, H2H_CURSE_BOOST_HOME)
            result["labels"].append(f"往绩魔咒({curse_streak}连胜主队)")
        elif curse_favor == "away":
            result["xG_adjust_b"] += min(curse_boost, H2H_CURSE_BOOST_AWAY)
            result["labels"].append(f"往绩魔咒({curse_streak}连胜客队)")

    # 8) 进球荒因子：连续3场以上没进球
    # 传递方式：intelligence.json 的 teams.<code>.goal_drought 字段
    for code in (a, b):
        team_intel = intel.get("teams", {}).get(code, {}) if intel else {}
        drought = team_intel.get("goal_drought", 0)
        if drought >= GOAL_DROUGHT_THRESHOLD:
            drought_penalty = GOAL_DROUGHT_PENALTY
            if code == a:
                result["xG_adjust_a"] += drought_penalty
            else:
                result["xG_adjust_b"] += drought_penalty
            result["labels"].append(f"{code}进球荒({drought}场)")

    # 9) 平局基础概率调整：实力接近时
    if elo_gap < CLOSE_MATCH_ELO_THRESHOLD:
        result["draw_boost"] = result.get("draw_boost", 0.0) + CLOSE_MATCH_DRAW_BOOST
        result["labels"].append("实力接近(平局+4%)")

    # 10) 冷门预警：当往绩+状态+伤停三重因素叠加时
    upset_factors = 0
    # 往绩因素
    if curse_streak >= H2H_CURSE_THRESHOLD:
        upset_factors += 1
    # 状态因素：一方状态火热 vs 另一方状态低迷
    # 这个需要从外部传入，暂时用 elo_delta 判断
    if elo_delta_a < -10 or elo_delta_b < -10:
        upset_factors += 1
    # 伤停因素
    if elo_delta_a < INJURY_CRISIS_THRESHOLD or elo_delta_b < INJURY_CRISIS_THRESHOLD:
        upset_factors += 1
    if upset_factors >= UPSET_WARNING_THRESHOLD:
        result["upset_warning"] = True
        result["upset_penalty"] = UPSET_WARNING_PENALTY
        result["labels"].append("冷门预警(多因素叠加)")

    # ---- 2026-09-06复盘新增：强队低迷+防守韧性场景修正 ----

    # 场景1：强队2轮以上不胜/0球 → 胜率下调8-12%
    # 传递方式：intelligence.json 的 teams.<code>.form_drought 字段
    # form_drought: int = 连续无胜或0球的场次数
    for code in (a, b):
        team_intel = intel.get("teams", {}).get(code, {}) if intel else {}
        drought_streak = team_intel.get("form_drought", 0)
        if drought_streak >= FORM_DROUGHT_THRESHOLD:
            drought_penalty = FORM_DROUGHT_PENALTY
            if code == a:
                result["xG_adjust_a"] += drought_penalty
            else:
                result["xG_adjust_b"] += drought_penalty
            result["labels"].append(f"{code}状态低迷({drought_streak}轮不胜/0球)")

    # 场景2：升班马+主场+防守型 → 平局概率上调4-6%
    # 传递方式：intelligence.json 的 matches[].promoted_home_defensive 字段
    promoted_home_def = intel.get("promoted_home_defensive", False) if intel else False
    if promoted_home_def:
        result["draw_boost"] = result.get("draw_boost", 0.0) + PROMOTED_HOME_DRAW_BOOST
        result["labels"].append("升班马主场防守(平局+5%)")

    # 场景3：强队客场+对手魔鬼主场 → 客胜概率下调6-8%
    # 传递方式：intelligence.json 的 matches[].fortress_venue 字段
    fortress_venue = intel.get("fortress_venue", False) if intel else False
    if fortress_venue:
        result["xG_adjust_b"] += FORTRESS_AWAY_PENALTY  # 客队xG下调
        result["labels"].append("对手魔鬼主场(客胜-7%)")

    # 场景4：强队近期被逆转 → 状态信心修正-10%
    # 传递方式：intelligence.json 的 teams.<code>.recent_reversal 字段
    for code in (a, b):
        team_intel = intel.get("teams", {}).get(code, {}) if intel else {}
        recent_reversal = team_intel.get("recent_reversal", False)
        if recent_reversal:
            reversal_penalty = REVERSAL_CONFIDENCE_PENALTY
            if code == a:
                result["xG_adjust_a"] += reversal_penalty
            else:
                result["xG_adjust_b"] += reversal_penalty
            result["labels"].append(f"{code}近期被逆转(信心-10%)")

    # ---- 11) 欧战客场保守系数（2026-09-09新增） ----
    # 新赛制下强队客场打更强对手时可能保守战术（铁桶阵保平）
    EURO_AWAY_CONSERVATIVE_ELO_THRESHOLD = 150  # Elo差阈值
    EURO_AWAY_CONSERVATIVE_XG_PENALTY = -0.10   # 客场xG下调
    EURO_AWAY_CONSERVATIVE_DRAW_BOOST = 0.03    # 平局概率上调
    EURO_COMPETITIONS_SET = {"champions_league", "europa_league"}
    if league in EURO_COMPETITIONS_SET and elo_gap > EURO_AWAY_CONSERVATIVE_ELO_THRESHOLD:
        # b是客队，如果b的Elo显著低于a → b可能保守
        if elo_b < elo_a:
            result["xG_adjust_b"] += EURO_AWAY_CONSERVATIVE_XG_PENALTY
            result["draw_boost"] = result.get("draw_boost", 0.0) + EURO_AWAY_CONSERVATIVE_DRAW_BOOST
            result["labels"].append("客队保守战术(铁桶阵+反击)")
        # a是主队，如果a的Elo显著低于b → a可能保守
        elif elo_a < elo_b:
            result["xG_adjust_a"] += EURO_AWAY_CONSERVATIVE_XG_PENALTY
            result["draw_boost"] = result.get("draw_boost", 0.0) + EURO_AWAY_CONSERVATIVE_DRAW_BOOST
            result["labels"].append("主队保守战术(铁桶阵+反击)")

    # ---- 12) 轮换风险标签（2026-09-09新增） ----
    # 读取intelligence_snapshot中的next_match字段，判断轮换风险
    ROTATION_RISK_PENALTY = -0.08  # 轮换时xG下调
    ROTATION_RISK_LABELS = {"high": "轮换风险高", "medium": "轮换风险中", "low": "轮换风险低"}
    if intel:
        next_match = intel.get("next_match", {})
        rotation_risk = next_match.get("rotation_risk", "")
        if rotation_risk in ROTATION_RISK_LABELS:
            risk_label = ROTATION_RISK_LABELS[rotation_risk]
            next_desc = next_match.get("description", "")
            # 判断哪支队伍有轮换风险（通过next_match.affected_team）
            affected = next_match.get("affected_team", "")
            if affected == a:
                result["xG_adjust_a"] += ROTATION_RISK_PENALTY
                result["labels"].append(f"{a}{risk_label}({next_desc})")
            elif affected == b:
                result["xG_adjust_b"] += ROTATION_RISK_PENALTY
                result["labels"].append(f"{b}{risk_label}({next_desc})")
            elif not affected:
                # 未指定受影响队伍时，对客队默认生效（客队更可能轮换）
                result["xG_adjust_b"] += ROTATION_RISK_PENALTY
                result["labels"].append(f"{b}{risk_label}({next_desc})")

    return result


# ================================================================
# 自动调参覆盖（model_overrides.json）
# 导入时把 references/model_overrides.json 里的全局常量覆盖应用到本模块
# ================================================================
_OVERRIDES = load_model_overrides()
if _OVERRIDES:
    for _k, _v in _OVERRIDES.items():
        if _k in globals():
            globals()[_k] = _v
    del _OVERRIDES, _k, _v
else:
    del _OVERRIDES