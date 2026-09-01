# 足球预测系统技能

## 概述

这是一个专业的足球比赛预测系统，基于Elo评级和泊松分布模型，支持多联赛分析。系统包含完整的预测、复盘和参数调优功能。

## 主要功能

### 1. 单场比赛预测
- 基于球队Elo评级计算预期进球
- 考虑联赛特殊因子（人工草皮、旅行距离等）
- 支持赛前情报修正（伤停、状态、战术等）
- 生成详细的HTML预测报告

### 2. 联赛模拟
- 蒙特卡洛模拟整个赛季
- 预测最终积分榜排名
- 分析夺冠、降级、欧战资格概率

### 3. 每日复盘系统
- 自动对比预测与实际结果
- 计算方向命中率、比分命中率等指标
- 基于历史数据自动调优模型参数

### 4. 情报分析系统
- 结构化的赛前情报采集
- 支持球员伤停、战术对位、状态分析
- 自动计算Elo和xG修正值

## 支持联赛

| 联赛 | 代码 | 特殊因子 |
|------|------|----------|
| 挪超 | eliteserien | 人工草皮、欧战消耗、北方主场 |
| 瑞超 | allsvenskan | 春季场地、德比加成 |
| MLS | mls | 旅行距离、人工草皮、季后赛冲刺 |
| 韩职 | kleague | 亚洲风格、赛程密集 |
| 日职 | jleague | 技术流、主场优势 |
| 英超 | epl | 高强度、竞争激烈 |
| 西甲 | laliga | 技术流、控球型 |
| 意甲 | seriea | 防守反击、战术严谨 |
| 德甲 | bundesliga | 高位逼抢、进攻足球 |
| 法甲 | ligue1 | 身体对抗、速度型 |
| 荷甲 | eredivisie | 青训体系、进攻足球 |
| 巴甲 | brasileirao | 技术流、主场优势 |
| 芬超 | veikkausliiga | 人工草皮、北方气候 |
| 欧联杯 | europa_league | 跨国对决、赛程密集 |
| 欧冠资格赛 | ucl_qualifying | 高强度、心理压力 |
| 解放者杯 | libertadores | 南美风格、客场挑战 |

## 使用方法

### 预测单场比赛
```bash
python scripts/league_predict.py match BOD MOL --league eliteserien --date 2026-08-30
```

### 模拟联赛积分榜
```bash
python scripts/league_predict.py table eliteserien --sims 10000
```

### 每日复盘
```bash
python scripts/daily_review.py review --date 2026-08-29
```

### 自动调参
```bash
python scripts/daily_review.py tune --dry-run  # 只建议不写回
python scripts/daily_review.py tune             # 实际写回
```

## 数据结构

```
football-prediction/
├── references/           # 联赛配置和球队数据
│   ├── league_config.json
│   ├── <league>/teams.json
│   └── <league>/context.json
├── data/
│   ├── live/            # 实时数据
│   │   └── <league>/intelligence.json
│   └── reviews/         # 复盘数据
│       ├── predictions.json
│       ├── reviews.json
│       └── tuning_log.json
├── reports/             # 预测报告
│   └── <league>/*.html
├── scripts/             # 核心脚本
│   ├── league_predict.py
│   ├── league_data.py
│   ├── daily_review.py
│   └── ...
└── skill_manifest.json  # 技能配置
```

## 技术特点

1. **科学模型**：基于Elo评级和泊松分布的统计模型
2. **实时修正**：支持赛前情报动态调整预测
3. **自动学习**：通过复盘系统持续优化参数
4. **多联赛支持**：覆盖全球主要足球联赛
5. **详细报告**：生成专业的HTML预测报告

## 注意事项

1. 预测结果仅供参考，足球比赛具有不确定性
2. 情报系统需要基于真实数据，不能凭空猜测
3. 自动调参需要足够的历史样本（建议30+场）
4. 建议每日运行复盘系统以持续改进模型

## 扩展性

系统设计具有良好的扩展性：
- 可轻松添加新联赛
- 支持自定义特殊因子
- 可集成更多数据源
- 支持多种输出格式

---

*本系统由MiMo团队开发，基于先进的统计模型和机器学习技术。*