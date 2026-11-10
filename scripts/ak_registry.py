"""Read the interface registry shipped inside akshare, without importing akshare.

akshare 1.20+ 自带机器可读接口表 `akshare/data/interfaces.json`（schema_version 1，
约 1072 条：name/category/desc/params/outputs/example/url/rate_limit/limit_desc）。
这意味着**接口清单、参数、文档列本身不再需要联网实测就能拿到**——只有
`status` / `columns` / `row_count` 必须现场调用才有。本模块把这层读取能力统一提供给
build_catalog.py / retest.py，并允许从 wheel/sdist 或解出的 json 读，
以便在不安装某个版本的前提下比对版本漂移。

用法：
  python ak_registry.py                 # 当前环境的 registry 摘要
  python ak_registry.py --src <path>    # 指定 json/wheel/sdist 路径
  python ak_registry.py --dump out.json # 归一化后的记录写盘
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import importlib.util
import json
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

DEFAULT_PARAM_RE = re.compile(r"^\s*(\w+)\s*=\s*(.+?)(?:[；;]|$)")


def installed_version() -> str | None:
    """已安装 akshare 的版本号；不 import 它。"""
    try:
        return importlib.metadata.version("akshare")
    except importlib.metadata.PackageNotFoundError:
        return None


def find_registry_json() -> Path | None:
    """已安装 akshare 包内的 data/interfaces.json；未安装或旧版（<1.20）返回 None。"""
    try:
        spec = importlib.util.find_spec("akshare")
    except (ImportError, ValueError):
        return None
    if not spec or not spec.submodule_search_locations:
        return None
    for loc in spec.submodule_search_locations:
        cand = Path(loc) / "data" / "interfaces.json"
        if cand.exists():
            return cand
    return None


def _extract_from_archive(src: Path, workdir: Path) -> Path | None:
    """从 wheel(.whl) 或 sdist(.tar.gz) 里取出 data/interfaces.json。"""
    if src.suffix == ".whl":
        with zipfile.ZipFile(src) as z:
            names = [n for n in z.namelist() if n.endswith("/data/interfaces.json")]
            if not names:
                return None
            target = workdir / "interfaces.json"
            with z.open(names[0]) as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
            return target
    if src.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(src, "r:*") as t:
            members = [m for m in t.getmembers()
                       if m.isfile() and m.name.endswith("/data/interfaces.json")]
            if not members:
                return None
            extracted = t.extractfile(members[0])
            if extracted is None:
                return None
            target = workdir / "interfaces.json"
            target.write_bytes(extracted.read())
            return target
    return None


def resolve_registry_path(src: Path | None = None) -> Path:
    """把"任意来源"解析成一个真实的 interfaces.json 路径。"""
    if src is None:
        found = find_registry_json()
        if found is None:
            sys.exit(
                "未找到 akshare 内置接口表（需要 akshare>=1.20，或显式传 --src）。\n"
                f"  已安装版本：{installed_version() or '无'}\n"
                "  可用来源：--src <interfaces.json|.whl|.tar.gz>，"
                "或旧版字典仓库的 akshare_dictionary.json"
            )
        return found
    if not src.exists():
        sys.exit(f"source not found: {src}")
    if src.suffix == ".json":
        return src
    workdir = Path(tempfile.mkdtemp(prefix="akdict-reg-"))
    got = _extract_from_archive(src, workdir)
    if got is None:
        sys.exit(f"{src.name} 里没有 data/interfaces.json（不是 akshare>=1.20 的发行包？）")
    return got


def example_params(example: str, name: str) -> tuple[list, dict] | None:
    """从 example 源码里 AST 解析 `ak.<name>(...)` 的实参；失败返回 None。

    命中调用即算成功，**包括零参调用**（`ak.foo()` 本身就是官方给的可用调用）。
    """
    if not example:
        return None
    try:
        tree = ast.parse(example)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)):
            continue
        if fn.value.id != "ak" or fn.attr != name:
            continue
        try:
            args = [ast.literal_eval(a) for a in node.args]
            kwargs = {kw.arg: ast.literal_eval(kw.value)
                      for kw in node.keywords if kw.arg}
        except (ValueError, SyntaxError):
            return None
        return args, kwargs
    return None


def _desc_defaults(params: list[dict[str, Any]]) -> dict[str, Any]:
    """params[].desc 常写成 `symbol='603777'；股票代码…`，把默认值抠出来。"""
    out: dict[str, Any] = {}
    for p in params:
        name, desc = p.get("name"), p.get("desc") or ""
        if not name or name in out:
            continue
        m = DEFAULT_PARAM_RE.match(desc)
        if not m or m.group(1) != name:
            continue
        raw = m.group(2).strip().rstrip("，,。")
        try:
            out[name] = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            out[name] = raw.strip("'\"")
    return out


def derive_params(record: dict[str, Any]) -> tuple[list, dict, str]:
    """实测参来源优先级：example 里的真实调用 > desc 内嵌默认值 > 无参。

    返回 (args, kwargs, 来源)。example 命中即采用——它本身就是官方给的可用调用写法，
    零参（`ak.foo()`）也算，不要退回去猜 desc。
    """
    got = example_params(record.get("example") or "", str(record.get("name") or ""))
    if got is not None:
        args, kwargs = got
        return args, kwargs, "example"
    defaults = _desc_defaults(record.get("params") or [])
    if defaults:
        return [], defaults, "desc"
    return [], {}, "none" if not (record.get("params") or []) else "no-default"


def normalize_records(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """akshare registry 记录 → 本 skill 的统一中间结构。"""
    out = []
    for r in raw:
        name = r.get("name")
        if not name:
            continue
        args, kwargs, psrc = derive_params(r)
        out.append({
            "name": str(name),
            "category": str(r.get("category") or ""),
            "desc": r.get("desc") or "",
            "url": r.get("url") or "",
            "limit": r.get("limit_desc") or "",
            "params": [
                {"name": p.get("name"), "type": p.get("type"), "desc": p.get("desc")}
                for p in (r.get("params") or []) if p.get("name")
            ],
            "outputs": [
                {"name": o.get("name"), "type": o.get("type"),
                 "desc": None if o.get("desc") in (None, "-") else o.get("desc")}
                for o in (r.get("outputs") or []) if o.get("name")
            ],
            "example": r.get("example") or "",
            "rate_limit": r.get("rate_limit"),
            "documented": bool(r.get("documented")),
            "args_used": args,
            "params_used": kwargs,
            "params_source": psrc,
        })
    return out


def load_registry(src: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读 registry，返回 (归一化记录, meta)。meta 含 akshare_version / schema_version。"""
    path = resolve_registry_path(src)
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("interfaces")
    if not isinstance(raw, list):
        sys.exit(f"{path} 不是接口表（缺 interfaces 数组）")
    version = installed_version() if src is None else None
    if not version:
        version = str(data.get("akshare_version") or data.get("version") or "") or "unknown"
    return normalize_records(raw), {
        "akshare_version": version,
        "schema_version": data.get("schema_version"),
        "registry_path": str(path),
        "kind": "akshare-registry",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Inspect the akshare interface registry")
    ap.add_argument("--src", type=Path, help="interfaces.json / .whl / .tar.gz（默认用已安装版本）")
    ap.add_argument("--dump", type=Path, help="把归一化记录写成 json")
    args = ap.parse_args()

    records, meta = load_registry(args.src)
    cats: dict[str, int] = {}
    psrc: dict[str, int] = {}
    for r in records:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
        psrc[r["params_source"]] = psrc.get(r["params_source"], 0) + 1
    print(f"# akshare={meta['akshare_version']} schema={meta['schema_version']} "
          f"interfaces={len(records)}")
    print(f"# params 可自动派生: example={psrc.get('example', 0)} "
          f"desc={psrc.get('desc', 0)} no-default={psrc.get('no-default', 0)} "
          f"none={psrc.get('none', 0)}")
    print(f"# categories({len(cats)}): " +
          " ".join(f"{k}={v}" for k, v in sorted(cats.items(), key=lambda x: -x[1])))
    if args.dump:
        args.dump.write_text(json.dumps({"meta": meta, "interfaces": records},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"dumped -> {args.dump}")


if __name__ == "__main__":
    main()
