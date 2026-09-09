# 单场预测驱动模式（In-Memory Driver）

## 概述

默认工作流把赛前情报写入 `data/live/<league>/intelligence.json` 快照，再由 `league_predict.py` 读取。这对**前瞻性预测**没问题，但以下场景直接写快照会污染共享数据：

1. **次回合 / 非快照比赛**：快照里的 intelligence 条目可能是**首回合**上下文（主客队、伤停、战术、战意全变了）。覆盖它会永久丢失首回合情报。
2. **已完赛比赛复盘**：比赛已踢完（results.json 可查或赛果已公开），写快照意义不大，还会留下过期情报影响后续模拟。
3. **情景试验**：想测试不同情报假设（不同战术 / 伤停 / 战意）对胜率的影响。

**驱动模式**：用一段临时 Python 脚本在**内存中构造** intelligence 字典，直接调用 `cmd_match()`，跑完即删。

> **必入台账（用户要求，2026-08-16）**：`cmd_match` 默认 `record=True`，预测**必写入** `data/reviews/predictions.json`（预测台账）。台账是每日复盘闭环的数据地基；`--no-record` / `record=False` 仅用于情景试验/纯试算。驱动脚本无需传 `record=True`（已是默认）。

---

## 驱动脚本模板

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时驱动：<主队中文名> vs <客队中文名> <第几回合>预测（不修改任何数据文件）"""
import sys, os
SKILL = os.path.join("<skill 绝对路径>", ".claude", "skills", "football-prediction")
sys.path.insert(0, os.path.join(SKILL, "scripts"))
from league_data import load_league_teams, load_league_context, load_results_data, compute_elo_delta
import league_predict as lp

LEAGUE = "europa_league"   # 联赛代码，见 SKILL.md 联赛配置
teams = load_league_teams(LEAGUE)          # 读取现有 Elo 评级
ctx = load_league_context(LEAGUE)          # 读取联赛特殊因子
results_data = load_results_data(LEAGUE)   # 读取已赛结果（交手统计）

intelligence = {
    "teams": {
        "MID": {
            "elo_delta": compute_elo_delta([
                {"role": "starter", "count": 2},
                {"role": "rotation", "count": 2},
            ]),
            "confidence": 0.85,
            "form": "胜-胜-平-负-胜",      # 最新一场在最后；可省略则中性
            "notes": "伤停 / 状态 / 特殊背景摘要"
        },
        "BJK": {
            "elo_delta": compute_elo_delta([{"role": "starter", "count": 2}]),
            "confidence": 0.85,
            "notes": "..."
        }
    },
    "matches": [
        {
            "teams": ["MID", "BJK"],       # [主队, 客队]
            "confidence": 0.85,
            "goal_delta": {"MID": 0.05, "BJK": 0.0},
            "competition_context": {
                "type": "decider",
                "goal_boost": {"MID": 0.10, "BJK": 0.0},
                "note": "晋级情境说明，例如：总比分0-1落后需净胜2球"
            },
            "context_openness": {"MID": 0.10, "BJK": 0.05},
            "tactical": {
                "style_a": "high_press",
                "style_b": "counter_attack",
                "matchup": "高压 vs 防反 — 克制关系描述",
                "expected_pattern": "open",      # open / cautious / balanced
                "ineffective_possession": False,
                "physical_mismatch": 0,
                "derby_boost": False,
                "fighting_spirit": {
                    "a": {"level": "high", "note": "争冠冲刺，主场必须取胜"},
                    "b": {"level": "normal"}
                }
            },
            "factors": [{"summary": "关键情报点 1（给用户展示）"}, {"summary": "..."}],
            "notes": ["赛前信息 / 参考预测"]
        }
    ]
}

lp.cmd_match(teams, "MID", "BJK", LEAGUE, intelligence, {}, False, {}, results_data)
```

运行（Windows bash）：
```bash
export PYTHONIOENCODING=utf-8 && python3 _tmp_xxx.py
```

跑完删除临时脚本：
```bash
rm -f _tmp_xxx.py
```

---

## 只读数据源（绝不修改）

| 文件 | 作用 |
|------|------|
| `references/<league>/teams.json` | 球队 Elo 评级（基准实力） |
| `references/<league>/context.json` | 联赛特殊因子 |
| `data/live/<league>/results.json` | 已赛结果（交手次数统计用） |
| `scripts/league_predict.py` / `league_data.py` | 模型代码（`cmd_match` / `compute_elo_delta` / `compute_tactical_delta`） |

intelligence 字典只存在于脚本内存，预测结束后无任何持久化副作用。

---

## intelligence 字段速查

与 `data/live/<league>/intelligence.json` 结构完全一致，仅不再写入文件。

### `teams.<code>`（团队级）
| 字段 | 说明 |
|------|------|
| `elo_delta` | 用 `compute_elo_delta()` 计算（负值=实力下降），会按 `confidence` 缩放并 clamp ±30 |
| `confidence` | 0~1，情报可信度 |
| `form` | `"胜-胜-平-负-胜"`，最新一场在最后，自动量化为 ±0.10 xG；格式错误（无分隔符/未知 token/放错键位）会输出 ⚠️ 警告并按中性处理 |
| `notes` | 伤停 / 状态 / 特殊背景摘要 |

### `matches[].tactical`（战术对位，必填）
| 字段 | 说明 |
|------|------|
| `style_a` / `style_b` | 主 / 客队风格枚举：`high_press` / `possession` / `counter_attack` / `direct` / `defensive_deep` / `physical` / `hybrid` |
| `matchup` | 克制关系简述 |
| `expected_pattern` | `open` / `cautious` / `balanced`；`open` 还必须通过下方准入门槛 |
| `open_eligibility` | 仅 `open` 必填：双方 `attack_ready`、双方 `transition_threat`、双方 `no_key_attacking_absences` 均为 `true`，且 `first_leg_or_opener_cautious` 为 `false`；任一缺失即自动降为 `balanced` |
| `ineffective_possession` | 传控队面对大巴/反击的无效控球惩罚 |
| `physical_mismatch` | -2~2 身体对抗差异 |
| `derby_boost` | 德比/特殊战意 |
| `fighting_spirit` | 按队战意（A=主队，B=客队）：`{"a": {"level": "high", "note": "..."}, "b": {"level": "normal"}}`；等级 `desperate`(+0.30)/`high`(+0.15)/`normal`(0)/`low`(-0.15)/`dead_rubber`(-0.30)，预测中权重最高 |

### `matches[]` 其他字段
| 字段 | 说明 |
|------|------|
| `goal_delta` | 单场 xG 修正 ±0.35，按 `confidence` 缩放 |
| `competition_context.type` | `decider`（生死战）等，`goal_boost` ±0.20 战意加成 |
| `context_openness` | ±0.30，取两队较大值叠加双方 |
| `factors[]` / `notes[]` | 展示用情报（仅影响输出文案，不影响模型） |

完整战术字段 Schema 与量化规则见 `references/intelligence.md`。

---

## ⚠️ 2026-09-02 复盘教训（三项改进，已固化）

### 教训1：伤停权重大幅上调

**案例**：米尔沃尔7人伤停（3前锋+后卫+中场+2停赛），模型仅扣-14 Elo，实际0-3惨败。

**改进规则**（写入 `model_overrides.json` → `INJURY_ELO_SCALING`）：

| 伤停人数 | Elo扣分 | 说明 |
|---------|---------|------|
| **≥7人** | **-40** | 防线崩盘+替补真空，灾难级 |
| 5-6人 | -30 | 严重影响轮换深度 |
| 3-4人 | -20 | 中等影响 |
| 核心伤缺 | 额外-15 | 叠加在角色扣分之上 |

**驱动模式操作**：`compute_elo_delta` 现在的 `scale_multiplier=2.0`，`cap=-60`。7人伤停应传：
```python
compute_elo_delta([
    {"role": "star", "count": 1},      # 核心 -25×2=-50
    {"role": "starter", "count": 2},   # 首发 -10×2=-20
    {"role": "rotation", "count": 2},  # 轮换 -5×2=-10
])
# 实际返回约-38~-40（递减后）
```

### 教训2：升班马/弱队客场韧性加成

**案例**：雷克瑟姆（升班马，第22名）客场3-0大胜米尔沃尔，3-4-2-1大巴阵型成功防守反击。

**改进规则**（写入 `model_overrides.json` → `PROMOTED_AWAY_RESILIENCE`）：

- **触发条件**：升班马 AND 客场
- **防守加成**：+0.08 xG（防守端）
- **反击加成**：+0.05 xG（进攻端）
- **低 block 阵型额外加成**：+0.06 xG（若阵型为3-4-2-1/5-4-1/5-3-2/4-5-1）

**驱动模式操作**：
```python
"tactical": {
    "style_b": "defensive_deep",  # 升班马客场大概率摆大巴
    "matchup": "XX vs 大巴 — 升班马3中卫低 block 防守",
    "expected_pattern": "cautious",  # 客场保守
}
# 模型会自动应用大巴克制矩阵 + 防守加成
```

### 教训3：盘口降盘信号必须重视

**案例**：米尔沃尔盘口从-0.5降至-0.25，市场早就看到米尔沃尔的问题（7人伤停+上轮1-5惨败），模型未充分采信。

**改进规则**（写入 `model_overrides.json` → `MARKET_LINE_SIGNAL`）：

- **触发条件**：盘口降≥0.25球
- **平局概率**：+5%
- **热门方向概率**：-5%

**驱动模式操作**：
```python
# 当发现盘口从初盘到即时降了≥0.25球时：
# 1. 在factors[]中标注："⚠️ 盘口降盘预警：从X降至Y"
# 2. 在intelligence中手动调整：
intelligence["matches"][0]["context_openness"] = {"MIL": 0.10, "WRE": 0.05}
# 增加不确定性/平局倾向
```

**盘口信号判断标准**：
| 降盘幅度 | 信号强度 | 操作 |
|---------|---------|------|
| 降0.25球 | 🟡 轻微 | 平局+3% |
| 降0.5球 | 🟠 中等 | 平局+5%，热门-5% |
| 降0.75球+ | 🔴 强烈 | 平局+8%，热门-8%，考虑反向 |

### 教训4：开放局的大比分尾部偏轻

**案例**：`expected_pattern = open` 的比赛里，模型对 3球以上和双方进球的尾部分布偏保守，容易低估 2-2、3-2、2-3、3-3、4-2、2-4 这类高比分。

**改进规则**（写入 `model_overrides.json` → `OPEN_MATCH_TAIL_BOOST`）：

- **触发条件**：`expected_pattern == open`
- **尾部进球加成**：+0.06 xG
- **高比分乘数**：`1.15`
- **很高比分乘数**：`1.25`
- **优先抬升比分**：`2-2/3-2/2-3/3-3/4-2/2-4`

**驱动模式操作**：
```python
# open型比赛里：
intelligence["matches"][0]["tactical"]["expected_pattern"] = "open"
# 若双方都能进球且强弱差明显，需额外抬升高比分尾部
# factors[]里明确写："开放局，高比分尾部上修"
```

### 教训5：客强队平局保护要加重

**案例**：客队Elo更高，但若同时存在伤停多、连续客场、盘口降盘、对手主场战意强，平局往往被低估。

**改进规则**（写入 `model_overrides.json` → `AWAY_FAVORITE_DRAW_PROTECTION`）：

- **触发条件**：客队为热门且同时满足多重风险
- **平局概率**：+5%
- **热门方向概率**：-4%
- **触发参考**：伤停≥4、连续客场≥2、盘口降≥0.25、对手主场战意高涨

**驱动模式操作**：
```python
# 客强队但风险叠加时：
intelligence["matches"][0]["context_openness"] = {"HOME": 0.05, "AWAY": 0.05}
# 并在factors[]标注："客强队平局保护上调"
```

---

## 盘口分析 + 诱盘检测（可选）

提供实时亚盘数据时，模型会把「预期让球差」与「市场盘口」对比，检测是否**诱盘**并决定是否采信市场。**不传 `odds_data` 时整条链路完全不变**（向后兼容）。

### `odds_data` 输入格式
```python
odds_data = {
    "matches": [
        {
            "home": "SIR", "away": "BRO",
            "handicap": "主让一球",          # 中文盘口文本（可选）
            "handicap_line": -1.0,           # 数值盘口，主队视角（主让为负/受让为正）
            "water_home": 0.95,              # 主队水位（0.80 以下=市场强烈引导该侧）
            "water_away": 0.90,              # 客队水位
            # "total_line": 2.5,             # 大小球盘口（暂未参与计算，预留）
        }
    ]
}
```
- 主客对按 `{home, away}` 双向匹配；`handicap_line` 缺失时会尝试解析 `handicap` 文本（`parse_asian_handicap`）。
- 只提供水位 / 只提供盘口均可；两者都缺则该场跳过盘口分析。

### 驱动模式调用
```python
lp.cmd_match(teams, "SIR", "BRO", LEAGUE, intelligence, {}, False, {}, results_data,
             date="2026-08-11", record=True, odds_data=odds_data)
# build_match_entry 同样多一个位置参数：…, date, odds_data)
```

### 命令行等价方案
```bash
python3 scripts/league_predict.py match SIR BRO --league allsvenskan --odds odds.json
```

### 输出字段（`prediction.odds`，无盘口数据时为 None）
| 字段 | 说明 |
|------|------|
| `handicap_line` / `handicap_cn` | 本场盘口（主队视角） |
| `model_margin` / `market_margin` | 模型预期让球差 vs 市场预期让球差（= -盘口） |
| `divergence` | 两者之差（>0 = 模型认为主队比盘口更强） |
| `trap_score` | 诱盘评分 0~6（分歧 + 水位失衡 + 模型信心） |
| `trap_decision` | `normal` / `suspect` / `trap` |
| `market_weight` | 市场可信权重 `0.3 / 0.15 / 0.0`（`trap`=0 完全走模型） |
| `market_confidence` | `high` / `medium` / `ignore` |
| `market_factor` | 施加到 xG 的对称修正（`la += f, lb -= f`） |
| `original_xg` / `final_xg` | 盘口调整前 / 后的 xG |

判据与调参见 `scripts/odds_analyzer.py` 与 `references/odds_config.json`。两个内置测试场景（正常盘口 → `trap_score<=2`、权重 0.3；明显诱盘 → `trap_score>=4`、权重 0）可用：
```bash
python3 scripts/odds_analyzer.py --handicap "受让一球"
```

### 诱盘判定复盘
每日 `review` 时，若当日预测含盘口分析，会追加「诱盘判定复盘」段，评估**忽略盘口走模型是否有效**，明细写入 `data/reviews/trap_log.json`（含模型方向 vs 市场方向 vs 实际、命中情况）。累积样本后据此判断 trap 判定 / 权重 / 阈值是否需要微调。

---

## 已完赛比赛复盘流程

比赛已结束时应执行（skill 工作流"已完赛比赛的处理"）：

1. **联网查实际比分**，先告知用户赛果。
2. 用驱动模式跑"赛前预测"用于对比验证。
3. **赛后复盘**：
   - 模型概率最高方向 / 最高比分是否命中实际结果 → 命中说明模型逻辑合理。
   - 未命中 → 分析哪些因子被低估 / 高估（战术克制、伤停、红牌、个体爆发等）。

---

## 命令行等价方案（需要持久化情报时）

若某场比赛的情报**应该**长期保留供联赛模拟使用，才改快照：

```bash
# 1. 备份
cp data/live/<league>/intelligence.json data/live/<league>/intelligence.json.bak
# 2. 改写 intelligence.json（写入新情报条目）
# 3. 运行
python3 scripts/league_predict.py match MID BJK --league europa_league
# 4. 还原（默认）
mv data/live/<league>/intelligence.json.bak data/live/<league>/intelligence.json
```

**默认**优先使用驱动模式；只有明确需要把情报持久化进快照时才走命令行方案。
