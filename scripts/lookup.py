"""Search the compact AKShare interface catalog.

Examples:
  python lookup.py 日线 A股
  python lookup.py --category stock 分红
  python lookup.py --name stock_zh_a_hist
  python lookup.py --status success 期货 库存
  python lookup.py --json 宏观 CPI
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

CATALOG = Path(__file__).resolve().parent.parent / "data" / "catalog.json"
STATUS_MARK = {"success": "OK", "error": "ERR", "timeout": "TO", "unknown": "?"}
CAT_LABELS = {
    "stock": "股票 a股 沪深 行情",
    "stock_feature": "股票 特征",
    "stock_fundamental": "股票 基本面 财务",
    "futures": "期货 商品 合约",
    "futures_derivative": "衍生品",
    "option": "期权",
    "fund": "基金 etf 净值",
    "index": "指数 股指",
    "bond": "债券 国债 可转债",
    "macro": "宏观 经济",
    "interest_rate": "利率 国债收益率",
    "fx": "外汇 汇率",
    "currency": "货币 汇率",
    "spot": "现货",
    "energy": "能源",
    "reits": "reits 房地产信托",
    "qdii": "qdii",
    "cal": "日历 交易日",
    "others": "其他",
    "tool": "工具",
}
CAT_ALIAS = {
    "股票": "stock",
    "a股": "stock",
    "期货": "futures",
    "期权": "option",
    "基金": "fund",
    "etf": "fund",
    "指数": "index",
    "债券": "bond",
    "可转债": "bond",
    "宏观": "macro",
    "经济": "macro",
    "利率": "interest_rate",
    "外汇": "fx",
    "汇率": "fx",
    "现货": "spot",
    "能源": "energy",
    "日历": "cal",
}
SYNONYMS = {
    "日线": ["日线", "日频", "日频率"],
    "周线": ["周线", "周频", "weekly"],
    "月线": ["月线", "月频", "monthly"],
    "分钟": ["分钟", "分时", "min"],
    "实时": ["实时", "spot"],
    "a股": ["a股", "沪深京", "stock_zh_a"],
    "股票": ["股票", "stock"],
    "宏观": ["宏观", "macro", "经济"],
    "期货": ["期货", "futures"],
    "期权": ["期权", "option"],
    "基金": ["基金", "fund", "etf"],
    "指数": ["指数", "index"],
    "债券": ["债券", "bond", "可转债"],
    "外汇": ["外汇", "fx", "汇率"],
    "gdp": ["gdp", "国内生产"],
    "cpi": ["cpi", "居民消费"],
    "ppi": ["ppi", "工业生产者"],
    "k线": ["k线", "行情", "hist"],
}


def load_catalog() -> dict[str, Any]:
    if not CATALOG.exists():
        sys.exit(f"catalog not found: {CATALOG}")
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _fold(text: str) -> str:
    return "".join(str(text).lower().split())


def _alts(token: str) -> list[str]:
    t = _fold(token)
    return [_fold(x) for x in SYNONYMS.get(t, [token])]


def haystack(item: dict[str, Any]) -> str:
    parts: list[str] = [
        str(item.get("name") or ""),
        str(item.get("category") or ""),
        CAT_LABELS.get(str(item.get("category") or ""), ""),
        str(item.get("desc") or ""),
        str(item.get("limit") or ""),
    ]
    for p in item.get("params") or []:
        parts.append(str(p.get("name") or ""))
        parts.append(str(p.get("desc") or ""))
    for o in item.get("outputs") or []:
        parts.append(str(o.get("name") or ""))
        parts.append(str(o.get("desc") or ""))
    parts.extend(str(c) for c in (item.get("columns") or []))
    return _fold(" ".join(parts))


def _hit(blob: str, name: str, desc: str, token: str) -> int:
    alts = _alts(token)
    if any(a == name for a in alts):
        return 100
    if any(name.startswith(a) or a in name for a in alts if a):
        return 70
    if any(a in desc for a in alts if a):
        return 40
    if any(a in blob for a in alts if a):
        return 20
    return 0


def score(item: dict[str, Any], tokens: list[str], name_query: str | None) -> int:
    name = _fold(item.get("name") or "")
    desc = _fold(item.get("desc") or "")
    blob = haystack(item)
    s = 0
    if name_query:
        nq = _fold(name_query)
        if name == nq:
            s += 200
        elif nq in name:
            s += 120
        elif nq in blob:
            s += 40
        else:
            return -1
    for tok in tokens:
        if not _fold(tok):
            continue
        pts = _hit(blob, name, desc, tok)
        if pts == 0:
            return -1
        s += pts
    qfold = _fold(" ".join(tokens))
    foreign = ("usa", "uk", "canada", "germany", "swiss", "japan", "euro", "france", "australia", "korea", "香港", "hk")
    if not any(f in qfold for f in foreign):
        if "china" in name or "_zh_a" in name or name.startswith("macro_china"):
            s += 12
    if item.get("status") == "success":
        s += 8
    elif item.get("status") == "timeout":
        s += 2
    return s


def format_params(item: dict[str, Any]) -> str:
    params = item.get("params") or []
    if not params:
        return "(无参)"
    bits = []
    for p in params:
        bit = p.get("name") or ""
        if p.get("type") and p["type"] not in ("-",):
            bit += f":{p['type']}"
        if p.get("desc") and p["desc"] not in ("-",):
            desc = p["desc"].replace("\n", " ")
            if len(desc) > 80:
                desc = desc[:80] + "..."
            bit += f"  # {desc}"
        bits.append(bit)
    return "\n    ".join(bits)


def format_columns(item: dict[str, Any]) -> str:
    cols = [str(c) for c in (item.get("columns") or [])]
    if cols:
        tag = "实测列"
        if item.get("columns_match_doc") is False:
            tag += "（与文档不一致，以实测为准）"
        return f"{tag}: " + ", ".join(cols)
    outs = [o.get("name") for o in (item.get("outputs") or []) if o.get("name")]
    if outs:
        return "文档列: " + ", ".join(outs)
    return "列: (未知)"


def print_brief(item: dict[str, Any]) -> None:
    st = STATUS_MARK.get(item.get("status") or "", item.get("status") or "?")
    desc = (item.get("desc") or "").replace("\n", " ")
    if len(desc) > 72:
        desc = desc[:72] + "..."
    cat = str(item.get("category") or "")
    name = str(item.get("name") or "")
    print(f"[{st}] {cat:<18} {name:<42} {desc}")


def print_detail(item: dict[str, Any]) -> None:
    print("=" * 72)
    print(f"{item.get('name')}  [{item.get('status')}]  category={item.get('category')}")
    if item.get("desc"):
        print(f"desc: {item['desc']}")
    if item.get("limit"):
        print(f"limit: {item['limit']}")
    if item.get("url"):
        print(f"url: {item['url']}")
    print(f"params:\n    {format_params(item)}")
    used = item.get("params_used") or {}
    if used:
        print(f"params_used: {json.dumps(used, ensure_ascii=False)}")
    print(format_columns(item))
    if item.get("row_count") is not None:
        print(f"row_count: {item['row_count']}")
    if item.get("error"):
        print(f"error: {item['error']}")
    if item.get("example"):
        print("example:")
        print(item["example"])
    print("=" * 72)


def main() -> None:
    parser = argparse.ArgumentParser(description="Search AKShare interface catalog")
    parser.add_argument("keywords", nargs="*", help="AND-matched tokens")
    parser.add_argument("--name", help="function name (exact or substring)")
    parser.add_argument("--category", help="filter by category, e.g. stock/futures/macro")
    parser.add_argument(
        "--status",
        choices=["success", "error", "timeout", "unknown"],
        help="filter by last test status",
    )
    parser.add_argument("--limit", type=int, default=12, help="max brief hits (default 12)")
    parser.add_argument("--json", action="store_true", help="print matches as JSON")
    parser.add_argument(
        "--detail",
        action="store_true",
        help="print full records (auto if --name unique or 1 hit)",
    )
    args = parser.parse_args()

    if not args.keywords and not args.name and not args.category:
        parser.error("need keywords, --name, or --category")

    data = load_catalog()
    items: list[dict[str, Any]] = data.get("interfaces") or []
    if args.category:
        raw = _fold(args.category)
        cat = CAT_ALIAS.get(raw, raw)
        if cat in CAT_LABELS:
            pass
        elif raw in {_fold(k) for k in CAT_LABELS}:
            cat = raw
        else:
            cat = args.category.lower()
        if cat == "stock":
            items = [i for i in items if str(i.get("category") or "").startswith("stock")]
        else:
            items = [i for i in items if (i.get("category") or "").lower() == cat]
    if args.status:
        items = [i for i in items if i.get("status") == args.status]

    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        s = score(item, args.keywords, args.name)
        if s >= 0:
            ranked.append((s, item))
    ranked.sort(key=lambda x: (-x[0], x[1].get("name") or ""))

    if not ranked:
        print("NO_MATCH")
        print("Try fewer tokens, drop --status, or search category aliases (stock/futures/fund/macro/index/option/bond/fx).")
        sys.exit(1)

    hits = [it for _, it in ranked]
    if args.name:
        exact = [h for h in hits if (h.get("name") or "") == args.name]
        if not exact:
            exact = [h for h in hits if _fold(h.get("name") or "") == _fold(args.name)]
        if exact:
            hits = exact
    show_detail = args.detail or bool(args.name) or len(hits) == 1
    brief_hits = hits if show_detail else hits[: args.limit]

    if args.json:
        payload = brief_hits if show_detail else hits[: args.limit]
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return

    meta = data.get("meta") or {}
    print(
        f"# akshare {meta.get('akshare_version', '?')}  "
        f"catalog={meta.get('total', len(items))}  "
        f"hits={len(hits)}  generated={meta.get('generated_at', '')[:10]}"
    )
    if not show_detail:
        print("# status: OK=success  ERR=error  TO=timeout")
        for item in brief_hits:
            print_brief(item)
        if len(hits) > args.limit:
            print(f"... {len(hits) - args.limit} more. Narrow keywords or pass --name <fn>.")
        print("# for params/columns: bash lookup.sh --name <function>")
        return

    cap = hits[: min(len(hits), args.limit if args.name or args.detail else 3)]
    if len(hits) > len(cap) and not args.name:
        print(f"# showing {len(cap)}/{len(hits)} details; pass --name to pin one")
    for item in cap:
        print_detail(item)


if __name__ == "__main__":
    main()
