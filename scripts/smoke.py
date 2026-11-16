"""Run a small live availability smoke test for representative AKShare APIs.

烟测只回答一个问题：**这类数据源现在还能不能取到数**。
所以每组给多个候选，组内任一候选成功即视为该组可用；
任何候选挂掉都只算信号，不算故障。

候选只写"当前叫法"。上游 akshare 会改名（1.20.44 / 1.20.46 一次性改掉了几百个），
写死旧名会让整组因为 AttributeError 变红——那不是数据源故障。所以：
  * 候选名先按 akshare 内置 renames.json 解析成**当前名**（见 resolve_names）；
  * catalog 里以旧名存档的条目也能被找到，参数沿用 catalog 的实测值；
  * 名字被彻底移除、救不回来时，结果标 RENAMED，明确区别于 FAIL。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import sys
from pathlib import Path
from typing import Any

from check import check_one, load_catalog
from lookup import catalog_removed, catalog_renames

GROUPS: dict[str, tuple[str, ...]] = {
    "A股实时行情": ("stock_zh_a_spot_em", "stock_zh_a_spot_tx"),
    # 历史日线这三个候选在 1.20.44 / 1.20.46 全都改过名，这里保留旧名，
    # 交给 resolve_names() 按 renames.json 换到当前名。
    "A股历史日线": ("stock_zh_a_hist", "stock_zh_a_daily", "stock_zh_a_hist_tx"),
    # 曾经这里只有 index_zh_a_hist 一个候选，它一超时整组变红（Actions run 42887680319）。
    # 该接口每次调用都要先分页拉一遍指数量表（17 页、页间 sleep），冷启动约 30s，
    # 所以补上新浪/腾讯两个轻量候选，避免单点把整组拖红。
    "指数历史日线": ("index_zh_a_hist", "stock_zh_index_daily", "stock_zh_index_daily_tx"),
    "ETF历史日线": ("fund_etf_hist_em", "fund_etf_hist_sina"),
    "期货历史日线": ("futures_zh_daily_sina",),
    "中国宏观数据": ("macro_china_gdp",),
}

# catalog 的 params_used 是采集当天为了记录列名取的宽区间（index_zh_a_hist 固化的是
# 19700101→22220101，一次翻完全部历史）。验证连通性用窄区间就够，也更省 CI 时间。
WINDOW_DAYS = 30

# 按"当前名"声明窄区间参数；旧名解析成当前名后同样命中，新旧两套都列着，
# 这样即便某个版本没有 renames.json，参数也还是对的。
SMOKE_PARAMS: dict[str, dict[str, Any]] = {
    "stock_zh_a_hist": {"symbol": "000001", "period": "daily", "adjust": ""},
    "stock_zh_a_hist_em": {"symbol": "000001", "period": "daily", "adjust": ""},
    "stock_zh_a_daily": {"symbol": "sz000001", "adjust": ""},
    "stock_zh_a_hist_sina": {"symbol": "sz000001", "adjust": ""},
    "stock_zh_a_hist_tx": {"symbol": "sz000001", "adjust": ""},
    "index_zh_a_hist": {"symbol": "000016", "period": "daily"},
    "index_zh_hist_em": {"symbol": "000016", "period": "daily"},
    "stock_zh_index_daily": {"symbol": "sz399552"},
    "index_zh_daily_sina": {"symbol": "sz399552"},
    "stock_zh_index_daily_tx": {"symbol": "sh000001"},
    "index_zh_daily_tx": {"symbol": "sh000001"},
    "fund_etf_hist_em": {"symbol": "513500", "period": "daily", "adjust": ""},
    "fund_etf_hist_sina": {"symbol": "sh510050"},
    "futures_zh_daily_sina": {"symbol": "RB0"},
}

# 冷启动就要几十秒的接口，别用统一超时冤枉它（新旧名都列，映射前后都能命中）
TIMEOUT_OVERRIDES: dict[str, float] = {
    "index_zh_a_hist": 90.0,
    "index_zh_hist_em": 90.0,
}


def current_version() -> str:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            import akshare

            return str(getattr(akshare, "__version__", "?"))
        except Exception:
            return "?"


def resolve_names(names: tuple[str, ...]) -> tuple[list[tuple[str, str | None]], list[str]]:
    """把组内候选解析成当前名；返回 ([(当前名, 原始旧名或 None)], 已彻底移除的名字)。

    上游只在"改名"时才给新名，"移除"是另一张表——被移除的接口留着也没意义，
    单独报出来，免得混进 FAIL 里掩盖真正的问题。
    """
    renames = catalog_renames()
    removed = catalog_removed()
    resolved: list[tuple[str, str | None]] = []
    dropped: list[str] = []
    for name in names:
        current = renames.get(name, name)
        if current != name:
            resolved.append((current, name))
        elif name in removed:
            dropped.append(name)
        else:
            resolved.append((name, None))
    return resolved, dropped


def _recent_window(days: int = WINDOW_DAYS) -> tuple[str, str]:
    today = dt.date.today()
    return (today - dt.timedelta(days=days)).strftime("%Y%m%d"), today.strftime("%Y%m%d")


def smoke_item(item: dict[str, Any]) -> dict[str, Any]:
    """把 catalog 条目改写成“轻量烟测”版本：窄日期区间，且不新增目录里没有的参数。"""
    params = dict(item.get("params_used") or {})
    for key, value in (SMOKE_PARAMS.get(str(item.get("name"))) or {}).items():
        if key in params:
            params[key] = value
    if "start_date" in params and "end_date" in params:
        start, end = _recent_window()
        params["start_date"], params["end_date"] = start, end
    return {**item, "args_used": list(item.get("args_used") or []), "params_used": params}


def _probe(item: dict[str, Any], timeout: float, attempts: int = 1) -> dict[str, Any]:
    probe = smoke_item(item)
    result: dict[str, Any] = {}
    for index in range(max(1, attempts)):
        result = check_one(probe, timeout)
        if result.get("ok"):
            result["attempts"] = index + 1
            return result
    result["attempts"] = max(1, attempts)
    return result


def _catalog_item(name: str, by_name: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """按当前名找 catalog 条目；找不到就按改名表反查旧名存档的那条。"""
    item = by_name.get(name)
    if item is not None:
        return item
    for old, current in catalog_renames().items():
        if current == name and old in by_name:
            return by_name[old]
    return None


def run(timeout: float, retries: int = 1) -> dict[str, Any]:
    data = load_catalog()
    by_name = {str(item.get("name")): item for item in data.get("interfaces") or []}
    results: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []
    renamed: list[dict[str, str]] = []

    for group, names in GROUPS.items():
        resolved, dropped = resolve_names(names)
        group_results: list[dict[str, Any]] = []
        for name, old_name in resolved:
            if old_name:
                renamed.append({"group": group, "old": old_name, "new": name})
            item = _catalog_item(name, by_name)
            if item is None:
                missing.append(name)
                continue
            probe_item = {**item, "name": name}
            # 全部候选都探一遍：哪个数据源挂了要能一眼看出来，而不是被组内其它候选掩盖
            group_results.append(_probe(probe_item, TIMEOUT_OVERRIDES.get(name, timeout)))

        # 整组都没通才重试首选，用一次重试区分“真故障”和“网络抖动”。
        # 但如果失败原因是"本机压根没这个接口"，重试毫无意义，只会白等一轮。
        all_missing = bool(group_results) and all(
            _verdict(r) == "MISSING" for r in group_results)
        if (retries > 0 and group_results and not all_missing
                and not any(r.get("ok") for r in group_results)):
            for first_name, _ in resolved:
                first = _catalog_item(first_name, by_name)
                if first is None:
                    continue
                retried = _probe({**first, "name": first_name},
                                 TIMEOUT_OVERRIDES.get(first_name, timeout),
                                 attempts=retries + 1)
                retried["retried"] = True
                group_results.append(retried)
                break

        results[group] = group_results
        if dropped and not group_results:
            missing.extend(dropped)

    # 分组判定分两种，别混在一起：
    #   failed_groups       组内所有候选都真的试着连了、但全没成功 —— 数据源问题
    #   missing_groups      组内候选本机 akshare 里都不存在 —— 版本问题，不是数据源问题
    failed_groups: list[str] = []
    missing_groups: list[str] = []
    for group, group_results in results.items():
        if any(result.get("ok") for result in group_results):
            continue
        if group_results and all(_verdict(r) == "MISSING" for r in group_results):
            missing_groups.append(group)
        else:
            failed_groups.append(group)

    return {
        "akshare_now": current_version(),
        "catalog_version": (data.get("meta") or {}).get("akshare_version", "?"),
        "catalog_generated": (data.get("meta") or {}).get("generated_at", ""),
        "groups": results,
        "missing": missing,
        "renamed": renamed,
        "failed_groups": failed_groups,
        "missing_groups": missing_groups,
    }


def _verdict(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "OK"
    if result.get("timeout"):
        return "TIMEOUT"
    if "has no attribute" in str(result.get("error") or ""):
        # 本机 akshare 里没有这个接口：通常是本地版本比 catalog 旧，
        # 和数据源不可用是两件事，单独标出来免得误导排查方向
        return "MISSING"
    return "FAIL"


def render(report: dict[str, Any]) -> str:
    lines = [
        f"# akshare={report['akshare_now']} "
        f"catalog={report['catalog_version']} "
        f"generated={str(report['catalog_generated'])[:10]}"
    ]
    for group, results in report["groups"].items():
        if any(result.get("ok") for result in results):
            state = "OK"
        elif results and all(_verdict(r) == "MISSING" for r in results):
            state = "MISSING"
        else:
            state = "FAIL"
        lines.append(f"[{state}] {group}")
        for result in results:
            error = f"  {result['error']}" if result.get("error") else ""
            notes = []
            if (result.get("attempts") or 1) > 1:
                notes.append("重试后")
            if result.get("retried"):
                notes.append("整组失败后重试首选")
            suffix = f" ({', '.join(notes)})" if notes else ""
            lines.append(
                f"  {result['name']}: {_verdict(result)} ({result['seconds']}s){suffix}{error}"
            )
    for entry in report.get("renamed") or []:
        lines.append(f"改名: {entry['old']} -> {entry['new']}（{entry['group']}）")
    if report["missing"]:
        lines.append(f"缺少目录接口: {', '.join(report['missing'])}")
    return "\n".join(lines)


def render_summary(report: dict[str, Any]) -> str:
    """GitHub Actions 作业摘要（Markdown）。"""
    failed = report["failed_groups"]
    missing_groups = report.get("missing_groups") or []
    if not failed and not report["missing"]:
        verdict = "✅ 全部数据源可用"
        if missing_groups:
            verdict += "（有分组仅因本机 akshare 版本旧而缺失接口，非数据源故障）"
    else:
        verdict = "❌ 有分组全部候选失败"
    lines = [
        "## AKShare live smoke test",
        "",
        f"- **结论**: {verdict}",
        f"- **akshare**: `{report['akshare_now']}`（catalog: `{report['catalog_version']}`）",
    ]
    if failed:
        lines.append(f"- **失败分组**: {', '.join(failed)}")
    if missing_groups:
        lines.append(f"- **接口在本机 akshare 中不存在（版本问题，非数据源故障）**: "
                     f"{', '.join(missing_groups)}")
    if report["missing"]:
        lines.append(f"- **缺少目录接口**: {', '.join(report['missing'])}")
    if report.get("renamed"):
        lines.append("- **已按改名表自动纠正**：" + "、".join(
            f"`{e['old']}`→`{e['new']}`" for e in report["renamed"]))
    lines += ["", "| 分组 | 接口 | 结果 | 耗时 | 尝试 |", "|---|---|---|---|---|"]
    icons = {"OK": "✅", "TIMEOUT": "⏱", "FAIL": "❌", "MISSING": "⚠️"}
    for group, results in report["groups"].items():
        for result in results:
            verdict = _verdict(result)
            lines.append(
                f"| {group} | `{result['name']}` | {icons[verdict]} {verdict} | "
                f"{result['seconds']}s | {result.get('attempts', 1)} |"
            )
    note = ("\n`⚠️ MISSING` = 本机 akshare 没有这个接口（catalog 更新），"
            "不是数据源故障。\n")
    return "\n".join(lines) + "\n" + note


def main() -> None:
    parser = argparse.ArgumentParser(description="Live smoke test for representative AKShare APIs")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="单接口超时秒数（默认 60，含 akshare 导入）")
    parser.add_argument("--retries", type=int, default=1,
                        help="整组候选全部失败后重试首选的次数（默认 1；0 表示不重试）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--summary", metavar="PATH",
                        help="额外写出 Markdown 摘要（CI 可指向 $GITHUB_STEP_SUMMARY）")
    args = parser.parse_args()

    report = run(args.timeout, max(0, args.retries))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))

    if args.summary:
        try:
            Path(args.summary).write_text(render_summary(report), encoding="utf-8")
        except OSError as exc:
            print(f"写摘要失败: {exc}", file=sys.stderr)

    # 退出码只反映"数据源是不是真的挂了"：
    #   有分组全部候选失败、或 catalog 里查不到候选 → 1
    #   只是本机 akshare 版本旧、接口名不存在（MISSING）→ 0，那是要升级依赖，不是故障
    # 这样即使本地版本落后，也能用退出码判断数据源健康度。
    if report["missing"] or report["failed_groups"]:
        sys.exit(1)


if __name__ == "__main__":
    main()