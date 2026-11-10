"""Run a small live availability smoke test for representative AKShare APIs."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from typing import Any

from check import check_one, load_catalog


GROUPS: dict[str, tuple[str, ...]] = {
    "A股实时行情": ("stock_zh_a_spot_em", "stock_zh_a_spot_tx"),
    "A股历史日线": ("stock_zh_a_hist", "stock_zh_a_daily", "stock_zh_a_hist_tx"),
    "指数历史日线": ("index_zh_a_hist",),
    "ETF历史日线": ("fund_etf_hist_em", "fund_etf_hist_sina"),
    "期货历史日线": ("futures_zh_daily_sina",),
    "中国宏观数据": ("macro_china_gdp",),
}


def current_version() -> str:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            import akshare

            return str(getattr(akshare, "__version__", "?"))
        except Exception:
            return "?"


def run(timeout: float) -> dict[str, Any]:
    data = load_catalog()
    by_name = {str(item.get("name")): item for item in data.get("interfaces") or []}
    results: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []

    for group, names in GROUPS.items():
        group_results = []
        for name in names:
            item = by_name.get(name)
            if item is None:
                missing.append(name)
                continue
            group_results.append(check_one(item, timeout))
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Live smoke test for representative AKShare APIs")
    parser.add_argument("--timeout", type=float, default=45.0,
                        help="单接口超时秒数（默认 45，含 akshare 导入）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    report = run(args.timeout)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"# akshare={report['akshare_now']} "
            f"catalog={report['catalog_version']} "
            f"generated={str(report['catalog_generated'])[:10]}"
        )
        for group, results in report["groups"].items():
            state = "OK" if any(result.get("ok") for result in results) else "FAIL"
            print(f"[{state}] {group}")
            for result in results:
                verdict = "OK" if result.get("ok") else (
                    "TIMEOUT" if result.get("timeout") else "FAIL"
                )
                error = f"  {result['error']}" if result.get("error") else ""
                print(f"  {result['name']}: {verdict} ({result['seconds']}s){error}")
        if report["missing"]:
            print(f"缺少目录接口: {', '.join(report['missing'])}")

    if report["missing"] or report["failed_groups"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
