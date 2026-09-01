# 每日复盘 + 自动调参

## 目标

把"预测 → 结果"的闭环跑起来：每次预测自动落台账，每天复盘昨日预测 vs 实际结果，攒够样本后自动微调模型参数。这不是自由文档，而是结构化 JSON 台账驱动的自动系统。

```
预测(带 --record) → data/reviews/predictions.json   ← 台账
                       │
次日 daily_review.py review ── 对账 results.json → 指标 → reports/reviews/<date>-review.md + reviews.json
                       │
样本足够 daily_review.py tune ── 坐标下降 → league_config.json(带备份) / model_overrides.json → tuning_log.json
```

## 数据位置

| 文件 | 作用 |
|------|------|
| `data/reviews/predictions.json` | 预测台账（每次预测追加一条，含预测概率、比分分布、模型输入快照） |
| `data/reviews/reviews.json` | 每日复盘摘要（趋势用，最近 N 天走势） |
| `data/reviews/tuning_log.json` | 每次调参变更记录（before / after / 改善量） |
| `reports/reviews/<date>-review.md` | 当日复盘报告 |
| `references/league_config.backup.json` | 调参写回前的整表备份（首次写回时生成） |
| `references/model_overrides.json` | 全局因子覆盖（战意/状态幅度），导入时自动应用 |

## 每日循环（Agent 工作流）

1. **预测时落台账**
   - 命令行：`python3 scripts/league_predict.py match A B --league X --date YYYY-MM-DD --record`
   - 驱动模式：临时脚本里 `lp.build_match_entry(...)` 得到条目 dict，再 `ledger.append_entry(entry)`。
   - `--date` 填比赛日期；不填则记今天。台账条目同时保存 `intelligence_snapshot`，可完整复跑。
2. **次日复盘**
   - `python3 scripts/daily_review.py review`（默认复盘昨天）或 `--date` 指定、`--league` 过滤。
   - 输出：方向命中率、比分命中率、平均 log-loss、平均 Brier；逐场明细 + 按联赛聚合 + 趋势。
3. **自动调参（样本够了才做）**
   - `python3 scripts/daily_review.py tune --dry-run`（只建议不写回）
   - `python3 scripts/daily_review.py tune`（写回）
   - 未达到门槛会明确提示"样本不足，继续积累"。

## 指标定义

| 指标 | 公式 | 含义 |
|------|------|------|
| 方向命中 | 预测方向 == 实际方向（主胜/平/客胜） | 最直观的"猜对了吗" |
| 比分命中 | 主预测比分 == 实际比分 | 难度高，命中率通常低 |
| log-loss | `-ln(p_实际赛果)` | 校准好坏：1.0≈随机猜，越低越好 |
| Brier | `Σ(p_k - y_k)²`（y 为实际赛果 one-hot） | 概率校准：0.667≈随机，越低越好 |

实际方向由 `{home, away, hg, ag}` 判定。`results.json` 无日期，复盘按 `{home, away}` 主客对匹配、**每条目消费一次**（防止双循环/两回合里同一对反复匹配）。

## 自动调参（tune）

### 调什么
- **每联赛参数**（`references/league_config.json`）：`avg_goals` / `home_adv` / `probability_shrink` / `shock_sd`。
- **全局因子**（跨联赛共享，写 `references/model_overrides.json`）：`SPIRIT_XG_STEP`（战意幅度）、`FORM_XG_MAX`（状态幅度）。

### 门槛（防过拟合）
- 每联赛已匹配到结果的台账 ≥ **30** 条才调该联赛参数。
- 全局因子跨联赛合计 ≥ **50** 条才调。

### 方法
- 坐标下降：逐参数在其附近尝试候选值，选训练集 log-loss 更优者。
- 时间序列切分：按日期排序，老样本（80%）训练 / 最新样本（20%）验证。
- 参数值 clamp 在合理区间（如 `avg_goals` ±7%、`probability_shrink` 0.02~0.30）。

### 写回规则（防过拟合）
- 仅当**验证集** log-loss 改善 ≥ 0.01 才写回；否则打印"改善不足，不写回"。
- 写回 `league_config.json` 前若不存在备份，自动生成 `league_config.backup.json`；每次变更记入 `tuning_log.json`。

### 回滚
```bash
# 每联赛参数回滚
cp references/league_config.backup.json references/league_config.json
# 全局因子回滚：直接删掉覆盖文件即恢复默认常量
rm references/model_overrides.json
```

## 驱动模式接入

驱动脚本中落台账：
```python
from ledger import append_entry
entry = lp.build_match_entry(teams, a, b, league, intelligence, {}, False, {}, results_data, date)
append_entry(entry)
```

## 期望管理

系统从启用 `--record` 起开始积累样本。刚开始时 `tune` 必然报"样本不足"——属正常；坚持每天预测 + 复盘，约 2~4 周后单联赛样本够 30 条，调参才有统计意义。全局因子（战意/状态幅度）需要更多样本，且只在有战意/状态标注的比赛里起作用。

## 与"学习"的关系

RAG 检索不是模型学习——数值泊松模型无法直接消费自由文本。真正的"学习"路径是：台账 → 校准/回测（用历史预测 vs 实际结果量化 log-loss 并微调参数）→ 后续可扩展 kNN 相似比赛先验。台账正是这条路径的数据地基。
