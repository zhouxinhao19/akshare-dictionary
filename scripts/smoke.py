"""Run a small live availability smoke test for representative AKShare APIs.

烟测只回答一个问题：**这类数据源现在还能不能取到数**。
所以每组给多个候选，组内任一候选成功即视为该组可用；
任何候选挂掉都只算信号，不算故障。
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


GROUPS: dict[str, tuple[str, ...]] = {
    "A股实时行情": ("stock_zh_a_spot_em", "stock_zh_a_spot_tx"),
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

SMOKE_PARAMS: dict[str, dict[str, Any]] = {
    "stock_zh_a_hist": {"symbol": "000001", "period": "daily", "adjust": ""},
    "stock_zh_a_daily": {"symbol": "sz000001", "adjust": ""},
    "stock_zh_a_hist_tx": {"symbol": "sz000001", "adjust": ""},
    "index_zh_a_hist": {"symbol": "000016", "period": "daily"},
    "stock_zh_index_daily": {"symbol": "sz399552"},
    "stock_zh_index_daily_tx": {"symbol": "sh000001"},
    "fund_etf_hist_em": {"symbol": "513500", "period": "daily", "adjust": ""},
    "fund_etf_hist_sina": {"symbol": "sh510050"},
    "futures_zh_daily_sina": {"symbol": "RB0"},
}

# 冷启动就要几十秒的接口，别用统一超时冤枉它
TIMEOUT_OVERRIDES: dict[str, float] = {
    "index_zh_a_hist": 90.0,
}


def current_version() -> str:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            import akshare

            return str(getattr(akshare, "__version__", "?"))
        except Exception:
            return "?"


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


def run(timeout: float, retries: int = 1) -> dict[str, Any]:
    data = load_catalog()
    by_name = {str(item.get("name")): item for item in data.get("interfaces") or []}
    results: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []

    for group, names in GROUPS.items():
        group_results: list[dict[str, Any]] = []
        for name in names:
            item = by_name.get(name)
            if item is None:
                missing.append(name)
                continue
            # 全部候选都探一遍：哪个数据源挂了要能一眼看出来，而不是被组内其它候选掩盖
            group_results.append(_probe(item, TIMEOUT_OVERRIDES.get(name, timeout)))

        # 整组都没通才重试首选，用一次重试区分“真故障”和“网络抖动”
        if retries > 0 and group_results and not any(r.get("ok") for r in group_results):
            first_name = next((n for n in names if n in by_name), None)
            if first_name is not None:
                retried = _probe(by_name[first_name],
                                 TIMEOUT_OVERRIDES.get(first_name, timeout),
                                 attempts=retries + 1)
                retried["retried"] = True
                group_results.append(retried)

        results[group] = group_results

    failed_groups = [
        group for group, group_results in results.items()
        if not any(result.get("ok") for result in group_results)
    ]
    return {
        "akshare_now": current_version(),
        "catalog_version": (data.get("meta") or {}).get("akshare_version", "?"),
        "catalog_generated": (data.get("meta") or {}).get("generated_at", ""),
        "groups": results,
        "missing": missing,
        "failed_groups": failed_groups,
    }


def _verdict(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "OK"
    return "TIMEOUT" if result.get("timeout") else "FAIL"


def render(report: dict[str, Any]) -> str:
    lines = [
        f"# akshare={report['akshare_now']} "
        f"catalog={report['catalog_version']} "
        f"generated={str(report['catalog_generated'])[:10]}"
    ]
    for group, results in report["groups"].items():
        state = "OK" if any(result.get("ok") for result in results) else "FAIL"
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
    if report["missing"]:
        lines.append(f"缺少目录接口: {', '.join(report['missing'])}")
    return "\n".join(lines)


def render_summary(report: dict[str, Any]) -> str:
    """GitHub Actions 作业摘要（Markdown）。"""
    healthy = not (report["failed_groups"] or report["missing"])
    lines = [
        "## AKShare live smoke test",
        "",
        f"- **结论**: {'✅ 全部数据源可用' if healthy else '❌ 有分组全部候选失败'}",
        f"- **akshare**: `{report['akshare_now']}`（catalog: `{report['catalog_version']}`）",
    ]
    if report["failed_groups"]:
        lines.append(f"- **失败分组**: {', '.join(report['failed_groups'])}")
    if report["missing"]:
        lines.append(f"- **缺少目录接口**: {', '.join(report['missing'])}")
    lines += ["", "| 分组 | 接口 | 结果 | 耗时 | 尝试 |", "|---|---|---|---|---|"]
    for group, results in report["groups"].items():
        for result in results:
            icon = {"OK": "✅", "TIMEOUT": "⏱", "FAIL": "❌"}[_verdict(result)]
            lines.append(
                f"| {group} | `{result['name']}` | {icon} {_verdict(result)} | "
                f"{result['seconds']}s | {result.get('attempts', 1)} |"
            )
    return "\n".join(lines) + "\n"


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

    if report["missing"] or report["failed_groups"]:
        sys.exit(1)


if __name__ == "__main__":
    main()