#!/usr/bin/env python3
"""football MCP → skill 快照同步

Agent 用内置 mcp__football__* 工具拉取原始 JSON（保存为文件）后，用本脚本写入
data/live/<league>/ 快照：table.json（积分榜）、results.json（赛果，合并去重）、
fixtures.json（剩余赛程），并可选运行 elo_updater 刷新评级。

用法示例：
  python scripts/mcp_snapshot.py championship \
      --matches data/live/championship/mcp_raw/matches.json \
      --matches data/live/championship/mcp_raw/fixtures.json \
      --standings data/live/championship/mcp_raw/standings.json \
      --elo

输入 JSON 形态（MCP 原样输出，无需手工改写）：
  get_standings → {"rows": [{"team": "Portsmouth", "played": 3, "won": 1,
                             "drawn": 0, "lost": 2, "goals_for": 5,
                             "goals_against": 6, "goal_difference": -1,
                             "points": 3, "form": "LWL"}]}
  get_matches   → {"matches": [{"home": "...", "away": "...",
                                "score": "2-1", "played": true, "date": "..."}]}
                  （played=true 进 results.json，played=false 进 fixtures.json；
                    可多次传 --matches 合并多个输出文件）

合并规则（results.json）：
  - 按 (home, away, hg, ag) 精确去重，重复运行幂等；
  - 同一对球队已有不同比分（双循环正常现象）→ 默认两条都保留并告警；
  - --fresh 用新数据覆盖该队对的旧条目（仅确认旧数据错误时使用）。
"""

import argparse
import json
import os
import re
import subprocess
import sys

# Windows GBK 控制台无法打印 emoji/中文符号 → 强制 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 已知难匹配别名：查询名归一化 → teams.json 里的规范名（归一化小写）
ALIASES = {
    "wolves": "wolverhampton wanderers",
    "spurs": "tottenham hotspur",
    "man united": "manchester united",
    "man city": "manchester city",
    "newcastle": "newcastle united",
    "brighton": "brighton hove albion",
    "qpr": "queens park rangers",
    "west brom": "west bromwich albion",
    "wba": "west bromwich albion",
    "west ham": "west ham united",
    "sheffield utd": "sheffield united",
    # 各数据源的德甲短名 -> teams.json 官方全名（2026-09-12 审计补充）
    "ein frankfurt": "eintracht frankfurt",
    "frankfurt": "eintracht frankfurt",
    "m gladbach": "borussia monchengladbach",
    "gladbach": "borussia monchengladbach",
    "monchengladbach": "borussia monchengladbach",
    "bayern": "bayern munich",
    "dortmund": "borussia dortmund",
    "leverkusen": "bayer 04 leverkusen",
    "koln": "1 fc koln",
    "mainz": "1 fsv mainz 05",
    "hoffenheim": "tsg 1899 hoffenheim",
    "paderborn": "sc paderborn 07",
    "union berlin": "1 fc union berlin",
    "elversberg": "sv elversberg",
    "hamburg": "hamburger sv",
    "bremen": "sv werder bremen",
    "freiburg": "sc freiburg",
    "stuttgart": "vfb stuttgart",
    "augsburg": "fc augsburg",
    # 意甲：ESPN 用 "Milan" 指 AC 米兰，但它同时是 "Inter Milan" 的子串，
    # token 子集匹配会得到 2 个候选而放弃，必须显式指定。
    "milan": "ac milan",
}

STOP_TOKENS = {"fc", "afc", "city", "town", "united", "county", "cf"}


def norm(name: str) -> str:
    """归一化球队名：小写、去标点、压缩空白。"""
    s = re.sub(r"[^0-9a-z]+", " ", name.lower())
    return " ".join(s.split())


def load_teams(league: str):
    """读 references/<league>/teams.json，建 归一化名 → 代码 查找表。"""
    path = os.path.join(BASE, "references", league, "teams.json")
    if not os.path.exists(path):
        sys.exit(f"❌ 未找到 {path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    teams = data.get("teams", data)
    lookup = {}
    for code, info in teams.items():
        lookup[code.upper()] = code
        for key in ("name", "name_en", "name_cn"):
            v = info.get(key)
            if isinstance(v, str) and v:
                lookup[norm(v)] = code
    return teams, lookup


def resolve(name: str, lookup: dict):
    """
    球队名 → 代码。精确 → 别名 → 去后缀 token 子集 → 唯一前缀。

    ⚠️ 模糊匹配不能用 isalpha() 过滤查找键：德甲/意甲大量队名自带数字
    （1. FC Koln、TSG 1899 Hoffenheim、Bayer 04 Leverkusen、SC Paderborn 07、
    FC Schalke 04），一旦要求"键去空格后全是字母"，这些队会被整体排除在
    模糊匹配之外，ESPN 的短名（Leverkusen / Hoffenheim / FC Koln）就全部无法
    映射。纯代码键本就因不含空格被 `" " in key` 排除，无需再靠 isalpha。
    """
    n = norm(name)
    if n in lookup:
        return lookup[n]
    if n in ALIASES and ALIASES[n] in lookup:
        return lookup[ALIASES[n]]
    tokens = {t for t in n.split() if t not in STOP_TOKENS}
    hits = [c for key, c in lookup.items()
            if " " in key
            and tokens and tokens <= set(key.split()) - STOP_TOKENS]
    if len(hits) == 1:
        return hits[0]
    if tokens:
        first = sorted(tokens, key=len, reverse=True)[0]
        if len(first) >= 4:
            pref = [c for key, c in lookup.items()
                    if " " in key
                    and key.split() and key.split()[0].startswith(first)]
            if len(pref) == 1:
                return pref[0]
    return None


def parse_score(score: str):
    m = re.match(r"^\s*(\d+)\s*[-–:]\s*(\d+)\s*$", str(score or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def convert_standings(path: str, lookup: dict):
    """
    get_standings 输出 → table.json 字典。

    ⚠️ 必须合并同一支球队的多行：ESPN 偶尔把一支球队拆成两行
    （例如德甲同时出现 "Union Berlin" 和 "1. FC Union Berlin"，各带一部分
    已赛场次）。若按代码直接赋值，后一行会静默覆盖前一行，导致该队积分和
    场次偏少，进而让积分榜排名与"争冠/保级"判定出错。两行覆盖的是互不重叠
    的比赛，因此累加即可。
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("rows", data.get("standings", []))
    table, unresolved = {}, []
    merged = {}
    table_names = {}
    # 原始字段名 -> table.json 规范字段名（合并时必须按规范名累加）
    additive = {
        "played": "played",
        "won": "wins",
        "drawn": "draws",
        "lost": "losses",
        "goals_for": "gf",
        "goals_against": "ga",
        "goal_difference": "gd",
        "points": "points",
    }
    for row in rows:
        name = row.get("team", "")
        code = resolve(name, lookup)
        if not code:
            unresolved.append(name)
            continue
        if code in table:
            # 记录首次出现时的队名，便于提示中看到"哪两行"被合并
            merged.setdefault(code, [table_names.get(code, code)]).append(name)
            for src, dest in additive.items():
                if isinstance(row.get(src), (int, float)):
                    table[code][dest] = table[code].get(dest, 0) + row[src]
            continue
        table_names[code] = name
        table[code] = {
            "points": row.get("points", 0),
            "gd": row.get("goal_difference", 0),
            "gf": row.get("goals_for", 0),
            "ga": row.get("goals_against", 0),
            "played": row.get("played", 0),
            "wins": row.get("won", 0),
            "draws": row.get("drawn", 0),
            "losses": row.get("lost", 0),
        }
    for code, names in merged.items():
        print(f"  ⚠️ {code} 在积分榜中出现 {len(names)} 行（{', '.join(names)}），已合并累加")
    return table, unresolved


def split_matches(paths, lookup: dict):
    """get_matches 输出（可多份）→ (results 列表, fixtures 列表, unresolved)"""
    results, fixtures, unresolved = [], [], []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for m in data.get("matches", []):
            home = resolve(m.get("home", ""), lookup)
            away = resolve(m.get("away", ""), lookup)
            if not home or not away:
                unresolved.append(f'{m.get("home")} vs {m.get("away")}')
                continue
            if m.get("played"):
                hg, ag = None, None
                if m.get("hg") is not None and m.get("ag") is not None:
                    hg, ag = int(m["hg"]), int(m["ag"])
                else:
                    parsed = parse_score(m.get("score", ""))
                    if parsed:
                        hg, ag = parsed
                if hg is None:
                    print(f"  ⚠️ 跳过无比分: {home} vs {away} @ {m.get('date')}")
                    continue
                results.append({"home": home, "away": away, "hg": hg, "ag": ag})
            else:
                fixtures.append({"home": home, "away": away,
                                 "_date": m.get("date", ""),
                                 "_kickoff": m.get("kickoff_utc", "")})
    return results, fixtures, unresolved


def merge_results(old_matches: list, new_matches: list, fresh: bool):
    """按精确四元组去重合并；同对异比分默认保留两条并告警。"""
    old = [dict(m) for m in old_matches]
    old_keys = {(m["home"], m["away"], m["hg"], m["ag"]) for m in old}
    added, warned = [], []
    for e in new_matches:
        key = (e["home"], e["away"], e["hg"], e["ag"])
        if key in old_keys:
            continue
        pair = (e["home"], e["away"])
        same_pair_diff = [m for m in old
                          if (m["home"], m["away"]) == pair
                          and (m["hg"], m["ag"]) != (e["hg"], e["ag"])]
        if same_pair_diff and fresh:
            for m in same_pair_diff:
                m["hg"], m["ag"] = e["hg"], e["ag"]
                old_keys.add((m["home"], m["away"], m["hg"], m["ag"]))
            continue
        if same_pair_diff:
            warned.append(pair)
        old_keys.add(key)
        added.append(e)
    return old + added, added, warned


def write_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  ✅ 已写入 {os.path.relpath(path, BASE)}")


def run_elo(league: str):
    script = os.path.join(BASE, "scripts", "elo_updater.py")
    print(f"\n>>> 运行 elo_updater {league} --reset ...")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, script, league, "--reset"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=BASE, env=env)
    for line in (r.stdout or "").strip().splitlines()[-15:]:
        print(f"  {line}")
    if r.returncode != 0:
        print(f"  ❌ elo_updater 失败: {(r.stderr or '')[-500:]}")


def main():
    ap = argparse.ArgumentParser(description="football MCP → skill 快照同步")
    ap.add_argument("league", help="skill 联赛目录名，如 championship / epl")
    ap.add_argument("--matches", action="append", default=[],
                    help="get_matches 原始输出 JSON（可多次传入）")
    ap.add_argument("--standings", help="get_standings 原始输出 JSON")
    ap.add_argument("--fresh", action="store_true",
                    help="同队对旧比分不同时用新数据覆盖（默认保留两条）")
    ap.add_argument("--elo", action="store_true",
                    help="写入 results 后运行 elo_updater --reset 刷新评级")
    ap.add_argument("--dry-run", action="store_true", help="只转换不写盘")
    args = ap.parse_args()

    teams, lookup = load_teams(args.league)
    print(f"联赛 {args.league}: {len(teams)} 支球队映射已加载")

    table, results, fixtures = None, [], []
    unresolved = []
    if args.standings:
        table, u1 = convert_standings(args.standings, lookup)
        unresolved += u1
    if args.matches:
        results, fixtures, u2 = split_matches(args.matches, lookup)
        unresolved += u2
    if unresolved:
        print("❌ 以下球队名无法映射到代码，中止（先补 teams.json 或别名）:")
        for u in sorted(set(unresolved)):
            print(f"   - {u}")
        sys.exit(2)

    if args.dry_run:
        print(f"[dry-run] table={len(table or {})} results={len(results)} fixtures={len(fixtures)}")
        return

    live_dir = os.path.join(BASE, "data", "live", args.league)
    if table is not None:
        write_json(os.path.join(live_dir, "table.json"), table)
    if args.matches:
        rp = os.path.join(live_dir, "results.json")
        old = []
        if os.path.exists(rp):
            with open(rp, encoding="utf-8") as f:
                old = json.load(f).get("matches", [])
        merged, added, warned = merge_results(old, results, args.fresh)
        write_json(rp, {"matches": merged})
        print(f"  · 赛果合并: 旧 {len(old)} + 新增 {len(added)} = {len(merged)}")
        for pair in sorted(set(warned)):
            print(f"  ⚠️ {pair[0]} vs {pair[1]} 存在不同比分，已保留两条（--fresh 可覆盖）")
        fp = os.path.join(live_dir, "fixtures.json")
        write_json(fp, [{"home": m["home"], "away": m["away"]} for m in fixtures])
        dates = [m["_date"] for m in fixtures if m["_date"]]
        if dates:
            print(f"  · 最近赛程日: {min(dates)} ~ {max(dates)}（{len(fixtures)} 场）")
    if args.elo:
        run_elo(args.league)
    print("\n✅ MCP 快照同步完成")


if __name__ == "__main__":
    main()
