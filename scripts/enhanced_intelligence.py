#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
增强版情报处理模块
处理历史交锋、关键球员、特殊比赛因素等
"""

import json
import os
import re
from typing import Dict, List, Tuple, Optional

# 常量
MAX_ELO_DELTA = 30.0
MAX_GOAL_DELTA = 0.35
MAX_KEY_PLAYER_IMPACT = 0.15  # 关键球员最大影响
MAX_H2H_IMPACT = 0.10  # 历史交锋最大影响
MAX_SPECIAL_FACTORS_IMPACT = 0.20  # 特殊因素最大影响

def clamp(value: float, low: float, high: float) -> float:
    """限制值在范围内"""
    return max(low, min(high, value))

def as_float(value, default=0.0) -> float:
    """安全转换为浮点数"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

class EnhancedIntelligenceProcessor:
    """增强版情报处理器"""
    
    def __init__(self, intelligence_data: Dict):
        self.intelligence = intelligence_data
        self.factors = []
        self.notes = []
        
    def process_all_factors(self, home_team: str, away_team: str) -> Dict:
        """处理所有情报因素"""
        result = {
            "elo_delta": {home_team: 0.0, away_team: 0.0},
            "goal_delta": {home_team: 0.0, away_team: 0.0},
            "form_delta": {home_team: 0.0, away_team: 0.0},
            "key_player_impact": {home_team: 0.0, away_team: 0.0},
            "h2h_impact": {home_team: 0.0, away_team: 0.0},
            "special_factors_impact": {home_team: 0.0, away_team: 0.0},
            "factors": [],
            "notes": []
        }
        
        # 1. 处理基础情报
        self._process_basic_intelligence(result, home_team, away_team)
        
        # 2. 处理历史交锋
        self._process_h2h_data(result, home_team, away_team)
        
        # 3. 处理关键球员影响
        self._process_key_players(result, home_team, away_team)
        
        # 4. 处理特殊比赛因素
        self._process_special_factors(result, home_team, away_team)
        
        # 5. 计算综合影响
        self._calculate_combined_impact(result, home_team, away_team)
        
        return result
    
    def _process_basic_intelligence(self, result: Dict, home: str, away: str):
        """处理基础情报（状态、Elo修正等）"""
        teams_data = self.intelligence.get("teams", {})
        
        for team_code in [home, away]:
            team_data = teams_data.get(team_code, {})
            if not isinstance(team_data, dict):
                continue
            
            # 处理Elo修正
            elo_delta = as_float(team_data.get("elo_delta", 0.0))
            confidence = clamp(as_float(team_data.get("confidence", 1.0)), 0.0, 1.0)
            result["elo_delta"][team_code] = clamp(elo_delta, -MAX_ELO_DELTA, MAX_ELO_DELTA) * confidence
            
            # 处理状态
            form_str = team_data.get("form", "")
            if form_str:
                form_delta = self._calculate_form_impact(form_str)
                result["form_delta"][team_code] = form_delta
                result["factors"].append(f"{team_code} 近期状态: {form_str}")
                result["notes"].append(f"{team_code} 状态修正: {form_delta:+.2f}")
    
    def _calculate_form_impact(self, form_str: str) -> float:
        """计算状态影响"""
        if not form_str:
            return 0.0
        
        # 解析状态字符串（如 "胜-平-负-胜-平"）
        results = form_str.split("-")
        if not results:
            return 0.0
        
        # 计算胜率
        wins = results.count("胜")
        draws = results.count("平")
        total = len(results)
        
        if total == 0:
            return 0.0
        
        win_rate = wins / total
        draw_rate = draws / total
        
        # 状态修正：胜率50%为基准，高于50%为正，低于50%为负
        form_impact = (win_rate - 0.5) * 0.2  # 最大±0.1
        
        # 连胜/连败加成
        if len(results) >= 3:
            last_three = results[-3:]
            if all(r == "胜" for r in last_three):
                form_impact += 0.05  # 连胜加成
            elif all(r == "负" for r in last_three):
                form_impact -= 0.05  # 连败惩罚
        
        return clamp(form_impact, -0.15, 0.15)
    
    def _process_h2h_data(self, result: Dict, home: str, away: str):
        """处理历史交锋数据（稳健解析：支持数字、字符串描述、冲突标注等多种格式）"""
        h2h_data = self.intelligence.get("h2h", {})
        if not h2h_data:
            return

        total_matches = h2h_data.get("total_matches", 0)

        # 提取数字：从 int/float/字符串中解析出总场次
        parsed_total = None
        if isinstance(total_matches, (int, float)):
            parsed_total = float(total_matches)
        elif isinstance(total_matches, str):
            # 优先匹配明确的数字（如 "15"、"共15场"）
            m = re.search(r"\d+", total_matches)
            if m:
                parsed_total = float(m.group())

        if parsed_total is None or parsed_total <= 0:
            # 无法解析总场次（纯文字描述/数据冲突）→ 使用保守估计
            result["h2h_impact"][home] = -0.02
            result["h2h_impact"][away] = 0.02
            desc = h2h_data.get("notes") or h2h_data.get("description") or str(total_matches)[:40]
            result["factors"].append(f"历史交锋(文字描述，保守估计): {desc}")
            result["notes"].append("历史交锋: 总场次无法解析，影响减小")
            return

        total_matches = parsed_total
        home_wins = h2h_data.get("home_wins", 0)
        away_wins = h2h_data.get("away_wins", 0)
        draws = h2h_data.get("draws", 0)

        # 防御：胜平负之和大于总场次时按比例归一
        try:
            total_recorded = float(home_wins) + float(away_wins) + float(draws)
        except (TypeError, ValueError):
            total_recorded = 0.0
        if total_recorded <= 0 or total_recorded > total_matches:
            if total_recorded <= 0:
                result["factors"].append("历史交锋: 胜平负明细缺失，使用保守估计")
                result["notes"].append("历史交锋: 明细缺失，影响减小")
                return
            scale = total_matches / total_recorded
            home_wins = float(home_wins) * scale
            away_wins = float(away_wins) * scale
            draws = float(draws) * scale

        home_win_rate = home_wins / total_matches
        away_win_rate = away_wins / total_matches

        # 历史交锋影响：胜率差影响
        h2h_diff = home_win_rate - away_win_rate
        h2h_impact = h2h_diff * 0.1  # 最大±0.1

        result["h2h_impact"][home] = clamp(h2h_impact, -MAX_H2H_IMPACT, MAX_H2H_IMPACT)
        result["h2h_impact"][away] = clamp(-h2h_impact, -MAX_H2H_IMPACT, MAX_H2H_IMPACT)

        result["factors"].append(f"历史交锋: {home} {home_wins:.0f}胜{draws:.0f}平{away_wins:.0f}负")
        result["notes"].append(f"历史交锋影响: {home} {h2h_impact:+.3f}, {away} {-h2h_impact:+.3f}")
    
    def _process_key_players(self, result: Dict, home: str, away: str):
        """处理关键球员影响"""
        teams_data = self.intelligence.get("teams", {})
        
        for team_code in [home, away]:
            team_data = teams_data.get(team_code, {})
            if not isinstance(team_data, dict):
                continue
            
            key_players = team_data.get("key_players", [])
            if not key_players:
                continue
            
            # 计算关键球员影响
            player_impact = 0.0
            player_names = []
            
            for player in key_players:
                if not isinstance(player, dict):
                    continue
                
                status = player.get("status", "available")
                impact_desc = player.get("impact", "")
                position = player.get("position", "")
                
                # 根据状态调整影响
                if status == "available":
                    # 根据位置和描述计算影响
                    if "射手王" in impact_desc or "核心" in impact_desc:
                        player_impact += 0.08
                    elif "组织核心" in impact_desc:
                        player_impact += 0.05
                    elif "状态火热" in impact_desc:
                        player_impact += 0.06
                    else:
                        player_impact += 0.03
                    
                    player_names.append(player.get("name", "未知"))
                elif status == "injured":
                    player_impact -= 0.05
                    player_names.append(f"{player.get('name', '未知')}(伤缺)")
            
            # 限制影响范围
            player_impact = clamp(player_impact, -MAX_KEY_PLAYER_IMPACT, MAX_KEY_PLAYER_IMPACT)
            
            result["key_player_impact"][team_code] = player_impact
            
            if player_names:
                result["factors"].append(f"{team_code} 关键球员: {', '.join(player_names[:3])}")
                result["notes"].append(f"{team_code} 关键球员影响: {player_impact:+.3f}")
    
    def _process_special_factors(self, result: Dict, home: str, away: str):
        """处理特殊比赛因素"""
        factors_data = self.intelligence.get("factors", [])
        
        home_impact = 0.0
        away_impact = 0.0
        
        for factor in factors_data:
            if not isinstance(factor, dict):
                continue
            
            factor_type = factor.get("type", "")
            impact = as_float(factor.get("impact", 0.0))
            confidence = clamp(as_float(factor.get("confidence", 1.0)), 0.0, 1.0)
            
            # 根据因素类型分配影响
            if "home" in factor_type.lower() or "主场" in factor_type:
                home_impact += impact * confidence
                result["factors"].append(f"主场因素: {factor.get('description', '')}")
            elif "away" in factor_type.lower() or "客场" in factor_type:
                away_impact += impact * confidence
                result["factors"].append(f"客场因素: {factor.get('description', '')}")
            elif "心理" in factor_type or "交锋" in factor_type:
                # 心理因素影响双方
                home_impact += impact * confidence * 0.5
                away_impact += impact * confidence * 0.5
                result["factors"].append(f"心理因素: {factor.get('description', '')}")
            elif "保级" in factor_type or "争冠" in factor_type:
                # 特殊比赛因素影响双方
                home_impact += impact * confidence * 0.3
                away_impact += impact * confidence * 0.3
                result["factors"].append(f"比赛因素: {factor.get('description', '')}")
        
        # 限制影响范围
        result["special_factors_impact"][home] = clamp(home_impact, -MAX_SPECIAL_FACTORS_IMPACT, MAX_SPECIAL_FACTORS_IMPACT)
        result["special_factors_impact"][away] = clamp(away_impact, -MAX_SPECIAL_FACTORS_IMPACT, MAX_SPECIAL_FACTORS_IMPACT)
        
        if factors_data:
            result["notes"].append(f"特殊因素影响: {home} {home_impact:+.3f}, {away} {away_impact:+.3f}")
    
    def _calculate_combined_impact(self, result: Dict, home: str, away: str):
        """计算综合影响"""
        # 计算总影响
        home_total = (
            result["elo_delta"][home] / 100 +  # Elo修正转换为进球影响
            result["form_delta"][home] +
            result["key_player_impact"][home] +
            result["h2h_impact"][home] +
            result["special_factors_impact"][home]
        )
        
        away_total = (
            result["elo_delta"][away] / 100 +
            result["form_delta"][away] +
            result["key_player_impact"][away] +
            result["h2h_impact"][away] +
            result["special_factors_impact"][away]
        )
        
        # 转换为进球修正
        result["goal_delta"][home] = clamp(home_total, -MAX_GOAL_DELTA, MAX_GOAL_DELTA)
        result["goal_delta"][away] = clamp(away_total, -MAX_GOAL_DELTA, MAX_GOAL_DELTA)
        
        # 添加综合影响说明
        result["notes"].append(f"综合影响: {home} {home_total:+.3f}, {away} {away_total:+.3f}")
        result["notes"].append(f"进球修正: {home} {result['goal_delta'][home]:+.3f}, {away} {result['goal_delta'][away]:+.3f}")

def load_enhanced_intelligence(league: str, home: str, away: str) -> Dict:
    """加载增强版情报数据"""
    base_path = os.path.join(os.path.dirname(__file__), "..", "data", "live", league)
    
    # 尝试加载特定比赛的情报文件
    specific_file = os.path.join(base_path, f"intelligence_{home}_{away}.json")
    if os.path.exists(specific_file):
        with open(specific_file, encoding="utf-8") as f:
            return json.load(f)
    
    # 尝试加载默认情报文件
    default_file = os.path.join(base_path, "intelligence.json")
    if os.path.exists(default_file):
        with open(default_file, encoding="utf-8") as f:
            return json.load(f)
    
    return {}

def process_enhanced_intelligence(league: str, home: str, away: str) -> Dict:
    """处理增强版情报数据"""
    intelligence_data = load_enhanced_intelligence(league, home, away)
    
    if not intelligence_data:
        return {
            "elo_delta": {home: 0.0, away: 0.0},
            "goal_delta": {home: 0.0, away: 0.0},
            "form_delta": {home: 0.0, away: 0.0},
            "factors": [],
            "notes": []
        }
    
    processor = EnhancedIntelligenceProcessor(intelligence_data)
    return processor.process_all_factors(home, away)