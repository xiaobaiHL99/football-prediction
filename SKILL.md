---
name: football-prediction
description: Predict and simulate football league matches for Norwegian Eliteserien, Swedish Allsvenskan, K League 1, J1 League, Major League Soccer, Brazilian Série A, Spanish La Liga, and UEFA Europa League. Uses Elo ratings, Poisson scoreline model, Monte Carlo simulation, and optional I Ching divination. Supports single-match prediction, full league table simulation, title race, European qualification, and relegation analysis. Generate local HTML reports with probability visualization, pre-match intelligence, and optional divination output. Use when the user asks for league match predictions, title race analysis, relegation battle odds, "谁会赢" predictions, or for-entertainment 算卦/玄学 predictions.
---

# 联赛足球预测 ⚽🔮

对挪超(Eliteserien)、瑞超(Allsvenskan)、韩职(K League 1)、日职(J1 League)、美职联(MLS)、巴甲(Brasileirão)、英超(Premier League)、西甲(La Liga)、法甲(Ligue 1)、欧联(Europa League)进行概率预测：单场比分、积分榜模拟、争冠/欧战/降级概率。

默认使用**理性模型**（Elo → 泊松比分 → 蒙特卡洛）；用户明确要求时才叠加**玄学趣味模式**。

本 skill 纯属娱乐分析，不构成投注建议或确定性判断。

## 使用脚本

从当前 skill 根目录运行脚本，不要假设固定安装路径。下例用 `$SKILL_DIR` 表示 `football-prediction` 目录：

### 理性预测（默认）
```bash
# 单场预测
python3 "$SKILL_DIR/scripts/league_predict.py" match BOD MOL --league eliteserien --seed 7
python3 "$SKILL_DIR/scripts/league_predict.py" match MJA SLO --league champions_league --seed 7
python3 "$SKILL_DIR/scripts/league_predict.py" match OLY NEC --league ucl_qualifying --seed 7
python3 "$SKILL_DIR/scripts/league_predict.py" match YFM KSA --league jleague --seed 7
python3 "$SKILL_DIR/scripts/league_predict.py" match LDU IDV --league libertadores --seed 7
python3 "$SKILL_DIR/scripts/league_predict.py" match RMA BAR --league laliga --seed 7
python3 "$SKILL_DIR/scripts/league_predict.py" match MCI ARS --league epl --seed 7
python3 "$SKILL_DIR/scripts/league_predict.py" match PSG MAR --league ligue1 --seed 7

# 积分榜模拟（含争冠/欧战/降级概率）
python3 "$SKILL_DIR/scripts/league_predict.py" table eliteserien --sims 10000

# 联赛完整模拟
python3 "$SKILL_DIR/scripts/league_simulator.py" eliteserien --sims 10000
python3 "$SKILL_DIR/scripts/league_simulator.py" allsvenskan --sims 10000 --seed 7
python3 "$SKILL_DIR/scripts/league_simulator.py" mls --sims 5000 --conference
python3 "$SKILL_DIR/scripts/league_simulator.py" brasileirao --sims 10000
python3 "$SKILL_DIR/scripts/league_simulator.py" europa_league --sims 5000
python3 "$SKILL_DIR/scripts/league_simulator.py" champions_league --sims 5000
python3 "$SKILL_DIR/scripts/league_simulator.py" kleague --sims 10000
python3 "$SKILL_DIR/scripts/league_simulator.py" jleague --sims 10000
python3 "$SKILL_DIR/scripts/league_simulator.py" laliga --sims 10000
python3 "$SKILL_DIR/scripts/league_simulator.py" epl --sims 10000
python3 "$SKILL_DIR/scripts/league_simulator.py" ligue1 --sims 10000
```

- 球队名支持代码（`BOD`）、英文（`Bodø/Glimt`）或中文（`博德闪耀`）。
- 联赛统一使用双循环积分制，胜3分平1分负0分。
- 单场预测输出胜/平/负三项概率、预期进球、比分分布Top5。
- 积分榜模拟包含当前已赛轮次锁定 + 剩余赛程蒙特卡洛模拟。
- 足球偶然性已默认进入模型：泊松比分抽样 + 单场状态冲击 + 概率收缩。
- `--seed` 用于复现结果。
- `--neutral` 中立场（取消主场优势）；`--host <球队>` 标记东道主，该队获得东道主加成（`HOST_BONUS=0.15` xG，可经联赛配置 `host_bonus` 覆盖）。示例：`match YFM KSA --league jleague --host YFM`。
- 东道主也可直接写在球队快照：`references/<league>/teams.json` 中某队加 `"host": true` 字段即自动获得加成（借鉴世界杯逻辑，适合赛事中立场/东道主场景）。
- 预测脚本不联网抓数据；若存在 `data/live/<league>/` 下的快照，会优先读取。

### 玄学模式（需用户主动开启）
```bash
python3 "$SKILL_DIR/scripts/divination.py" BOD MOL --league eliteserien
python3 "$SKILL_DIR/scripts/divination.py" MAL AIK --league allsvenskan --factor
python3 "$SKILL_DIR/scripts/divination.py" FLA PAL --league brasileirao --date 0723
python3 "$SKILL_DIR/scripts/divination.py" TOT ROM --league europa_league --factor
python3 "$SKILL_DIR/scripts/divination.py" ULS JEO --league kleague
python3 "$SKILL_DIR/scripts/divination.py" YFM VIS --league jleague
python3 "$SKILL_DIR/scripts/divination.py" RMA BAR --league laliga
python3 "$SKILL_DIR/scripts/divination.py" MCI ARS --league epl
python3 "$SKILL_DIR/scripts/divination.py" PSG MAR --league ligue1
```
三种玩法（按用户意图选）：**纯玄学** / **科学+玄学并列** / **玄学加权**（把 `--factor` 叠加到泊松胜率）。
玄学脚本可读取 `data/live/<league>/divination_context.json` 中由 Agent 保存的起卦背景。

### 单场预测驱动模式（In-Memory Driver）【默认】
单场预测**默认**使用驱动模式：用临时 Python 脚本在内存中构造 intelligence 字典，直接调用 `cmd_match()`，跑完即删。
- **必入台账（用户要求）**：预测**默认写入** `data/reviews/predictions.json`（预测台账，`cmd_match` 默认 `record=True`）；仅情景试验/纯试算显式传 `record=False`（CLI 用 `--no-record`）跳过。台账是每日复盘闭环的数据地基，缺失会导致复盘统计失真。
- **适用场景**：次回合 / 非快照比赛（快照里是首回合上下文）、已完赛复盘、情景试验。
- 只读 `teams.json` / `context.json` / `results.json`，绝不修改 `data/live/` 快照。
- 模板代码、字段速查、复盘流程见 `references/driver_predict.md`。
- 只有明确需要把情报**持久化**进快照供联赛模拟时，才改写 `intelligence.json` 后走命令行（详见该文档"命令行等价方案"）。

### 盘口分析 + 诱盘检测（可选，单场预测）
提供实时亚盘数据时，模型把「预期让球差」与「市场盘口」对比，检测是否诱盘并决定采信权重（正常 0.3 / 疑似 0.15 / 诱盘 0.0）。**不传 `odds_data` 时完全走原模型**（向后兼容）。

```python
# 驱动模式：cmd_match / build_match_entry 末位传 odds_data
odds_data = {"matches": [{
    "home": "SIR", "away": "BRO",
    "handicap": "主让一球",      # 中文盘口文本，或用数值 handicap_line（主让为负/受让为正）
    "water_home": 0.95, "water_away": 0.90,   # 水位；某侧 <0.80 = 市场强烈引导该侧
}]}
lp.cmd_match(teams, "SIR", "BRO", LEAGUE, intelligence, {}, False, {}, results_data,
             date="2026-08-11", record=True, odds_data=odds_data)

# 命令行等价方案
python3 "$SKILL_DIR/scripts/league_predict.py" match SIR BRO --league allsvenskan --odds odds.json
```

- 输出字段：`prediction.odds` 含 `original_xg` / `market_factor` / `final_xg` / `trap_score` / `trap_decision` / `market_confidence`（无盘口时 None）。
- 诱盘判定会动态调整概率收缩（normal ×0.6 互证更自信 / suspect ×1.0 分歧中性 / trap ×1.2 忽略盘口最谨慎）。
- 每日 `review` 会追加「诱盘判定复盘」段，明细写入 `data/reviews/trap_log.json`，累积样本后评估忽略盘口是否有效。
- 判据与调参：`scripts/odds_analyzer.py` + `references/odds_config.json`；内置两个测试场景（正常盘口 trap_score≤2 权重 0.3 / 明显诱盘 trap_score≥4 权重 0）可 `python3 scripts/odds_analyzer.py` 自测。
- **注意**：盘口分析是赛前情报的有界修正，非确定性结论；数据缺失时明确说明，不编造盘口。

### 每日复盘 + 自动调参（预测台账）
系统把每次预测自动记入结构化台账 `data/reviews/predictions.json`（不是自由文档），每天复盘昨日预测 vs 实际结果，并自动微调参数。这是"预测 → 落台账 → 复盘 → 调参"的闭环。

```bash
# 1) 预测时落台账：单场预测加 --record 与 --date
python3 "$SKILL_DIR/scripts/league_predict.py" match ULS POH --league kleague --date 2026-08-03 --record
# 驱动模式：临时脚本里调 lp.build_match_entry(...) + ledger.append_entry(...)

# 2) 次日复盘（默认复盘昨天；--date 可指定，--league 可过滤）
python3 "$SKILL_DIR/scripts/daily_review.py" review
python3 "$SKILL_DIR/scripts/daily_review.py" review --date 2026-08-02 --league kleague

# 3) 自动调参（攒够样本后；--dry-run 只建议不写回）
python3 "$SKILL_DIR/scripts/daily_review.py" tune --dry-run
python3 "$SKILL_DIR/scripts/daily_review.py" tune --league kleague
```

- 复盘指标：方向命中率、比分命中率、平均 log-loss、平均 Brier；报告在 `reports/reviews/<date>-review.md`，趋势在 `data/reviews/reviews.json`。
- 调参门槛：每联赛 ≥30 条已匹配结果才调该联赛参数；全局因子（战意/状态幅度）≥50 条才调。写回前自动备份 `references/league_config.backup.json`，可回滚。
- 详细说明见 `references/daily_review.md`。

## 工作流程
1. 判定用户要单场预测还是联赛模拟，以及是否明确要求玄学。
2. 理性预测前由 Agent 主动收集当前积分榜、剩余赛程、球队评级、伤停情报等数据。
3. 将基础数据保存为本地快照：`data/live/<league>/table.json`、`data/live/<league>/fixtures.json`、`data/live/<league>/teams.json`（可选）。
4. **单场预测前必须完成两队战术风格研究**。搜索赛前分析、球队本赛季战术特点、主帅风格，判断两队主要战术风格。将战术信息填入 `intelligence.json` 的 `matches[].tactical` 字段（见下方完整格式）。这是必做步骤，非可选。

   **搜索技巧（实战经验）**：
   - 同时搜索中英文关键词：`"<主队> vs <客队> 阵容 阵型 预测"` + `"<team> vs <team> predicted lineup formation"`
   - 搜索单队赛季风格：`"<球队名> 2026 战术 风格"` + `"<team> 2026 tactics formation style"`
   - 搜索赛前情报文章（如 `163.com`, `dszuqiu.com`, `titan007.com`），这些常包含阵型、伤停、战术分析
   - 如果某队上月已搜过，仍需重新确认——战术风格会随主教练、转会、崩盘状态而变化

   **两队同风格的处理**：
   - 当两队风格相同（如 possession vs possession），克制矩阵返回 ±0.00，无直接战术修正
   - 此时预测要**突出其他因子**：主客场战绩差异、近期状态反差、伤停影响、历史交锋心理优势、旅行消耗等
   - 示例：格雷米奥 vs 弗鲁米嫩塞（双方传控）→ 模型无战术修正，但主场龙 vs 客场虫 + 状态反差 + 人员此消彼长驱动了 1-1 的预测

   **已完赛比赛的处理**：
   - 如果发现用户预测的比赛**已结束**（在 results.json 中可查，或赛果已公开），应：
     - 告知用户实际结果
     - 仍运行模型展示赛前预测（用于对比验证）
     - 做赛后复盘：分析预测 vs 实际的偏差原因，检查模型逻辑是否合理
   - 模型概率最高比分命中实际结果 → 说明模型逻辑合理
   - 未命中 → 分析哪些因子被低估/高估

   **球员可用性处理**：对每个缺席的关键球员（伤病、轮休、停赛），使用 `compute_elo_delta()` 计算团队级 Elo 修正并填入 `intelligence.json` 的 `teams.<code>.elo_delta` 字段。此修正会同时影响单场预测和联赛模拟中的所有剩余比赛。

   ```json
   {
     "teams": {
       "MIA": {
         "elo_delta": -45,
         "confidence": 0.90,
         "form": "胜-胜-平-负-胜",
         "notes": "Messi轮休(核心); Suarez腿筋伤"
       },
       "CHI": {
         "elo_delta": 5,
         "confidence": 0.80,
         "form": "负-负-平-胜-负",
         "notes": "阵容齐整"
       }
     },
     "matches": [...]
   }
   ```

   `form` 字段格式：`"胜-胜-平-负-胜"`（中文）或 `"W-W-D-L-W"`（英文），最新一场在最后。form 会自动量化为 ±0.10 xG 调整值，最新比赛权重更高。

   **战术对位字段（必填）**：在 `matches[]` 中必须包含 `tactical` 字段，填入两队战术风格：
   ```json
   {
     "teams": ["BGN", "VIF"],
     "tactical": {
       "style_a": "high_press",
       "style_b": "counter_attack",
       "matchup": "高位逼抢 vs 防守反击 — 经典互克",
       "expected_pattern": "cautious",
       "ineffective_possession": false,
       "physical_mismatch": 0,
       "derby_boost": false,
       "fighting_spirit": {
         "a": {"level": "high", "note": "争冠冲刺，主场必须取胜"},
         "b": {"level": "low", "note": "已保级成功，无欲无求"}
       }
     }
   }
   ```
   战术风格枚举值：`high_press`(高位逼抢) / `possession`(传控) / `counter_attack`(反击) / `direct`(长传) / `defensive_deep`(大巴) / `physical`(身体) / `hybrid`(混合)

   **expected_pattern** 判断规则：
   - 两队都倾向进攻，或关键战必须赢 → `"open"`
   - 一方死守/客场保守，或双方都谨慎 → `"cautious"`
   - 正常情况 → `"balanced"`
   - 注意外部条件影响：高温预警（韩职35-38°C）、恶劣天气会拖慢比赛节奏，加重 `"cautious"` 倾向

   **intelligence.json teams 数据一致性**：
   - `teams.<code>.position` 和 `points` 必须与 `table.json` 一致
   - `teams.<code>.elo_delta` 使用 `compute_elo_delta()` 计算（负值=实力下降）
   - `teams.<code>.form` 格式为 `"胜-胜-平-负-胜"`，最新一场在最后——脚本会自动量化为 ±0.10 xG
   - `teams.<code>.notes` 写入关键情报摘要（伤停、状态、特殊背景）

   战术情报采集方法（按优先级）：
   - **① 本场比赛赛前报道**（最优先）：搜索 `"<主队> vs <客队> 阵容 阵型 预测"` 或 `"<team> vs <team> predicted lineup formation"`。确认本场实际部署。
   - **② 球队常规风格**（基线）：如果搜不到本场确认信息，搜索 `"<球队名> 2026 战术 风格"` 或 `"<team> 2026 tactics formation style"`，使用赛季常规打法。
   - **③ 状态崩盘球队的特殊处理**：当球队近期战绩很差（如近5轮≤4分，或连败中），必须考虑该队可能**放弃常规打法**，采取以下变阵：
     - 客场崩盘队 → 大概率改踢防反/大巴（`counter_attack` 或 `defensive_deep`），放弃传控
     - 主场崩盘队 → 可能破釜沉舟加强进攻，或收缩防反
     - 判断依据：崩盘程度 + 主客场 + 对手强弱 + 主帅性格
     - 在 `factors[]` 中注明："推测XX因状态崩盘可能放弃常规XX打法，改打XX"

   **区分"真崩盘" vs "状态波动"**（实战经验）：
   - **真崩盘**：垫底队（如光州FC 1胜19轮、-33净胜球），联赛表现全线崩溃 → **必须**推测变阵
     - 即使主帅公开声称要打"压迫"，实际比赛也会被迫收缩——球员信心和能力都不足以执行原战术
   - **状态波动**：中游队3连败但积分安全（如格雷米奥3连败仍居第8） → 不强制推测变阵
     - 主帅如公开表态"坚持体系"，应尊重其战术理念，保留原风格
     - 但需在 `factors[]` 中注明"XX近N场状态低迷"作为风险提示
   - **训练原则**：用联赛排名+积分+净胜球做初始判断，再看近5场状态做微调

   - **预期赛果方向修正**：当推测弱队变阵防反时，同时调整 `expected_pattern` 为 `"cautious"`
   - 在 `factors[]` 中注明所有战术信息的来源（赛前报道/赛季数据/推测）
   - 如果完全搜不到信息，默认使用 `hybrid` + `balanced`（此时模型不产生战术修正）

   `compute_elo_delta()` 位于 `scripts/league_data.py`，接受结构化缺席列表：
   ```python
   compute_elo_delta([
       {"role": "star", "count": 1},       # 核心缺阵 -25 Elo
       {"role": "starter", "count": 2},    # 两名首发缺阵各 -10 Elo
   ])
   # 返回约 -38（递减后）
   ```
5. **单场预测默认用驱动模式**（见上方"单场预测驱动模式"）：在临时脚本内存中构造 intelligence 并调用 `cmd_match()`，不写 `intelligence.json`；只有联赛模拟所需的 table/fixtures 快照才写入 `data/live/`。命令行方式（`league_predict.py match ...`）仅在情报需持久化进快照时使用。
6. **如果用户明确要求玄学**，Agent 可收集比赛信息，按上下文保存起卦背景。
7. 按 `references/html_report.md` 由 Agent 直接生成自包含 HTML 报告，保存到 `reports/<league>/`，并自动打开。
8. **每日复盘（可选但推荐）**：若用户在"每日复盘"模式，预测时给单场加 `--record`（或驱动模式里调 `ledger.append_entry`）落台账；次日运行 `python3 scripts/daily_review.py review` 复盘昨日预测 vs 实际结果，样本足够后按需运行 `python3 scripts/daily_review.py tune` 自动调参（详见 `references/daily_review.md`）。

## 联赛配置

| 联赛 | 代码 | 球队数 | 轮次 | 赛季 |
|------|------|--------|------|------|
| 挪威超级联赛 | eliteserien | 16 | 30 | 4月-11月 |
| 瑞典超级联赛 | allsvenskan | 16 | 30 | 3月-11月 |
| 美国职业大联盟 | mls | 30 | 34 | 2月-10月 |
| 巴西足球甲级联赛 | brasileirao | 20 | 38 | 4月-12月 |
| 韩国K1联赛 | kleague | 12 | 38 | 3月-11月 |
| 欧足联欧洲联赛 | europa_league | 36 | 8(联赛阶段) | 8月-5月 |
| 欧洲冠军联赛 | champions_league | 32 | 8(联赛阶段) | 9月-6月 |
| 欧冠资格赛 | ucl_qualifying | 30 | 4(资格赛) | 7月-8月 |
| 日本J1联赛 | jleague | 20 | 38 | 2月-12月 |
| 南美解放者杯 | libertadores | 16 | 4(淘汰赛) | 2月-11月 |
| 法国足球乙级联赛 | ligue2 | 20 | 38 | 8月-5月 |
| 西班牙足球甲级联赛 | laliga | 20 | 38 | 8月-5月 |
| 荷兰足球甲级联赛 | eredivisie | 18 | 34 | 8月-5月 |
| 英格兰足球超级联赛 | epl | 20 | 38 | 8月-5月 |
| 法国足球甲级联赛 | ligue1 | 18 | 34 | 8月-5月 |

### 联赛特殊因子

**挪超**: 人工草皮惩罚(-0.12 xG)、欧战双线消耗(-0.08)、北方球队主场优势(+0.06)、夏季高进球(+0.15)

**瑞超**: 春季场地偏硬(-0.05)、欧战消耗(-0.06)、斯德哥尔摩德比加成(+0.10)、哥德堡德比加成(+0.08)

**MLS**: 跨东西海岸旅行消耗(-0.10)、人工草皮惩罚(-0.10)、季后赛冲刺加成(+0.06)、夏窗新援(+0.05)、国际比赛日减员(-0.06)

**巴甲**: 跨区域旅行消耗(-0.08)、东北部高温高湿(+0.06 主队)、高海拔优势(+0.05)、解放者杯消耗(-0.08)、人工草皮惩罚(-0.10)

**欧联**: 跨国旅行消耗(-0.08)、周四紧凑赛程(-0.06)、南北欧天气差异(-0.05)

**欧冠**: 跨国旅行消耗(-0.08)、周中+周末紧凑赛程(-0.06)、南北欧天气差异(-0.05)

**欧冠资格赛**: 跨国旅行消耗(-0.08)、周中+周末紧凑赛程(-0.06)、南北欧天气差异(-0.05)

**解放者杯**: 南美跨国旅行消耗(-0.08)、周中紧凑赛程/联赛双线(-0.06)、基多/高原主场等逐场用驱动模式手动体现

**韩职**: 夏季高温高湿(-0.04 xG 双方)、亚冠双线消耗(-0.08)、金泉尚武军旅体制(-0.05)

**日职**: 夏季高温高湿(-0.04 xG 双方)、亚冠(亚精英赛)双线消耗(-0.08)

**西甲**: 8-9月西班牙高温(-0.04 xG 双方)、欧冠/欧联双线消耗(-0.08)

**荷甲**: 欧冠/欧联/欧协双线消耗(-0.08)

**英超**: 欧冠/欧联双线消耗(-0.08)、圣诞快车/周中双赛体能消耗(逐场驱动模式体现)

**法甲**: 欧冠/欧联双线消耗(-0.08)

### 通用附加因子（所有联赛生效，由 Agent 情报驱动）
| 因子 | 数据来源 | 影响范围 | 说明 |
|------|---------|---------|------|
| **近期状态** | intelligence.json `teams.<code>.form` | ±0.10 xG | 近5场字符串量化，最新权重更高 |
| **交手熟悉度** | results.json 中两队已赛次数 | 弱队 +0.04 / 强队 -0.03 xG | ≥3次触发，弱队战术准备获益 |
| **战术反制** | intelligence.json `matches[].tactical` | 弱队 +0.06 / 强队 -0.05 xG | Agent 确认有明显克制时标注 |

### 战术对位因子（Tactical Matchup Factors）

战术对位因子通过 `intelligence.json` 的 `matches[].tactical` 中的扩展字段生效，由 `compute_tactical_delta()` 函数自动计算并叠加到 xG。

| 因子 | 影响幅度 | 触发条件 |
|------|---------|---------|
| **战术风格克制** | ±0.08~0.25 xG | 两队风格存在明确克制关系查克制矩阵 |
| **对攻/保守预期** | +0.20 / -0.15 xG | `expected_pattern` 为 `open` 或 `cautious` |
| **无效控球惩罚** | -0.15~0.35 xG | 传控/混合型队面对大巴或反击型队 |
| **身体对抗差异** | ±0.12~0.24 xG | `physical_mismatch` 非 0，每级 ±0.12 xG |
| **德比/特殊战意** | 主队 +0.18 / 客队 +0.08 xG | `derby_boost=true` |
| **战意因素** | 每队 ±0.15~0.30 xG | `fighting_spirit` 显式标注（等级见下），预测权重最高的因子 |
| **单队总修正上限** | ±0.35 xG（软截断） | 超过 ±0.25 部分对数衰减 |

**战意因素（fighting_spirit）**：`matches[].tactical.fighting_spirit` 按队显式标注（A=主队/对阵第一队，B=客队/对阵第二队），是预测中最重要的人为因子：

| level | 含义 | xG |
|-------|------|:--:|
| `desperate` | 背水一战（保级生死、总比分落后、末轮定生死） | **+0.30** |
| `high` | 战意高涨（争冠、欧战资格、德比、复仇、新帅首秀） | **+0.15** |
| `normal` | 正常（默认） | 0 |
| `low` | 战意偏低（已达标、保级无忧、双线保留） | **-0.15** |
| `dead_rubber` | 无欲无求/放弃（赛季末无意义、全力备战更关键赛事） | **-0.30** |

**防重复计分**：同一战意来源只用一条通道——
- 德比氛围已用 `derby_boost=true` 表达时，不要再把该队 `fighting_spirit` 设为 `high`（除非有独立战意来源）。
- 保级战/争冠/副班长情境已被 `classify_match()` 按积分榜自动加成 → 默认写 `normal` 或省略，仅在有**额外**明确战意依据（复仇、新帅效应、摆烂轮换等）时才显式标注。

**战术风格枚举**：`high_press`(高位逼抢) / `possession`(传控) / `counter_attack`(反击) / `direct`(长传) / `defensive_deep`(大巴) / `physical`(身体) / `hybrid`(混合)

**克制矩阵摘要**：
- 高位逼抢克制传控 (+0.20)，但怕反击 (-0.25) 和破不了大巴 (-0.15)
- 传控克制大巴 (+0.15)，但怕反击 (-0.20) 和高位逼抢 (-0.20)
- 反击克制高位逼抢 (+0.25) 和传控 (+0.20)，但怕大巴 (-0.10)
- 长传克制传控 (+0.15) 和逼抢 (+0.10)
- 身体对抗克制传控 (+0.15)，但怕反击 (-0.08) 和逼抢 (-0.10)
- 大巴克制逼抢 (+0.10) 和反击 (+0.10)，但怕传控 (-0.15)

**数据来源要求**：Agent 必须基于赛前分析、历史交锋、统计数据判定风格，注明来源。禁止凭空猜测。详见 `references/intelligence.md`。

**向后兼容**：当 `tactical` 字段缺失或为空时，所有战术因子返回 0.0 修正，不影响现有预测。

## 重要约束
- 只说**概率**，不说"必胜/一定"；区分"理性"与"玄学"两类来源。
- 单场输出必须突出概率最高的赛果方向作为主预测结果。
- 联赛积分榜预测包含争冠、欧战资格、降级三条主线。
- 明确足球单场随机性很高；强队高概率不等于稳胜。
- `references/<league>/teams.json` 是可编辑离线样例快照。
- 赛前情报是 Agent 采集后的有界修正，不是确定性结论。
- 如果无法获得足够新的赛前情报，要明确说明数据不足。
- **战术情报是必填项**：每次单场预测前必须搜索两队战术风格并填入 `matches[].tactical`。如果搜索不到，使用 `hybrid` + `balanced` 并注明"战术数据不足"。
- 玄学起卦背景只用于娱乐语境，不得包装成真实预测依据。
- HTML 报告必须包含预测原因、情报摘要等完整内容。
- 数据锁定：`data/live/<league>/results.json` 中已结束比赛会被锁定，模拟时用真实比分代替。

## 参考文档（按需阅读）
- `references/model.md` — 理性模型数学原理与联赛适配
- `references/html_report.md` — HTML 报告内容结构与视觉要求
- `references/driver_predict.md` — 单场预测驱动模式（In-Memory Driver）模板与字段速查
- `references/daily_review.md` — 每日复盘 + 自动调参（预测台账、指标、门槛与回滚）
- `references/hexagrams.json` — 六十四卦数据（玄学用）
- `references/league_config.json` — 联赛配置总表
- `references/<league>/teams.json` — 各联赛球队评级数据
- `references/<league>/context.json` — 各联赛特殊因子
