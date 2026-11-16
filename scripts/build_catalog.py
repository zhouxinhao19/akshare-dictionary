"""Rebuild data/catalog.json.

两种来源，自动识别：

  python build_catalog.py                    # 默认：已安装 akshare 的内置接口表（1.20+）
  python build_catalog.py --src <path>       # 显式指定：interfaces.json / .whl / .tar.gz
  python build_catalog.py --src <dict.json>  # 兼容：旧字典仓库的 akshare_dictionary.json

关键行为：**实测结果按"来源接口 + 参数指纹"继承**。继承分两步：
  1. 同名接口且测试参数没变 —— 直接沿用上一版的 status/columns/row_count；
  2. 接口被上游改名（akshare 1.20.44 起内置 data/renames.json）—— 按改名表追到
     旧名，若旧名存在且参数指纹一致，同样沿用它的实测结果。
只有真正新增的接口、以及参数变了的接口才会标成 untested，所以上游发新版时重建是
秒级的，只有真正可能失效的接口需要 retest。

  --keep-status   继承上一版全部实测状态（最省，但参数已变的接口状态会过时）
  --no-inherit    全部标 untested（强制全量重测）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ak_registry import flatten_renames, load_registry, load_renames  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
OUT = SKILL_DIR / "data" / "catalog.json"
LEGACY_INTERFACES = ("name", "category", "desc", "url", "limit_desc", "params",
                     "outputs", "example", "test")


def _fingerprint(args: list, kwargs: dict) -> str:
    return json.dumps([args, kwargs], ensure_ascii=False, sort_keys=True)


def _slim_error(err: object) -> str | None:
    if not err:
        return None
    text = str(err)
    return text[:240] + "..." if len(text) > 240 else text


def _legacy_to_records(src: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """旧字典仓库 akshare_dictionary.json → 统一中间结构（保留它实测出来的东西）。"""
    data = json.loads(src.read_text(encoding="utf-8"))
    out = []
    for it in data.get("interfaces") or []:
        test = it.get("test") or {}
        used = test.get("params_used") or {}
        out.append({
            "name": str(it.get("name")),
            "category": str(it.get("category") or ""),
            "desc": it.get("desc") or "",
            "url": it.get("url") or "",
            "limit": it.get("limit_desc") or "",
            "params": [
                {"name": p.get("name"), "type": p.get("type"), "desc": p.get("desc")}
                for p in (it.get("params") or [])
                if p.get("name") not in (None, "-", "")
            ],
            "outputs": [
                {"name": o.get("name"), "type": o.get("type"),
                 "desc": None if o.get("desc") in (None, "-") else o.get("desc")}
                for o in (it.get("outputs") or [])
                if o.get("name") not in (None, "-", "")
            ],
            "example": it.get("example") or "",
            "rate_limit": it.get("rate_limit"),
            "documented": bool(it.get("documented", True)),
            "args_used": test.get("args_used") or [],
            "params_used": used,
            "params_source": "legacy-test" if used else "legacy",
            "_tested": {
                "status": test.get("status") or "unknown",
                "columns": test.get("columns") or [],
                "row_count": test.get("row_count"),
                "columns_match_doc": test.get("columns_match_doc"),
                "error": _slim_error(test.get("error")),
            },
        })
    meta = dict(data.get("meta") or {})
    meta["kind"] = "legacy-dictionary"
    return out, meta


def load_records(src: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按文件内容/后缀判断走 registry 路径还是旧字典路径。"""
    if src is not None and src.exists() and src.suffix == ".json":
        try:
            head = json.loads(src.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            head = None
        if isinstance(head, dict) and "interfaces" in head:
            sample = (head["interfaces"] or [{}])[0]
            if "test" in sample or any(k in sample for k in LEGACY_INTERFACES) \
                    and "schema_version" not in head:
                return _legacy_to_records(src)
    return load_registry(src)


STATUS_FIELDS = ("status", "columns", "row_count", "columns_match_doc",
                 "error", "tested_at", "test_seconds")


def _adopt(rec: dict[str, Any], old: dict[str, Any], source: str) -> None:
    for key in STATUS_FIELDS:
        if key in old:
            rec[key] = old[key]
    rec["status_source"] = source
    # 记录实测状态是从哪个接口名继承来的，改名后仍能追溯到原始实测
    if source == "renamed":
        rec["status_inherited_from"] = old.get("name")


def inherit_previous(records: list[dict[str, Any]], mode: str,
                     renames: dict[str, str] | None = None) -> dict[str, int]:
    """把上一版 catalog 的实测状态按参数指纹继承下来；返回统计。

    renames 是压平后的 旧名 → 当前名 映射（见 ak_registry.flatten_renames）；
    接口被上游改名时，用旧名的实测结果填新名的状态，避免白白重测几百个接口。
    """
    if not OUT.exists():
        return {"no_previous": len(records)}
    prev = json.loads(OUT.read_text(encoding="utf-8"))
    prev_by_name = {str(i.get("name")): i for i in (prev.get("interfaces") or [])}
    # 反向映射：当前名 → 上一版里的旧名（可能多个旧名并到一个新名，取第一个有实测的）
    old_names: dict[str, list[str]] = {}
    for old_name, new_name in (renames or {}).items():
        if old_name != new_name and old_name in prev_by_name:
            old_names.setdefault(new_name, []).append(old_name)

    stats = {"inherited": 0, "stale_params": 0, "new": 0, "dropped_status": 0,
             "renamed": 0}
    for rec in records:
        tested = rec.pop("_tested", None)
        if tested:
            rec.update(tested)
            rec["status_source"] = "legacy"
            stats["inherited"] += 1
            continue

        hit = prev_by_name.get(rec["name"])
        source = "inherited"
        if hit is None:
            # 没有同名接口：看它是不是旧接口改名来的
            for old_name in old_names.get(rec["name"], []):
                cand = prev_by_name[old_name]
                if cand.get("status") and cand.get("status") != "untested":
                    hit, source = cand, "renamed"
                    break
            if hit is None:
                stats["new"] += 1
                rec["status"] = "untested"
                continue

        same_params = (_fingerprint(hit.get("args_used") or [],
                                   hit.get("params_used") or {})
                       == _fingerprint(rec.get("args_used") or [],
                                       rec.get("params_used") or {}))
        # 接口改过名时参数口径可能整体变过（如 stock_zh_a_hist -> *_em 的 start/end
        # 默认值不同），这类"参数变了"不必然意味着旧实测失效，但也不能假装没变：
        # 仍按 stale 处理，交给 retest，避免把错的列名继承到新名下。
        keep = mode == "keep-status" or same_params
        if keep and hit.get("status") and hit.get("status") != "untested":
            _adopt(rec, hit, source)
            stats[source] += 1
        else:
            rec["status"] = "untested"
            stats["stale_params" if not same_params else "dropped_status"] += 1
    if mode == "no-inherit":
        for rec in records:
            rec["status"] = "untested"
    return stats


def build(src: Path | None, mode: str) -> dict:
    records, src_meta = load_records(src)
    renamed_raw = load_renames(src)
    renames = flatten_renames(renamed_raw["renamed"])
    stats = inherit_previous(records, mode, renames)
    now = datetime.now(timezone.utc).isoformat()
    for rec in records:
        rec["example"] = rec.get("example") or ""
    counts: dict[str, int] = {}
    for rec in records:
        counts[rec.get("status") or "unknown"] = counts.get(rec.get("status") or "unknown", 0) + 1
    categories = {}
    for rec in records:
        cat = rec.get("category") or "uncategorized"
        cur = categories.setdefault(cat, {"total": 0, "desc": ""})
        cur["total"] += 1
    interfaces = []
    for rec in records:
        interfaces.append({
            "name": rec["name"],
            "category": rec["category"],
            "desc": rec["desc"],
            "url": rec["url"],
            "limit": rec["limit"],
            "params": rec["params"],
            "outputs": rec["outputs"],
            "example": rec["example"],
            "status": rec.get("status") or "untested",
            "args_used": rec.get("args_used") or [],
            "params_used": rec.get("params_used") or {},
            "params_source": rec.get("params_source"),
            "columns": rec.get("columns") or [],
            "row_count": rec.get("row_count"),
            "columns_match_doc": rec.get("columns_match_doc"),
            "error": rec.get("error"),
            "tested_at": rec.get("tested_at"),
            "status_source": rec.get("status_source"),
            "status_inherited_from": rec.get("status_inherited_from"),
            "rate_limit": rec.get("rate_limit"),
            "documented": rec.get("documented", True),
        })
    interfaces.sort(key=lambda i: (str(i["category"]), str(i["name"])))
    # 改名对照写进 catalog：LLM 看到旧名时能直接查到新名，不必先 import akshare 才发现
    renamed_pairs = {old: new for old, new in sorted(renames.items()) if old != new}
    meta = {
        "akshare_version": src_meta.get("akshare_version", "unknown"),
        "generated_at": now,
        "total": len(interfaces),
        "status_counts": counts,
        "source": {
            "kind": src_meta.get("kind", "akshare-registry"),
            "registry_path": src_meta.get("registry_path"),
            "schema_version": src_meta.get("schema_version"),
        },
        "inherit": {"mode": mode, **stats},
        "renames": {
            "renamed": renamed_pairs,
            "removed": dict(sorted((renamed_raw["removed"] or {}).items())),
        },
    }
    return {"meta": meta, "categories": categories, "interfaces": interfaces}


def main() -> None:
    ap = argparse.ArgumentParser(description="Rebuild data/catalog.json")
    ap.add_argument("--src", type=Path, default=None,
                    help="来源：默认用已安装 akshare 的内置接口表；"
                         "可给 interfaces.json / .whl / .tar.gz / 旧版 akshare_dictionary.json")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--keep-status", action="store_true",
                    help="无条件继承上一版全部实测状态（最快，参数变更的接口状态可能过时）")
    ap.add_argument("--no-inherit", action="store_true",
                    help="不继承任何状态，全部标 untested（配合 retest 做全量重测）")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写盘")
    args = ap.parse_args()

    mode = "no-inherit" if args.no_inherit else ("keep-status" if args.keep_status else "auto")
    catalog = build(args.src, mode)
    meta = catalog["meta"]
    counts = meta["status_counts"]
    print(f"# akshare={meta['akshare_version']}  source={meta['source']['kind']}  "
          f"interfaces={meta['total']}")
    print("# status: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    inh = meta["inherit"]
    print(f"# 继承实测状态: inherited={inh.get('inherited', 0)} "
          f"改名继承={inh.get('renamed', 0)} "
          f"参数变更需重测={inh.get('stale_params', 0)} "
          f"新增={inh.get('new', 0)} 无上一版={inh.get('no_previous', 0)}")
    ren = meta["renames"]["renamed"]
    print(f"# 改名对照: renamed={len(ren)}  removed={len(meta['renames']['removed'])}")
    if args.dry_run:
        print("--dry-run: 未写盘")
        return
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    print(f"wrote {args.out}  interfaces={meta['total']}  bytes={args.out.stat().st_size}")


if __name__ == "__main__":
    main()
