#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SportMonks Football API v3 客户端

提供实时数据抓取：赛程、比分、积分榜、赔率、xG、球队/球员查询。
供 league_predict.py / daily_review.py 等模块调用。

环境变量：SPORTMONKS_API_TOKEN

Usage:
  from sportmonks_client import SportMonksClient
  client = SportMonksClient()
  fixtures = client.fixtures_by_date("2026-08-31")
  standings = client.standings_by_season(23626)
"""

import json
import os
import sys
import time
from datetime import datetime, date
from typing import Optional, Dict, List, Any
from urllib.request import urlopen, Request
from urllib.parse import urlencode, quote
from urllib.error import HTTPError, URLError

# ================================================================
# 配置
# ================================================================

API_TOKEN = os.environ.get("SPORTMONKS_API_TOKEN", "")
BASE_URL = "https://api.sportmonks.com/v3/football"
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache", "sportmonks")
CACHE_TTL_SECONDS = 300  # 5 分钟缓存

# 常用联赛 ID（SportMonks）
LEAGUE_IDS = {
    "epl": 8,              # Premier League
    "laliga": 564,         # La Liga
    "bundesliga": 82,      # Bundesliga
    "seriea": 384,         # Serie A
    "ligue1": 301,         # Ligue 1
    "eredivisie": 72,      # Eredivisie
    "champions_league": 2, # UEFA Champions League
    "europa_league": 5,    # UEFA Europa League
}

# 常用市场 ID（SportMonks）
MARKET_IDS = {
    "1x2": 1,
    "handicap": 2,
    "over_under": 3,
    "btts": 8,
    "correct_score": 9,
    "ht_ft": 10,
}


# ================================================================
# 客户端类
# ================================================================

class SportMonksClient:
    """SportMonks Football API v3 客户端"""

    def __init__(self, token: str = "", cache_ttl: int = CACHE_TTL_SECONDS):
        self.token = token or API_TOKEN
        self.cache_ttl = cache_ttl
        if not self.token:
            raise ValueError("SPORTMONKS_API_TOKEN 未设置。请设置环境变量或传入 token 参数。")
        os.makedirs(CACHE_DIR, exist_ok=True)

    # ── 底层请求 ──

    def _cache_path(self, key: str) -> str:
        safe = key.replace("/", "_").replace("?", "_").replace("&", "_")[:200]
        return os.path.join(CACHE_DIR, f"{safe}.json")

    def _get(self, path: str, params: Optional[Dict] = None, use_cache: bool = True) -> Dict:
        """发送 GET 请求，带本地文件缓存"""
        params = params or {}
        # 构建缓存 key
        full_key = f"{path}?{urlencode(sorted(params.items()))}"
        cache_file = self._cache_path(full_key)

        # 检查缓存
        if use_cache and os.path.exists(cache_file):
            age = time.time() - os.path.getmtime(cache_file)
            if age < self.cache_ttl:
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)

        # 构建 URL（path 需要 URL 编码）
        url_params = {"api_token": self.token}
        url_params.update(params)
        encoded_path = quote(path, safe="/")
        url = f"{BASE_URL}{encoded_path}?{urlencode(url_params)}"

        # 发送请求
        try:
            req = Request(url, headers={"Accept": "application/json"})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"SportMonks API {e.code}: {body}")
        except URLError as e:
            raise RuntimeError(f"SportMonks 网络错误: {e.reason}")

        # 写入缓存
        if use_cache:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

        return data

    def _paginate(self, path: str, params: Optional[Dict] = None, max_pages: int = 5) -> List[Dict]:
        """自动分页获取全部数据"""
        all_data = []
        params = params or {}
        for page in range(1, max_pages + 1):
            params["page"] = page
            resp = self._get(path, params)
            items = resp.get("data", [])
            if not items:
                break
            all_data.extend(items)
            # 检查是否有下一页
            pagination = resp.get("pagination", {})
            if pagination.get("current_page", 0) >= pagination.get("total_pages", 0):
                break
        return all_data

    # ── 实时比分 ──

    def livescores(self, include: str = "") -> List[Dict]:
        """获取当前进行中的比赛"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get("/livescores/now", params, use_cache=False)
        return resp.get("data", [])

    # ── 赛程 ──

    def fixtures_by_date(self, date_str: str, include: str = "") -> List[Dict]:
        """获取指定日期的赛程（YYYY-MM-DD）"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/fixtures/date/{date_str}", params)
        return resp.get("data", [])

    def fixture_by_id(self, fixture_id: int, include: str = "") -> Dict:
        """获取单场比赛详情"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/fixtures/{fixture_id}", params)
        return resp.get("data", {})

    def fixtures_by_ids(self, ids: List[int], include: str = "") -> List[Dict]:
        """批量获取比赛详情"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/fixtures/multi/{','.join(map(str, ids))}", params)
        return resp.get("data", [])

    # ── 联赛 & 赛季 ──

    def leagues(self, include: str = "") -> List[Dict]:
        """获取所有联赛"""
        params = {}
        if include:
            params["include"] = include
        return self._paginate("/leagues", params)

    def search_leagues(self, name: str, include: str = "") -> List[Dict]:
        """搜索联赛"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/leagues/search/{name}", params)
        return resp.get("data", [])

    def season_by_id(self, season_id: int, include: str = "") -> Dict:
        """获取赛季详情"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/seasons/{season_id}", params)
        return resp.get("data", {})

    # ── 积分榜 ──

    def standings_by_season(self, season_id: int, include: str = "") -> List[Dict]:
        """获取赛季积分榜"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/standings/seasons/{season_id}", params)
        return resp.get("data", [])

    def standings_by_round(self, round_id: int, include: str = "") -> List[Dict]:
        """获取某轮积分榜"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/standings/rounds/{round_id}", params)
        return resp.get("data", [])

    # ── 球队 ──

    def team_by_id(self, team_id: int, include: str = "") -> Dict:
        """获取球队详情"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/teams/{team_id}", params)
        return resp.get("data", {})

    def search_teams(self, name: str, include: str = "") -> List[Dict]:
        """搜索球队"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/teams/search/{name}", params)
        return resp.get("data", [])

    # ── 球员 ──

    def player_by_id(self, player_id: int, include: str = "") -> Dict:
        """获取球员详情"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/players/{player_id}", params)
        return resp.get("data", {})

    def search_players(self, name: str, include: str = "") -> List[Dict]:
        """搜索球员"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/players/search/{name}", params)
        return resp.get("data", [])

    # ── 射手榜 ──

    def topscorers_by_season(self, season_id: int, include: str = "") -> List[Dict]:
        """获取赛季射手榜"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/topscorers/seasons/{season_id}", params)
        return resp.get("data", [])

    # ── 赔率 ──

    def odds_by_fixture(self, fixture_id: int, include: str = "") -> List[Dict]:
        """获取比赛赔率"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/odds/pre-match/fixtures/{fixture_id}", params)
        return resp.get("data", [])

    def odds_by_market(self, fixture_id: int, market_id: int, include: str = "") -> List[Dict]:
        """获取指定市场的赔率"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/odds/pre-match/fixtures/{fixture_id}/markets/{market_id}", params)
        return resp.get("data", [])

    def markets(self, include: str = "") -> List[Dict]:
        """获取所有可用市场"""
        params = {}
        if include:
            params["include"] = include
        return self._paginate("/markets", params)

    # ── 预测 ──

    def predictions_by_fixture(self, fixture_id: int, include: str = "") -> Dict:
        """获取比赛预测"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/predictions/probabilities/fixtures/{fixture_id}", params)
        return resp.get("data", {})

    # ── xG ──

    def xg_by_fixture(self, fixture_id: int, include: str = "") -> Dict:
        """获取预期进球数据"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/expected/fixtures/{fixture_id}", params)
        return resp.get("data", {})

    # ── 轮次 ──

    def rounds_by_season(self, season_id: int, include: str = "") -> List[Dict]:
        """获取赛季所有轮次"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/rounds/seasons/{season_id}", params)
        return resp.get("data", [])

    # ── 赛程表 ──

    def schedule_by_season(self, season_id: int, include: str = "") -> List[Dict]:
        """获取赛季完整赛程"""
        params = {}
        if include:
            params["include"] = include
        return self._paginate(f"/schedules/seasons/{season_id}", params)

    # ── 转会 ──

    def transfers_latest(self, include: str = "") -> List[Dict]:
        """获取最新转会"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get("/transfers/latest", params)
        return resp.get("data", [])

    # ── 场馆 ──

    def venue_by_id(self, venue_id: int, include: str = "") -> Dict:
        """获取场馆详情"""
        params = {}
        if include:
            params["include"] = include
        resp = self._get(f"/venues/{venue_id}", params)
        return resp.get("data", {})


# ================================================================
# 便捷函数（供快速调用）
# ================================================================

def get_today_fixtures(league_key: str = "", include: str = "scores,participants,league") -> List[Dict]:
    """获取今天的比赛（可按联赛过滤）"""
    client = SportMonksClient()
    today = date.today().isoformat()
    fixtures = client.fixtures_by_date(today, include)
    if league_key and league_key in LEAGUE_IDS:
        lid = LEAGUE_IDS[league_key]
        fixtures = [f for f in fixtures if f.get("league_id") == lid]
    return fixtures


def get_live_scores(league_key: str = "") -> List[Dict]:
    """获取实时比分"""
    client = SportMonksClient()
    scores = client.livescores("scores,participants,league")
    if league_key and league_key in LEAGUE_IDS:
        lid = LEAGUE_IDS[league_key]
        scores = [s for s in scores if s.get("league_id") == lid]
    return scores


def get_fixture_odds(fixture_id: int, market: str = "1x2") -> List[Dict]:
        """获取指定比赛的赔率"""
        client = SportMonksClient()
        market_id = MARKET_IDS.get(market, 1)
        return client.odds_by_market(fixture_id, market_id, "bookmaker")


# ================================================================
# CLI 测试
# ================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SportMonks API 客户端测试")
    sub = parser.add_subparsers(dest="cmd")

    p_live = sub.add_parser("live", help="实时比分")
    p_live.add_argument("--league", default="")

    p_fix = sub.add_parser("fixtures", help="今日赛程")
    p_fix.add_argument("--date", default=date.today().isoformat())
    p_fix.add_argument("--league", default="")

    p_stand = sub.add_parser("standings", help="积分榜")
    p_stand.add_argument("season_id", type=int)

    p_odds = sub.add_parser("odds", help="比赛赔率")
    p_odds.add_argument("fixture_id", type=int)
    p_odds.add_argument("--market", default="1x2", choices=list(MARKET_IDS.keys()))

    p_search = sub.add_parser("search", help="搜索球队/联赛")
    p_search.add_argument("type", choices=["team", "league", "player"])
    p_search.add_argument("name")

    args = parser.parse_args()

    if args.cmd == "live":
        results = get_live_scores(args.league)
        print(json.dumps(results, indent=2, ensure_ascii=False))
    elif args.cmd == "fixtures":
        client = SportMonksClient()
        fixtures = client.fixtures_by_date(args.date, "scores,participants,league")
        if args.league:
            lid = LEAGUE_IDS.get(args.league, 0)
            fixtures = [f for f in fixtures if f.get("league_id") == lid]
        print(json.dumps(fixtures, indent=2, ensure_ascii=False))
    elif args.cmd == "standings":
        client = SportMonksClient()
        standings = client.standings_by_season(args.season_id, "participant")
        print(json.dumps(standings, indent=2, ensure_ascii=False))
    elif args.cmd == "odds":
        odds = get_fixture_odds(args.fixture_id, args.market)
        print(json.dumps(odds, indent=2, ensure_ascii=False))
    elif args.cmd == "search":
        client = SportMonksClient()
        if args.type == "team":
            results = client.search_teams(args.name)
        elif args.type == "league":
            results = client.search_leagues(args.name)
        else:
            results = client.search_players(args.name)
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        parser.print_help()
