"""Detect interface-surface drift between data/catalog.json and an akshare version.

回答"上游 akshare 发新版后，这个 skill 的 catalog 还准不准"，**不需要联网调用数据源**，
也不需要安装目标版本——可以直接拿它的 wheel/sdist 来比：

  python drift.py                       # catalog vs 已安装 akshare（内置接口表）
  python drift.py --src akshare-1.20.65-py3-none-any.whl   # catalog vs 指定发行包
  python drift.py --json                # 机器可读（CI 用）

漂移四类（旧名的去向优先查 akshare 内置的 data/renames.json）：
  renamed   catalog 里有、新版改名了（如 1.20.44 把 stock_zh_index_daily -> index_zh_daily_sina）
  removed   catalog 里有、新版彻底没了（改名表里记为 removed）
  added     catalog 没有、新版新增（扣除改名目标后剩下的才是真新增）
  changed   两边都有但参数不同（example/签名变了，实测状态不再可信）

退出码：无漂移 0；有漂移 1（--strict 时 changed 也算，默认只有 removed/added 非空才 1）。
改名不算"落后"——它能被 build_catalog.py 自动继承实测状态，所以单独归类、不影响退出码。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ak_registry import (flatten_renames, installed_version, load_registry,  # noqa: E402
                         load_renames, normalize_records, resolve_registry_path,
                         version_key)
from lookup import catalog_removed, catalog_renames  # noqa: E402

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


def compare(cat: dict[str, dict], reg: dict[str, dict],
            renames: dict[str, str] | None = None,
            removed_names: dict[str, Any] | None = None) -> dict[str, Any]:
    cat_names, reg_names = set(cat), set(reg)
    renames = renames or {}
    removed_names = removed_names or {}

    # 上一版的名字里，哪些其实是改名了：旧名 -> 新名，且新名确实在新版里
    renamed = {old: renames[old] for old in cat_names - reg_names
               if renames.get(old) in reg_names}
    # 既没改名、也不在新版里的旧名——这些是真的没了
    gone = cat_names - reg_names - set(renamed)
    # 旧名既没改名、也没被 akshare 登记为移除的，仍按移除处理，
    # 但单独标 unregistered，方便判断是上游漏登记还是 catalog 有问题
    removed = sorted(gone)
    unregistered = sorted(n for n in gone if n not in removed_names)
    # 新名里扣掉改名目标，剩下的才是真正新增的接口
    rename_targets = {n for n in renamed.values() if n in reg_names}
    added = sorted(reg_names - cat_names - rename_targets)

    changed = []
    for name in sorted(cat_names & reg_names):
        a, b = cat[name]["params"], reg[name]["params"]
        # 文档抽取与接口表对顺序口径不同；catalog 全部用 kwargs 调用，按集合比降噪
        if set(x for x in a if x) != set(x for x in b if x):
            changed.append({"name": name,
                            "catalog_params": a,
                            "registry_params": b})
    return {
        "renamed": renamed,
        "removed": removed,
        "unregistered": unregistered,
        "added": added,
        "changed": changed,
        "counts": {"renamed": len(renamed), "removed": len(removed), "added": len(added),
                   "changed": len(changed), "unregistered": len(unregistered),
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
        rename_src: Path | None = args.src
    else:
        records, rmeta = load_registry(None)
        reg_version = installed_version() or rmeta.get("akshare_version", "?")
        rename_src = None

    # 目标版本的改名/移除对照表：旧名的去向以它为准，而不是靠"名字消失了"去猜。
    # 目标版本没带 renames.json（<1.20.44）时，退回 catalog 里固化的对照表——
    # 那份至少能说明"这些名字曾经改成什么"，仍比空表有用。
    rename_data = load_renames(rename_src)
    if not rename_data["renamed"] and not rename_data["removed"]:
        rename_data = {"renamed": catalog_renames(), "removed": catalog_removed()}
    flat = flatten_renames(rename_data["renamed"])

    reg_fp = registry_fingerprints(records)
    report = compare(cat, reg_fp, flat, rename_data["removed"])
    report["catalog_version"] = meta.get("akshare_version")
    report["registry_version"] = reg_version
    # 改名能被 build_catalog.py 自动继承实测状态，不算 catalog 落后；
    # 只有真移除 / 真新增才需要维护者动手
    drifted = report["counts"]["removed"] > 0 or report["counts"]["added"] > 0
    if args.strict and report["counts"]["changed"] > 0:
        drifted = True
    report["version_mismatch"] = report["catalog_version"] != report["registry_version"]
    report["target_older"] = version_key(report["registry_version"]) < version_key(
        report["catalog_version"])
    report["drifted"] = drifted

    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(f"# catalog={report['catalog_version']}  target={report['registry_version']}  "
              f"{'⚠️ 版本不一致' if report['version_mismatch'] else '版本一致'}")
        c = report["counts"]
        older = report["target_older"]
        print(f"# renamed={c['renamed']}  removed={c['removed']}  added={c['added']}  "
              f"params-changed={c['changed']}  (catalog {c['catalog_total']} / "
              f"registry {c['registry_total']})")
        if older:
            # 目标版本比 catalog 旧时，removed/added 的含义是反的，逐条列出来只会误导
            print("# ⚠️ 目标版本比 catalog 旧：上面的 removed/added 方向相反——"
                  "新版才有的接口会算进 removed，catalog 独有的会算进 added。")
            print("# 这种比较只能确认版本差异；要同步 catalog 请指向较新的发行包。")
        else:
            shown = 0
            for old, new in report["renamed"].items():
                if shown >= 10:
                    break
                print(f"  > {old} -> {new}")
                shown += 1
            if c["renamed"] > shown:
                print(f"  ... {c['renamed'] - shown} more renamed（这些能自动继承实测状态）")
            for name in report["removed"][:15]:
                tag = ("" if name not in report["unregistered"]
                       else "  [上游未登记，确认是否真移除]")
                print(f"  - {name}  (catalog status={cat[name]['status']}){tag}")
            for name in report["added"][:15]:
                print(f"  + {name}  ({reg_fp[name]['desc']})")
            for ch in report["changed"][:10]:
                print(f"  ~ {ch['name']}: {ch['catalog_params']} -> {ch['registry_params']}")
            if c["removed"] > 15:
                print(f"  ... {c['removed']-15} more removed")
            if c["added"] > 15:
                print(f"  ... {c['added']-15} more added")
            if drifted:
                print("# 有漂移：跑 bash scripts/build_catalog.sh --src <新版本 .whl> 同步 catalog"
                      "（改名接口的实测状态会自动继承，只需 retest 参数变了的）")
            elif c["renamed"]:
                print("# 只有改名：build_catalog 能自动继承，建议仍同步一次让 catalog 用上新名")
            else:
                print("# 无漂移")
    return 1 if (drifted and not report["target_older"]) else 0


if __name__ == "__main__":
    sys.exit(main())
