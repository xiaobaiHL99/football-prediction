#!/usr/bin/env python3
"""全联赛快照体检（snapshot health check）。

背景
----
2026-09-12 的人工排查发现：德甲快照缺埃尔沃斯堡/沙尔克04、意甲缺弗洛西诺内、
沙特联缺赛哈特海湾/迪里耶，导致这些球队的真实比赛根本无法建模；同时欧冠
results.json 里混入了一条亚冠数据。这类问题**不会自己暴露**——预测照常跑完，
只是悄悄算错。本脚本把它变成机械检查，避免重复人工排查。

设计原则
--------
不依赖任何外部数据源，只做**本地各文件之间的代码集一致性**比对。
外部数据（积分榜）会引入反爬和时效问题，本地一致性检查则永远可运行。

严重度阶梯（关键）
------------------
CRITICAL  真实比赛来源（results/fixtures/台账）引用了快照里没有的代码
          → 该场比赛无法建模，必须立刻修
WARN      table.json（派生数据）含快照没有的代码 → 多为派生数据未刷新，需人工确认
WARN      teams.json 里的队从未在任何比赛来源出现 → 可能是已降级队的残留
WARN      快照超过 STALE_DAYS 天未更新
INFO      队数统计、超集联赛

用法
----
    python scripts/audit_snapshots.py
    python scripts/audit_snapshots.py --json data/reviews/snapshot_audit.json
    python scripts/audit_snapshots.py --quiet     # 只输出问题

退出码：发现 CRITICAL 时为 1，否则为 0（可直接用于 CI / 日常流程）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "references"
LIVE_DIR = ROOT / "data" / "live"
LEDGER = ROOT / "data" / "reviews" / "predictions.json"

STALE_DAYS = 21

# 台账里的联赛名 -> references/ 目录名。
# 注意：不要把资格赛联赛（ucl_qualifying / uel_qualifying）映射到正赛快照。
# 资格赛球队本就不在正赛快照里，映射后会产生大量假 CRITICAL。这类赛事按
# LEGACY_LEDGER_LEAGUES 处理。
LEAGUE_ALIASES: dict[str, str] = {}

# 已退役但仍留在台账里的历史赛事：有预测记录，但既无 references 目录也无
# data/live 数据，因此无法也不应再做一致性校验。
LEGACY_LEDGER_LEAGUES = {
    "ucl_qualifying": "欧冠资格赛（已结束，快照与实时数据均已退役）",
    "uel_qualifying": "欧联资格赛（已结束，快照与实时数据均已退役）",
}

# 这些联赛的快照刻意大于联赛规模（保留全部候选池），其"多余"球队不算问题。
# 值 = 简要说明，写入报告以便日后复核。
EXPECTED_SUPERSETS = {
    "champions_league": "快照为欧冠候选池，含资格赛阶段球队",
    "europa_league": "快照为欧联候选池，含资格赛阶段球队",
}


# --------------------------------------------------------------------------- #
# 读取工具：容忍多种 schema
# --------------------------------------------------------------------------- #
def _read_json(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def _codes_from_teams(data) -> tuple[set[str], dict[str, float], str | None]:
    """teams.json -> (代码集合, 代码->elo, _meta.updated_at)。"""
    if not isinstance(data, dict):
        return set(), {}, None
    teams = data.get("teams", data)
    if not isinstance(teams, dict):
        return set(), {}, None
    codes, elos = set(), {}
    for code, info in teams.items():
        if not isinstance(code, str) or not isinstance(info, dict):
            continue
        codes.add(code)
        elo = info.get("elo")
        if isinstance(elo, (int, float)):
            elos[code] = float(elo)
    updated = None
    meta = data.get("_meta")
    if isinstance(meta, dict):
        updated = meta.get("updated_at") or meta.get("updated")
    return codes, elos, updated


def _codes_from_table(data) -> tuple[set[str], dict[str, int]]:
    """table.json -> (代码集合, 代码->已赛场次)。兼容 points/pts 两种字段命名。"""
    if not isinstance(data, dict):
        return set(), {}
    rows = data.get("table", data)
    if not isinstance(rows, dict):
        return set(), {}
    codes, played = set(), {}
    for code, row in rows.items():
        if not isinstance(code, str) or not isinstance(row, dict):
            continue
        codes.add(code)
        p = row.get("played")
        if not isinstance(p, (int, float)):
            won = row.get("won", row.get("wins"))
            drawn = row.get("drawn", row.get("draws"))
            lost = row.get("lost", row.get("losses"))
            if all(isinstance(v, (int, float)) for v in (won, drawn, lost)):
                p = won + drawn + lost
        if isinstance(p, (int, float)):
            played[code] = int(p)
    return codes, played


def _codes_from_matches(data) -> set[str]:
    """results.json / fixtures.json -> 代码集合。"""
    if not isinstance(data, dict):
        return set()
    rows = data.get("matches", data.get("fixtures", []))
    if not isinstance(rows, list):
        return set()
    codes = set()
    for m in rows:
        if not isinstance(m, dict):
            continue
        for key in ("home", "away", "home_team", "away_team"):
            v = m.get(key)
            if isinstance(v, str) and v:
                codes.add(v)
    return codes


def _norm_name(name: str) -> str:
    """球队名归一化，用于跨联赛比对同一支球队。"""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(name).lower()).split())


def _load_ledger_codes() -> dict[str, set[str]]:
    data = _read_json(LEDGER)
    out: dict[str, set[str]] = {}
    if not isinstance(data, dict):
        return out
    for entry in data.get("predictions", data.get("entries", [])):
        if not isinstance(entry, dict):
            continue
        # 作废条目（void_reason）是已知无效的历史痕迹，不应再参与一致性校验，
        # 否则一条无法结算的旧记录会让体检永久报错。
        if entry.get("void_reason"):
            continue
        lg = entry.get("league")
        if not isinstance(lg, str):
            continue
        lg = LEAGUE_ALIASES.get(lg, lg)
        bucket = out.setdefault(lg, set())
        for key in ("home", "away"):
            v = entry.get(key)
            if isinstance(v, str) and v:
                bucket.add(v)
    return out


# --------------------------------------------------------------------------- #
# 体检主体
# --------------------------------------------------------------------------- #
def audit() -> dict:
    leagues = sorted(p.name for p in REF_DIR.iterdir() if p.is_dir())
    ledger_codes = _load_ledger_codes()
    findings: list[dict] = []
    report: dict = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "stale_days_threshold": STALE_DAYS,
        "leagues": {},
        "findings": findings,
    }

    def add(sev: str, league: str, check: str, message: str) -> None:
        findings.append({"severity": sev, "league": league, "check": check, "message": message})

    for lg in leagues:
        teams_path = REF_DIR / lg / "teams.json"
        teams_raw = _read_json(teams_path)
        snapshot_codes, elos, updated = _codes_from_teams(teams_raw)

        table_codes, table_played = _codes_from_table(_read_json(LIVE_DIR / lg / "table.json"))
        result_codes = _codes_from_matches(_read_json(LIVE_DIR / lg / "results.json"))
        fixture_codes = _codes_from_matches(_read_json(LIVE_DIR / lg / "fixtures.json"))
        led_codes = ledger_codes.get(lg, set())

        info = {
            "snapshot_team_count": len(snapshot_codes),
            "table_team_count": len(table_codes),
            "results_match_codes": len(result_codes),
            "fixture_codes": len(fixture_codes),
            "ledger_codes": len(led_codes),
            "snapshot_updated_at": updated,
            "snapshot_age_days": None,
            "teams_missing_elo": [],
        }

        # --- 结构完整性 ---
        if isinstance(teams_raw, dict) and "__error__" in teams_raw:
            add("CRITICAL", lg, "parse", f"teams.json 解析失败：{teams_raw['__error__']}")
        if not snapshot_codes:
            add("CRITICAL", lg, "parse", "teams.json 未解析出任何球队")

        missing_elo = sorted(snapshot_codes - set(elos))
        if missing_elo:
            info["teams_missing_elo"] = missing_elo
            add("CRITICAL", lg, "structure", f"{len(missing_elo)} 支球队缺少 elo 字段：{', '.join(missing_elo)}")

        # --- 核心检查：真实比赛来源引用了快照没有的代码 ---
        # results / fixtures / 台账都代表真实存在的比赛，缺代码 = 无法建模。
        real_sources = {
            "results.json": result_codes,
            "fixtures.json": fixture_codes,
            "台账": led_codes,
        }
        for src_name, src_codes in real_sources.items():
            unknown = sorted(src_codes - snapshot_codes)
            if unknown:
                add(
                    "CRITICAL",
                    lg,
                    "missing_team",
                    f"{src_name} 使用快照中不存在的代码 {', '.join(unknown)} —— 这些比赛无法建模",
                )

        # --- 派生数据过期 ---
        unknown_table = sorted(table_codes - snapshot_codes)
        if unknown_table:
            add(
                "WARN",
                lg,
                "stale_table",
                f"table.json 含快照中不存在的代码 {', '.join(unknown_table)} —— 派生数据待刷新或快照需复核",
            )

        # --- 快照里的队从未出现在任何比赛来源 ---
        all_observed = result_codes | fixture_codes | led_codes | table_codes
        never_seen = sorted(snapshot_codes - all_observed)
        if never_seen:
            note = EXPECTED_SUPERSETS.get(lg)
            if note:
                add("INFO", lg, "superset", f"{len(never_seen)} 支球队未出现（预期内：{note}）")
            else:
                add(
                    "WARN",
                    lg,
                    "unused_team",
                    f"{len(never_seen)} 支球队未出现在任何比赛来源 {', '.join(never_seen[:12])}"
                    + (" ..." if len(never_seen) > 12 else "")
                    + " —— 可能是已降级队残留",
                )

        # --- 时效 ---
        if updated:
            try:
                stamp = dt.datetime.fromisoformat(str(updated).replace("Z", ""))
                age = (dt.datetime.now() - stamp).days
                info["snapshot_age_days"] = age
                if age > STALE_DAYS:
                    add("WARN", lg, "staleness", f"快照已 {age} 天未更新（阈值 {STALE_DAYS}）")
            except ValueError:
                add("WARN", lg, "staleness", f"无法解析 updated_at：{updated!r}")
        else:
            add("WARN", lg, "staleness", "teams.json 缺少 _meta.updated_at，无法判断时效")

        # --- 联赛规模提示：快照与派生表队数不一致 ---
        if table_codes and snapshot_codes != table_codes:
            info["code_set_mismatch_with_table"] = True

        report["leagues"][lg] = info

    # 台账里存在但没有 references 目录的联赛
    for lg in sorted(set(ledger_codes) - set(leagues)):
        if lg in LEGACY_LEDGER_LEAGUES:
            add("INFO", lg, "legacy_league",
                f"台账 {len(ledger_codes[lg])} 个代码引用：{LEGACY_LEDGER_LEAGUES[lg]}")
        else:
            add(
                "WARN",
                lg,
                "orphan_league",
                f"台账有 {len(ledger_codes[lg])} 个代码引用，但 references/{lg}/ 不存在",
            )

    # 同一支球队在不同联赛快照里用了不同代码（如国际米兰：seriea=INTER，champions_league=INT）。
    # 代码是按联赛隔离的，因此目前不影响预测；但任何跨联赛的功能（欧战参赛队合并、
    # 跨赛事台账统计）都会踩坑。统一代码的收益是长期可维护性，故报告为 WARN。
    #
    # 注意：这里**只查"同队不同代码"**，不查"同代码不同队"。后者是 3 字母前缀方案的
    # 既定设计（HAM=Hammarby/Hamburg/HamKam、MIL=AC米兰/Millwall…），联赛内完全隔离，
    # 逐条报告会产生约 40 条噪声，把真正的问题淹没。
    name_to_codes: dict[str, dict[str, str]] = {}
    for lg in leagues:
        teams_raw = _read_json(REF_DIR / lg / "teams.json")
        if not isinstance(teams_raw, dict):
            continue
        teams = teams_raw.get("teams", teams_raw)
        if not isinstance(teams, dict):
            continue
        for code, info in teams.items():
            if not isinstance(info, dict):
                continue
            name = info.get("name")
            if isinstance(name, str) and name:
                name_to_codes.setdefault(_norm_name(name), {}).setdefault(lg, code)

    for name, per_league in sorted(name_to_codes.items()):
        codes = set(per_league.values())
        if len(codes) > 1:
            detail = ", ".join(f"{lg}={code}" for lg, code in sorted(per_league.items()))
            add("WARN", "(跨联赛)", "code_mismatch",
                f"{name} 在不同快照使用不同代码（{detail}）")

    counts = {"CRITICAL": 0, "WARN": 0, "INFO": 0}
    for f in findings:
        counts[f["severity"]] += 1
    report["summary"] = counts
    return report


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
SEV_MARK = {"CRITICAL": "!!", "WARN": " *", "INFO": " ."}


def print_report(report: dict, quiet: bool) -> None:
    print(f"全联赛快照体检  {report['generated_at']}")
    print("=" * 78)

    if not quiet:
        print(f"{'联赛':<20}{'快照':>5}{'表':>5}{'赛果':>6}{'赛程':>6}{'台账':>6}{'天数':>6}")
        print("-" * 78)
        for lg, info in report["leagues"].items():
            age = info["snapshot_age_days"]
            print(
                f"{lg:<20}{info['snapshot_team_count']:>5}{info['table_team_count']:>5}"
                f"{info['results_match_codes']:>6}{info['fixture_codes']:>6}"
                f"{info['ledger_codes']:>6}{(age if age is not None else '-'):>6}"
            )
        print()

    order = {"CRITICAL": 0, "WARN": 1, "INFO": 2}
    findings = sorted(report["findings"], key=lambda f: (order[f["severity"]], f["league"], f["check"]))

    shown = [f for f in findings if not quiet or f["severity"] != "INFO"]
    if not shown:
        print("未发现问题。")
    for f in shown:
        print(f"{SEV_MARK[f['severity']]} [{f['severity']:<8}] {f['league']:<18} {f['message']}")

    s = report["summary"]
    print("=" * 78)
    print(f"汇总：CRITICAL {s['CRITICAL']}  WARN {s['WARN']}  INFO {s['INFO']}")
    if s["CRITICAL"]:
        print("存在 CRITICAL：真实比赛引用了快照中不存在的球队，必须修复后再预测。")


def main() -> int:
    ap = argparse.ArgumentParser(description="全联赛快照体检")
    ap.add_argument("--json", help="把完整报告写入指定路径")
    ap.add_argument("--quiet", action="store_true", help="只输出 WARN/CRITICAL，隐藏 INFO 与统计表")
    args = ap.parse_args()

    report = audit()
    print_report(report, args.quiet)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入 {out}")

    return 1 if report["summary"]["CRITICAL"] else 0


if __name__ == "__main__":
    sys.exit(main())
