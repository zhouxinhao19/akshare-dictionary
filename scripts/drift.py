"""Detect interface-surface drift between data/catalog.json and an akshare version.

回答"上游 akshare 发新版后，这个 skill 的 catalog 还准不准"，**不需要联网调用数据源**，
也不需要安装目标版本——可以直接拿它的 wheel/sdist 来比：

  python drift.py                       # catalog vs 已安装 akshare（内置接口表）
  python drift.py --src akshare-1.20.37-py3-none-any.whl   # catalog vs 指定发行包
  python drift.py --json                # 机器可读（CI 用）

漂移三类：
  removed   catalog 里有、新版没有（上游改名/删除，如 1.20 把 get_* 改名 futures_*）
  added     新版有、catalog 没有（上游新增接口）
  changed   两边都有但参数不同（example/签名变了，实测状态不再可信）

退出码：无漂移 0；有漂移 1（--strict 时 changed 也算，默认只有 removed/added 非空才 1）。
CI 用它决定要不要开"catalog 落后"提醒。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ak_registry import (installed_version, load_registry,  # noqa: E402
                         normalize_records, resolve_registry_path)
import re

SKILL_DIR = Path(__file__).resolve().parent.parent
CATALOG = SKILL_DIR / "data" / "catalog.json"

# registry 里 desc 内嵌默认值的抽取（与 ak_registry 保持一致口径）
_POS_ARG_RE = re.compile(r"^\s*(\w+)\s*=\s*(.+?)\s*$")


def _clean_params(params: list) -> list:
    """过滤 None/'-' 占位名，文档抽取与接口表两侧口径对齐。"""
    return [str(p.get("name")) for p in (params or [])
            if p.get("name") not in (None, "-", "")]


def catalog_fingerprints(path: Path | None = None) -> tuple[dict, dict]:
    data = json.loads((path or CATALOG).read_text(encoding="utf-8"))
    out = {}
    for i in data.get("interfaces") or []:
        out[str(i.get("name"))] = {
            "params": _clean_params(i.get("params")),
            "used": i.get("params_used") or {},
            "status": i.get("status"),
            "category": i.get("category"),
        }
    return out, data.get("meta") or {}


def registry_fingerprints(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for r in records:
        out[str(r.get("name"))] = {
            "params": _clean_params(r.get("params")),
            "used": r.get("params_used") or {},
            "category": r.get("category"),
            "desc": r.get("desc", "")[:70],
        }
    return out


def compare(cat: dict[str, dict], reg: dict[str, dict]) -> dict[str, Any]:
    cat_names, reg_names = set(cat), set(reg)
    removed = sorted(cat_names - reg_names)
    added = sorted(reg_names - cat_names)
    changed = []
    for name in sorted(cat_names & reg_names):
        a, b = cat[name]["params"], reg[name]["params"]
        # 文档抽取与接口表对顺序口径不同；catalog 全部用 kwargs 调用，按集合比降噪
        if set(x for x in a if x) != set(x for x in b if x):
            changed.append({"name": name,
                            "catalog_params": a,
                            "registry_params": b})
    return {
        "removed": removed,
        "added": added,
        "changed": changed,
        "counts": {"removed": len(removed), "added": len(added),
                   "changed": len(changed),
                   "catalog_total": len(cat_names), "registry_total": len(reg_names)},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Detect catalog vs akshare registry drift")
    ap.add_argument("--src", type=Path,
                    help="目标版本的 interfaces.json/.whl/.tar.gz；默认用已安装 akshare")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--catalog", type=Path, default=CATALOG,
                    help=f"要检出的 catalog（默认 {CATALOG}）")
    ap.add_argument("--strict", action="store_true", help="参数变更也算漂移（退出码 1）")
    args = ap.parse_args()

    if not args.catalog.exists():
        sys.exit(f"catalog not found: {args.catalog}")
    cat, meta = catalog_fingerprints(args.catalog)

    if args.src is not None:
        path = resolve_registry_path(args.src)
        data = json.loads(path.read_text(encoding="utf-8"))
        records = normalize_records(data.get("interfaces") or [])
        guess = re.search(r"(\d+\.\d+[\w.]*)", args.src.name)
        reg_version = (str(data.get("akshare_version") or "")
                       or (guess.group(1) if guess else "unknown"))
    else:
        records, rmeta = load_registry(None)
        reg_version = installed_version() or rmeta.get("akshare_version", "?")

    reg_fp = registry_fingerprints(records)
    report = compare(cat, reg_fp)
    report["catalog_version"] = meta.get("akshare_version")
    report["registry_version"] = reg_version
    drifted = report["counts"]["removed"] > 0 or report["counts"]["added"] > 0
    if args.strict and report["counts"]["changed"] > 0:
        drifted = True
    report["version_mismatch"] = report["catalog_version"] != report["registry_version"]
    report["drifted"] = drifted

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(f"# catalog={report['catalog_version']}  target={report['registry_version']}  "
              f"{'⚠️ 版本不一致' if report['version_mismatch'] else '版本一致'}")
        c = report["counts"]
        print(f"# removed={c['removed']}  added={c['added']}  "
              f"params-changed={c['changed']}  (catalog {c['catalog_total']} / "
              f"registry {c['registry_total']})")
        for name in report["removed"][:15]:
            print(f"  - {name}  (catalog status={cat[name]['status']})")
        for name in report["added"][:15]:
            print(f"  + {name}  ({reg_fp[name]['desc']})")
        for ch in report["changed"][:10]:
            print(f"  ~ {ch['name']}: {ch['catalog_params']} -> {ch['registry_params']}")
        if c["removed"] > 15:
            print(f"  ... {c['removed']-15} more removed")
        if c["added"] > 15:
            print(f"  ... {c['added']-15} more added")
        if drifted:
            print("# 有漂移：建议 bash scripts/refresh.sh 同步 catalog")
        else:
            print("# 无漂移")
    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main())
