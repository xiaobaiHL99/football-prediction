# 理性预测模型说明 — 联赛版

三层模型：**Elo 评级 → Agent 情报修正 → 足球随机性修正泊松比分 → 蒙特卡洛模拟**。预测脚本本身不联网；如果用户要求"最新/当前"预测，由 Agent 先核验真实积分榜、赛程、赛果，并在单场预测前保存赛前情报快照。

## 1. 球队评级（Elo）
每队需要一个 `elo` 值（约 1400–1920）。脚本读取 `references/<league>/teams.json` 中的评级数据。Agent 保存 live 快照时应保留 `elo`、`name`、`color`、`turf` 等字段。

### 联赛 Elo 范围参考

| 联赛 | Elo 范围 | 说明 |
|------|----------|------|
| 挪超 | 1450–1780 | 博德闪耀最高，保级队最低 |
| 瑞超 | 1450–1750 | 马尔默最高，升降机最低 |
| MLS | 1650–1920 | 洛杉矶FC/迈阿密国际最高，重建队最低 |

## 2. Agent 赛前情报修正

当存在 `data/live/<league>/intelligence.json` 时，脚本会读取 Agent 已保存的赛前情报快照：

- `teams.*.elo_delta`：团队级有界修正，限制在 `-80` 到 `+80` Elo 之间，影响单场和联赛模拟。
  - 修正后的 Elo 参与净胜球计算：`sup = ((elo_A + delta_A) - (elo_B + delta_B)) / elo_scale`
  - 与 `goal_delta` 正交叠加（Elo 层 + xG 层独立修正），同时生效
  - `confidence`（0.0-1.0）可缩放修正幅度
  - 联赛模拟（`cmd_table`/`simulate_season`）中所有剩余比赛均受影响
  - Agent 可使用 `compute_elo_delta()` 辅助函数（在 `league_data.py` 中）将结构化缺席数据转为 elo_delta 值

- `matches[].goal_delta`：指定两队单场的进球期望修正，限制在 `-0.35` 到 `+0.35` 之间，并按 `confidence` 折算。

这些修正来自伤停、预计首发、近期状态、赛程体能、场地天气、战术对位、比赛动机等信息。情报只调整基础预期，不取消足球随机性，也不能包装成确定性结论。

### 球员可用性辅助函数

Agent 可使用 `compute_elo_delta()`（位于 `league_data.py`）将结构化球员缺席数据转换为 Elo 修正值：

| role 值 | 单球员影响 | 典型场景 |
|---------|-----------|---------|
| `star`  | -25 Elo   | 梅西、哈兰德级核心缺阵 |
| `goalkeeper_star` | -20 Elo | 主力门将缺阵 |
| `starter` | -10 Elo | 常规首发缺阵 |
| `rotation` | -3 Elo  | 轮换球员缺阵 |

多类别缺席时自动递减：`总数 × 0.85^(类别数-1)`。Agent 应将计算结果填入 `intelligence.json` 的 `teams.<code>.elo_delta` 字段。

## 3. 泊松比分模型

由两队 Elo 差换算"净胜球期望"(goal supremacy)，再拆成各自的期望进球 λ：

```
sup = (elo_A - elo_B) / elo_scale      # 每 elo_scale 分约合 1 个净胜球
λ_A = avg_goals/2 + sup/2 + home_adv   # 主队加成
λ_B = avg_goals/2 - sup/2              # 客队无加成
```

### 联赛参数（来自 league_config.json）

| 联赛 | avg_goals | home_adv | elo_scale | draw_base | shock_sd |
|------|:---------:|:--------:|:---------:|:---------:|:--------:|
| 挪超 | 3.0 | 0.40 | 110 | 0.22 | 0.30 |
| 瑞超 | 2.7 | 0.38 | 115 | 0.26 | 0.28 |
| MLS | 2.8 | 0.50 | 120 | 0.22 | 0.30 |

### 联赛特殊因子

每个联赛有独特因子，在 `context.json` 中定义，通过 `apply_special_factors()` 作用于预期进球：

**挪超**：
- 人工草皮惩罚：客队从天然草皮到人工草皮适应成本 `-0.12 xG`
- 欧战消耗：周中欧战/资格赛后 `-0.08 xG`
- 北方主场优势：博德/特罗姆瑟等北极圈球队 `+0.06 xG`
- 夏季高进球：6-7月极昼期间进球增多 `+0.15 xG`

**瑞超**：
- 春季场地：3-4月场地偏硬 `-0.05 xG`
- 欧战消耗： `-0.06 xG`
- 斯德哥尔摩德比：AIK vs DJU 战意加成 `+0.10 xG`
- 哥德堡德比：GAIS vs SOF 战意加成 `+0.08 xG`

**MLS**：
- 旅行距离：跨东西海岸 `-0.10 xG`（双方）
- 人工草皮惩罚： `-0.10 xG`
- 季后赛冲刺：8月后争季后赛席位 `+0.06 xG`
- 夏窗新援： `+0.05 xG`
- 国际比赛日减员： `-0.06 xG`

- 比分 (i, j) 概率 = Poisson(i; λ_A) × Poisson(j; λ_B)，对 0–10 球截断。
- 单场输出会对若干状态冲击点做加权混合，而不是只使用一个静态 λ。
- 胜/平/负概率 = 对该矩阵按 i>j / i=j / i<j 求和。

### 主预测结果与比分分布

- 联赛单场的主预测赛果取胜/平/负三项中概率最高者。
- 主预测比分必须从与主预测赛果一致的比分里选择最高概率项。例如主方向是客胜，就不能把 `1-1` 包装成主预测比分。
- 联赛没有淘汰赛，不涉及点球决胜。

## 4. 足球不稳定因素

足球单场样本小、进球少，冷门和偶然性显著。脚本用三层方式避免过度确定：

1. **泊松进球随机性**：即使期望进球固定，实际比分也会随机抽样。
2. **单场状态冲击**：未锁定的比赛在抽样前加入一次零均值冲击，模拟临场状态、战术匹配、伤停、红牌、天气、裁判尺度等未显式建模因素：

```
shock ~ Normal(0, shock_sd)
λ_A' = max(0.15, λ_A + shock/2)
λ_B' = max(0.15, λ_B - shock/2)
```

3. **概率收缩**：单场胜平负输出会向联赛基线收缩，避免强队概率显得过满。基线为 `((1-draw_base)/2, draw_base, (1-draw_base)/2)`。

### 比赛情境加成（goal_boost）与开放性系数（openness）

`match` 命令支持通过 `intelligence.json` 中的 `competition_context` 注入比赛情境对进球预期的影响：

| 类型 | goal_boost | openness | 说明 |
|------|:----------:|:--------:|------|
| normal | 0.00 | 0.00 | 基线，无特殊情境 |
| must_win | +0.08 | 0.00 | 必须获胜才能争冠/保级 |
| decider | +0.12 | 0.00 | 直接对决（六分战） |
| derby | +0.10 | 0.00 | 德比战战意加成 |
| no_pressure | +0.10 | +0.10 | 已无压力，开放比赛 |
| rotated | -0.15 | +0.08 | 轮换阵容（杯赛前） |

## 5. 蒙特卡洛联赛模拟

默认 1 万次，每次：
1. **锁定已赛结果**：`results.json` 中已有的比赛用真实比分。
2. **模拟剩余比赛**：每场按修正泊松模型抽样比分。
3. **积分排名**：按积分 → 净胜球 → 进球数 → 随机项排序。
4. **统计输出**：
   - 争冠概率（第1名）
   - 欧战资格概率（挪超/瑞超前4，MLS分区排名）
   - 降级概率（后2名）

## 6. 真实赛果锁定

`data/live/<league>/results.json` 中已结束的比赛会被**锁定**，用真实比分代替模拟：

```json
{"matches": [
  {"home": "BOD", "away": "MOL", "hg": 2, "ag": 1}
]}
```

锁定使用 `frozenset((home, away))` 作为键，确保同一对组合不再重新模拟。

## 已知简化（可后续增强）
- 评级为静态值，未做赛中动态升降。
- 伤停、红牌、体能、天气等可通过 Agent 情报快照做有界修正。
- MLS 季后赛赛制（三局两胜首轮 + 单淘汰）尚未单独建模，当前仅模拟常规赛积分榜。
- 北欧联赛冬歇期后的状态恢复未单独建模。

## 7. 近期状态因子（Form Factor）

当 `intelligence.json` 的 `teams.<code>.form` 包含近5场字符串（如"胜-胜-负-胜-平"或"W-W-L-W-D"），模型将其量化为状态分数并转为 xG 调整值。

### 量化过程

```
form_str → 逐场映射 [胜=1.0, 平=0.5, 负=0.0]
         → 倒序加权：最新场 × 1.0, 次新场 × 0.85, 再前场 × 0.85² ...
         → 归一化为 0.0~1.0 的 form_score
         → xG_adjust = (form_score - 0.5) × 2 × FORM_XG_MAX
```

| form_str | form_score | xG_adjust |
|----------|-----------|-----------|
| 胜-胜-胜-胜-胜 | 1.000 | +0.100 |
| 胜-胜-平-负-胜 | 0.683 | +0.037 |
| 平-平-平-平-平 | 0.500 | 0.000 |
| 胜-负-负-负-负 | 0.260 | -0.048 |
| 负-负-负-负-负 | 0.000 | -0.100 |

### 常量

| 常量 | 默认值 | 说明 |
|------|--------|------|
| FORM_XG_MAX | 0.10 | 最大 xG 调整幅度（5连胜或5连败）|
| FORM_DECAY_FACTOR | 0.85 | 场次位置衰减系数，越早的比赛权重越低 |

### 适用场景
- 单场预测（`cmd_match`）：直接叠加到 xG_adjust
- 联赛模拟（`cmd_table` / `simulate_season`）：预提取所有球队 form，每场比赛分别叠加

## 8. 交手熟悉度因子（Meeting Familiarity）

当两队在本赛季已交锋达到阈值次数，弱队能从多次交手中获得战术准备优势。

### 原理
- 多次交手后：强队的战术套路被充分研究，突袭效果递减
- 弱队可以针对性布置（盯人、阵型收缩、反击路线设计）
- 韩职12队38轮制尤为明显（同组交手3-4次）

### 判定条件

| 条件 | 阈值 | 触发结果 |
|------|------|---------|
| meetings ≥ | 3 | 弱队 +0.04 xG / 强队 -0.03 xG |
| Elo 差 > 50 | (附加) | 标签显示"弱队熟悉度加成" |

### 常量

| 常量 | 默认值 | 说明 |
|------|--------|------|
| MEETING_FAMILIARITY_THRESHOLD | 3 | 触发熟悉度修正的最低交手次数 |
| FAMILIARITY_UNDERDOG_BOOST | 0.04 | 弱队 xG 加成 |
| FAMILIARITY_FAVORITE_PENALTY | -0.03 | 强队 xG 惩罚 |

### 数据来源
`results.json`（`data/live/<league>/results.json`）中的 `matches` 列表，通过 `count_meetings()` 函数统计两队已赛场次。

## 9. 战术反制因子（Tactical Counter）

由 Agent 在 `intelligence.json` 的 `matches[].tactical` 字段中手动标注，用于表达"弱队通过特定战术安排缩小实力差距"的场景。

### 触发条件

```json
{
  "tactical": {
    "counter_strategy": true,    // 必须为 true 才生效
    "underdog_boost": 0.06,      // 弱队 xG 加成
    "favorite_penalty": -0.05,   // 强队 xG 惩罚
    "description": "高位逼抢破坏出球体系"  // 描述文本
  }
}
```

- 仅当 `counter_strategy = true` 时生效
- `underdog_boost` 上限 +0.12，`favorite_penalty` 下限 -0.12
- 系统自动判断哪一方是弱队（Elo 低者），将加成/惩罚正确分配

### 适用场景（仅单场预测）
- 战术反制仅在 `cmd_match` 单场预测中生效
- 联赛模拟中不使用（战术信息通常只对单场有效）

### 使用建议
- 仅在 Agent 确认存在明显战术克制时标注（如：锁死核心球员、针对性阵型调整）
- 避免过度使用——不是每场比赛都有清晰的战术克制关系

### 三者叠加上限
近期状态(±0.10) + 交手熟悉度(+0.04/-0.03) + 战术反制(+0.06/-0.05) 合计控制在 ±0.20 xG 以内，不压过 Elo 基础差距。

## 10. 战术对位因子（Tactical Matchup Factors, v3 新增）

战术对位因子是 `intelligence.json` 中 `matches[].tactical` 扩展字段的自动化量化层。与第9节的 "战术反制"（手动标注弱队克制）不同，本层通过 `compute_tactical_delta()` 函数自动计算，覆盖风格克制、对攻预期、无效控球、身体对抗、德比战意、战意因素六个维度。

### 数据流

```
intelligence.json matches[].tactical
  → match_intelligence() 提取 tactical_matchup dict
  → expected_goals() 接收 tactical 参数
  → compute_tactical_delta(tactical) 返回 (delta_a, delta_b)
  → 叠加到 la, lb（与 goal_delta 正交）
```

### 风格克制矩阵

克制关系以 `(style_a, style_b) → (delta_a, delta_b)` 形式存储于 `TACTICAL_MATRIX` 常量字典：

| Style A | Style B | delta_A | delta_B | 原理 |
|---------|---------|:-------:|:-------:|------|
| high_press | possession | +0.20 | -0.20 | 高强度压迫破坏传控出球路线 |
| high_press | defensive_deep | -0.15 | +0.15 | 缺乏空间进行有效逼抢 |
| possession | defensive_deep | +0.15 | -0.15 | 耐心传导可缓慢渗透密集防线 |
| possession | counter_attack | -0.20 | +0.20 | 丢失球权后被快速反击打击 |
| counter_attack | high_press | +0.25 | -0.25 | 最典型的克制：利用逼抢身后的空间 |
| counter_attack | possession | +0.20 | -0.20 | 断球后直击传控队薄弱防线 |
| counter_attack | defensive_deep | -0.10 | +0.10 | 缺乏反击所需的空间 |
| direct | possession | +0.15 | -0.15 | 长传绕过传控型中场屏障 |
| direct | defensive_deep | -0.10 | +0.10 | 长传无法穿透密集防线 |
| direct | high_press | +0.10 | -0.10 | 长传越过压迫层直接威胁 |
| physical | possession | +0.15 | -0.15 | 身体对抗破坏传控节奏 |
| physical | counter_attack | -0.08 | +0.08 | 阵型松散容易被速度型反击打穿 |
| physical | high_press | -0.10 | +0.10 | 出球精度低面对逼抢失误多 |
| defensive_deep | high_press | +0.10 | -0.10 | 深度防守压缩逼抢所需的空间 |

### 六因子叠加规则

| 因子 | 函数 | 范围 | 说明 |
|------|------|:----:|------|
| 风格克制 | TACTICAL_MATRIX 查表 | ±0.08~0.25 | 混合型(hybrid)效果折半，不匹配组合=0 |
| 对攻预期 | expected_pattern | ±0.15~0.20 | open=+0.20, cautious=-0.15, balanced=0 |
| 无效控球 | ineffective_possession | -0.18~0.35 | 传控/混合型队面对大巴时的xG扣减 |
| 身体对抗 | physical_mismatch | ±0.06~0.24 | 每级±0.12，连带效应减半作用于对方 |
| 德比战意 | derby_boost | +0.18/+0.08 | 主队+0.18，客队+0.08 |
| 战意因素 | fighting_spirit | 每队 ±0.15~0.30 | 按队显式标注，预测中权重最高的人为因子 |

### 软截断（Soft Clamp）

单队总修正超过 ±0.25 xG 后，超出部分按**对数衰减**：

```
if val > 0.25:
    return 0.25 + log(1 + val - 0.25) * 0.10 / log(1 + 0.10)
```

绝对上限 ±0.35 xG（理论上任何战术调整都不应完全压过 Elo 基础差距）。

### 向后兼容

- 所有函数参数默认为 `None` 或 `{}`
- 当 `tactical` 字段在 intelligence.json 中缺失时，返回 (0.0, 0.0)
- 不影响仅有传统 intelligence.json 格式的预测

## 11. 战意因素（Fighting Spirit, v4 新增）

战意是单场预测中**权重最高的人为因子**。通过 `intelligence.json` 的 `matches[].tactical.fighting_spirit` 按队显式标注（A=主队/对阵第一队，B=客队/对阵第二队），由 `compute_fighting_spirit_delta()` 量化为 xG 修正，叠加进 `compute_tactical_delta()`（第6因子）。

### 数据结构

```json
"tactical": {
  "fighting_spirit": {
    "a": {"level": "high", "note": "争冠冲刺，主场必须取胜"},
    "b": {"level": "dead_rubber", "note": "中游无欲无求，主力轮换备战杯赛"}
  }
}
```

兼容简写：`"fighting_spirit": {"a": "high", "b": "normal"}`。

### 等级 → xG 修正（SPIRIT_XG_STEP = 0.15）

| level | 中文 | 典型情境 | xG 修正 |
|-------|------|---------|:-------:|
| `desperate` | 背水一战 | 保级生死、总比分落后、末轮定生死 | **+0.30** |
| `high` | 战意高涨 | 争冠、欧战资格、德比、复仇、新帅首秀 | **+0.15** |
| `normal` | 正常 | 默认 | 0 |
| `low` | 战意偏低 | 已达标、保级无忧、双线保留 | **-0.15** |
| `dead_rubber` | 无欲无求/放弃 | 赛季末无意义、全力备战更关键赛事 | **-0.30** |

修正与风格克制、对攻预期等其他战术因子一并计入软截断（单队总修正 ≤ ±0.35，>±0.25 对数衰减）。

### 与其他战意通道的关系（防重复计分）

模型已有三条战意相关通道，`fighting_spirit` 是 Agent 显式的第四条。同一战意来源**只用一个通道表达**：

| 通道 | 机制 | 何时使用 |
|------|------|---------|
| `classify_match()` 自动 | 保级关键战 / 争冠队 / 副班长殊死战，按积分榜自动加成 | 默认；表格位置已覆盖的情境无需再标 |
| `competition_context` | `must_win`/`decider`/`derby`/`no_pressure`/`rotated` → `goal_boost` ±0.20 | 比赛整体开放度/进球环境 |
| `derby_boost` | 德比氛围 +0.18/+0.08 | 仅德比敌意；已用时不要再把该队 `fighting_spirit` 设为 `high` |
| `fighting_spirit` | 按队 ±0.15~0.30，预测中权重最高 | 复仇、新帅效应、摆烂轮换、无欲无求等自动通道未覆盖的明确战意 |

**原则**：除非有**独立**于上述通道之外的明确战意依据，否则默认 `normal`/省略，避免重复加成。

## 12. 复盘与校准（RAG ≠ 学习）

预测模型是**数值泊松模型**，无法直接消费自由文本——RAG 向量检索只能帮 Agent 找参考资料，不会改变模型本身。真正的"学习"走的是**数据闭环**：预测台账 → 复盘对账 → 自动调参。

```
预测(--record) → data/reviews/predictions.json   预测台账（每条含概率/比分分布/模型输入快照）
                   ↓
daily_review.py review ── 对账 results.json → 命中率/log-loss/Brier → 复盘报告 + reviews.json
                   ↓
样本够(daily_review.py tune) ── 坐标下降 → league_config.json(带备份) / model_overrides.json → tuning_log.json
```

### 关键点

- **台账可复跑**：每条预测保存 `intelligence_snapshot` + 模型输入快照，复盘/调参时原样重放，不改今日输入。
- **调参只动参数、不动逻辑**：每联赛参数写回 `references/league_config.json`（改前自动备份 `league_config.backup.json`）；全局因子（战意/状态幅度）写 `references/model_overrides.json`，导入时自动覆盖常量。均可回滚。
- **防过拟合**：每联赛样本 ≥ 30、全局因子 ≥ 50 才调；时间序列留出验证（老样本训练 / 最新 20% 验证）；验证集 log-loss 改善 ≥ 0.01 才写回；参数值 clamp 在合理区间。
- **指标**：方向命中（主/平/客）、比分命中（主预测比分==实际）、log-loss（`-ln(p_实际赛果)`，1.0≈随机）、Brier（概率校准，0.667≈随机）。

### 与 RAG 的关系

RAG 检索（相似比赛先验 kNN 等）是**候选增强**，台账先积累起"预测 vs 结果"的配对数据，未来才能做校准/相似先验。当前阶段先把闭环跑通，攒样本是第一位。

详细操作见 `references/daily_review.md`。
