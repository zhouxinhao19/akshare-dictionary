"""Re-test cataloged AKShare interfaces against the current environment.

catalog.json 里的 status 只是构建当天（见 meta.generated_at）的快照；
写调用前可用本脚本对候选接口（含备选）现场复测，再决定用哪个。

  python check.py stock_zh_a_hist stock_zh_a_daily
  python check.py --timeout 15 fund_etf_hist_em
  python check.py --json stock_zh_a_spot_em

每次检查跑在独立子进程里（用 catalog 固化的 params_used 真实调用一次），
超时会被硬终止；耗时包含 akshare 的导入开销。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lookup import load_catalog  # noqa: E402

CATALOG_MARK = {"success": "OK", "error": "ERR", "timeout": "TO", "unknown": "?",
                "untested": "--"}
MAX_COLUMNS = 200  # 实测列名最多记录这么多，防止超宽表把 catalog 撑爆


def build_call(item: dict[str, Any]) -> str:
    """按 catalog 固化的实参拼出调用串（位置参与关键字参都支持）。"""
    name = str(item.get("name"))
    args = item.get("args_used") or []
    kwargs = item.get("params_used") or {}
    parts = [repr(a) for a in args]
    parts += [f"{k}={v!r}" for k, v in kwargs.items()]
    return f"ak.{name}({', '.join(parts)})"


def _build_code(item: dict[str, Any]) -> str:
    return (
        "import akshare as ak\n"
        f"_df = {build_call(item)}\n"
        "_rows = len(_df)\n"
        "_ncols = len(_df.columns)\n"
        "_columns = [str(c) for c in _df.columns][:MAX_COLUMNS]\n"
    )


def _child(code: str, q: Any) -> None:
    g: dict[str, Any] = {"__name__": "__main__", "MAX_COLUMNS": MAX_COLUMNS}
    t0 = time.perf_counter()
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            exec(code, g)  # noqa: S102 - 执行的是 catalog 固化的调用样例
        q.put({"ok": True, "rows": g.get("_rows"), "ncols": g.get("_ncols"),
               "columns": g.get("_columns") or [],
               "seconds": round(time.perf_counter() - t0, 2), "error": ""})
    except BaseException as e:  # 网络失败要以结果上报，不能把主进程拖垮
        q.put({"ok": False, "rows": None, "ncols": None, "columns": [],
               "seconds": round(time.perf_counter() - t0, 2),
               "error": f"{type(e).__name__}: {e}"})


def check_one(item: dict[str, Any], timeout: float) -> dict[str, Any]:
    ctx = mp.get_context("spawn")  # 各平台行为一致，且子进程可被硬超时终止
    q = ctx.Queue()
    p = ctx.Process(target=_child, args=(_build_code(item), q), daemon=True)
    t0 = time.perf_counter()
    p.start()
    p.join(timeout)
    if p.is_alive():
        p.terminate()
        p.join(3)
        return {"name": item.get("name"), "ok": False, "timeout": True,
                "seconds": round(time.perf_counter() - t0, 2), "rows": None, "ncols": None,
                "columns": [],
                "error": f"超过 {timeout:g}s 未返回（含 akshare 导入），已终止"}
    try:
        res = q.get(timeout=5)
    except Exception:
        return {"name": item.get("name"), "ok": False, "timeout": False,
                "seconds": round(time.perf_counter() - t0, 2), "rows": None, "ncols": None,
                "columns": [],
                "error": "子进程无结果（可能在导入阶段异常退出）"}
    res["name"] = item.get("name")
    res["timeout"] = False
    return res


def main() -> None:
    parser = argparse.ArgumentParser(description="Live re-test of cataloged AKShare interfaces")
    parser.add_argument("names", nargs="+", help="接口函数名（catalog 中的 name，可一次多个）")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="单接口超时秒数（默认 30，含 akshare 导入）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    data = load_catalog()
    by_name = {str(i.get("name")): i for i in (data.get("interfaces") or [])}
    items: list[dict[str, Any]] = []
    for n in args.names:
        it = by_name.get(n)
        if it is None:
            sys.exit(f"catalog 中没有接口 {n}；先用 lookup.sh --name 确认名字")
        items.append(it)

    now_ver = "?"
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            import akshare
            now_ver = getattr(akshare, "__version__", "?")
        except Exception:
            pass
    meta = data.get("meta") or {}
    cat_ver = meta.get("akshare_version", "?")
    cat_day = str(meta.get("generated_at", ""))[:10]

    results = []
    for it in items:
        r = check_one(it, args.timeout)
        r["catalog_status"] = it.get("status")
        results.append(r)

    if args.json:
        print(json.dumps({"akshare_now": now_ver, "catalog_version": cat_ver,
                          "catalog_generated": cat_day, "results": results},
                         ensure_ascii=False, indent=2))
        return

    print(f"# 现场复测 akshare={now_ver}（catalog: {cat_ver} @{cat_day}；单次耗时含 akshare 导入）")
    for r in results:
        verdict = "OK" if r["ok"] else ("TIMEOUT" if r["timeout"] else "FAIL")
        mark = CATALOG_MARK.get(str(r.get("catalog_status")), str(r.get("catalog_status")))
        shape = f" rows={r['rows']} cols={r['ncols']}" if r["ok"] else ""
        err = f"  {r['error']}" if r.get("error") else ""
        stale = "  [与快照不一致]" if r["ok"] != (r.get("catalog_status") == "success") else ""
        print(f"{r['name']:<40} 快照={mark:<3} now={verdict:<7} {r['seconds']:>6}s{shape}{err}{stale}")


if __name__ == "__main__":
    main()
