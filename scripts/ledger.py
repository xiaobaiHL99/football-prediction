#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
预测台账 — data/reviews/predictions.json 的读写与结果匹配。

每日复盘闭环的数据地基：
- append_entry(): 预测时自动追加一条结构化记录（date / league / 双方 / 预测概率 / 模型输入快照）。
- match_results(): 复盘时按 {home, away} 主客对把台账条目与 results.json 对齐，每条目消费一次
  （避免两回合/双循环里同一对反复匹配）。
- 只写 data/reviews/，绝不改动 data/live/ 既有快照。
"""

import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "data", "reviews")
LEDGER_PATH = os.path.join(DATA_DIR, "predictions.json")


def ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_ledger(path: str = None) -> dict:
    """加载台账。文件不存在或损坏时返回空台账。"""
    path = path or LEDGER_PATH
    ensure_dir()
    if not os.path.exists(path):
        return {"_meta": {"updated_at": None}, "predictions": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {}
    if not isinstance(data, dict) or "predictions" not in data:
        data = {"_meta": {"updated_at": None}, "predictions": list(data) if isinstance(data, list) else []}
    if isinstance(data["predictions"], dict):
        # 兼容旧版 dict 形态：摊平为列表
        data["predictions"] = list(data["predictions"].values())
    return data


def append_entry(entry: dict, path: str = None) -> dict:
    """Write one prediction per fixture date, replacing an older same-fixture version."""
    path = path or LEDGER_PATH
    ensure_dir()
    ledger = load_ledger(path)
    entry["_id"] = entry.get("_id") or make_id(entry)
    existing = ledger["predictions"]
    replacement_index = next((
        index for index, item in enumerate(existing)
        if item.get("_id") == entry["_id"]
    ), None)
    if replacement_index is None:
        existing.append(entry)
    else:
        existing[replacement_index] = entry
        # Remove legacy duplicates while preserving the replacement position.
        ledger["predictions"] = [
            item for index, item in enumerate(existing)
            if item.get("_id") != entry["_id"] or index == replacement_index
        ]
    ledger["_meta"] = ledger.get("_meta", {}) or {}
    from datetime import datetime
    ledger["_meta"]["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)
    return entry


def make_id(entry: dict) -> str:
    """基于比赛信息生成稳定 id：league|home|away|date（同场重复预测会得到相同 id，复盘时按序消费）。"""
    return "|".join([
        str(entry.get("league", "")),
        str(entry.get("home", "")).upper(),
        str(entry.get("away", "")).upper(),
        str(entry.get("date", "")),
    ])


def match_results(entries: list, results_data: dict):
    """
    把台账条目与 results.json 按 {home, away} 主客对对齐，每条目消费一次。

    Parameters
    ----------
    entries : list[dict] — 台账条目（建议按日期/顺序传入）
    results_data : dict — load_results_data(league) 的结果：{"matches": [{"home","away","hg","ag"}]}

    Returns
    -------
    list[tuple] — [(entry, result_or_None), ...]，result 为匹配到的赛果 dict 或 None。
    """
    matches = results_data.get("matches", []) if results_data else []
    used = [False] * len(matches)
    out = []
    for e in entries:
        home = str(e.get("home", "")).upper()
        away = str(e.get("away", "")).upper()
        found = None
        for idx, m in enumerate(matches):
            if used[idx]:
                continue
            if str(m.get("home", "")).upper() == home and str(m.get("away", "")).upper() == away:
                found = m
                used[idx] = True
                break
        out.append((e, found))
    return out


def filter_by_date(entries: list, date: str, league: str = None) -> list:
    """按比赛日期（可加联赛过滤）筛台账条目。date 为空时返回全部。"""
    out = []
    for e in entries:
        if date and e.get("date") != date:
            continue
        if league and str(e.get("league", "")).upper() != str(league).upper():
            continue
        out.append(e)
    return out
