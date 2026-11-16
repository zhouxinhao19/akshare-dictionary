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

# akshare 包内 data/ 下的机器可读元数据文件
DATA_FILES = {"interfaces": "interfaces.json", "renames": "renames.json"}

_RENAME_CACHE: dict[str, dict[str, Any]] = {}


def _version_from_archive(src: Path) -> str | None:
    """从 wheel/sdist 里读 akshare 的真实版本号（dist-info/METADATA 或 _version.py）。"""
    try:
        if src.suffix == ".whl":
            with zipfile.ZipFile(src) as z:
                for name in z.namelist():
                    if name.endswith(".dist-info/METADATA"):
                        for line in z.read(name).decode("utf-8", "replace").splitlines():
                            if line.startswith("Version:"):
                                return line.split(":", 1)[1].strip()
                for name in z.namelist():
                    if name.endswith("/_version.py"):
                        m = re.search(r'__version__\s*=\s*["\']([^"\']+)',
                                      z.read(name).decode("utf-8", "replace"))
                        if m:
                            return m.group(1)
        elif src.name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(src, "r:*") as t:
                for member in t.getmembers():
                    if member.isfile() and member.name.endswith("/_version.py"):
                        fh = t.extractfile(member)
                        if fh is None:
                            continue
                        m = re.search(r'__version__\s*=\s*["\']([^"\']+)',
                                      fh.read().decode("utf-8", "replace"))
                        if m:
                            return m.group(1)
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return None
    # 兜底：akshare-1.20.65-py3-none-any.whl / akshare-1.20.65.tar.gz
    m = re.match(r"akshare[-_](\d+\.\d+[\w.]*?)(?:-(?:py|cp)\d|\.tar|\.zip|$)", src.name)
    return m.group(1) if m else None


def version_key(text: object) -> tuple[int, ...]:
    """把 1.20.65 这类版本号拆成可比较的数字元组；解析不了的段按 0 处理。"""
    out = []
    for chunk in str(text or "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def version_key(text: object) -> tuple[int, ...]:
    """把 1.20.65 这类版本号拆成可比较的数字元组；解析不了的段按 0 处理。"""
    out = []
    for chunk in str(text or "").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def installed_version() -> str | None:
    """已安装 akshare 的版本号；不 import 它。"""
    try:
        return importlib.metadata.version("akshare")
    except importlib.metadata.PackageNotFoundError:
        return None


def find_data_file(filename: str) -> Path | None:
    """已安装 akshare 包内的 data/<filename>；未安装或该版本没这个文件时返回 None。"""
    try:
        spec = importlib.util.find_spec("akshare")
    except (ImportError, ValueError):
        return None
    if not spec or not spec.submodule_search_locations:
        return None
    for loc in spec.submodule_search_locations:
        cand = Path(loc) / "data" / filename
        if cand.exists():
            return cand
    return None


def find_registry_json() -> Path | None:
    """已安装 akshare 包内的 data/interfaces.json；未安装或旧版（<1.20）返回 None。"""
    return find_data_file(DATA_FILES["interfaces"])


def _extract_data_file(src: Path, workdir: Path, filename: str) -> Path | None:
    """从 wheel(.whl) 或 sdist(.tar.gz) 里取出 data/<filename>。"""
    marker = f"/data/{filename}"
    if src.suffix == ".whl":
        with zipfile.ZipFile(src) as z:
            names = [n for n in z.namelist() if n.endswith(marker)]
            if not names:
                return None
            target = workdir / filename
            with z.open(names[0]) as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
            return target
    if src.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(src, "r:*") as t:
            members = [m for m in t.getmembers()
                       if m.isfile() and m.name.endswith(marker)]
            if not members:
                return None
            extracted = t.extractfile(members[0])
            if extracted is None:
                return None
            target = workdir / filename
            target.write_bytes(extracted.read())
            return target
    return None


def resolve_data_path(filename: str, src: Path | None = None) -> Path | None:
    """把"任意来源"解析成 data/<filename> 的真实路径；拿不到返回 None。

    来源可以是已安装的 akshare、wheel/sdist，或一个 json 文件（此时在同目录找同名/兄弟文件）。
    """
    if src is not None:
        if src.is_file():
            if src.suffix == ".json":
                if src.name == filename:
                    return src
                sibling = src.parent / filename
                return sibling if sibling.exists() else None
            workdir = Path(tempfile.mkdtemp(prefix="akdict-data-"))
            got = _extract_data_file(src, workdir, filename)
            if got is not None:
                return got
        # 指定来源里没有这个文件时，退回已安装版本（例如旧字典仓库没有 renames.json）
    return find_data_file(filename)


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
    got = resolve_data_path(DATA_FILES["interfaces"], src)
    if got is None:
        sys.exit(f"{src.name} 里没有 data/interfaces.json（不是 akshare>=1.20 的发行包？）")
    return got


def load_renames(src: Path | None = None) -> dict[str, Any]:
    """读 akshare 内置的接口改名/移除对照表 data/renames.json。

    1.20.44 起随包提供，结构为 {"renamed": {旧名: [新名, 版本]}, "removed": {旧名: 版本}}。
    拿不到（老版本 akshare、来源里没有）时返回空表，调用方按"没有改名信息"处理即可。
    """
    key = "" if src is None else str(Path(src).resolve())
    cached = _RENAME_CACHE.get(key)
    if cached is not None:
        return cached

    data: dict[str, Any] = {}
    path = resolve_data_path(DATA_FILES["renames"], src)
    if path is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            data = {"renamed": dict(raw.get("renamed") or {}),
                    "removed": dict(raw.get("removed") or {})}
    data.setdefault("renamed", {})
    data.setdefault("removed", {})
    _RENAME_CACHE[key] = data
    return data


def flatten_renames(renamed: dict[str, Any]) -> dict[str, str]:
    """把改名表压平成 旧名 → 当前名，跟随多级改名并防止成环。"""
    out: dict[str, str] = {}
    for old in renamed:
        seen = {old}
        cur = old
        while cur in renamed:
            nxt = renamed[cur]
            nxt = nxt[0] if isinstance(nxt, (list, tuple)) else str(nxt)
            if not nxt or nxt in seen:
                break
            seen.add(nxt)
            cur = nxt
        out[old] = cur
    return out


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
    if not version and src is not None and src.is_file() and src.suffix != ".json":
        version = _version_from_archive(src)
    if not version:
        version = str(data.get("akshare_version") or data.get("version") or "") or "unknown"
    renames = load_renames(src)
    return normalize_records(raw), {
        "akshare_version": version,
        "schema_version": data.get("schema_version"),
        "registry_path": str(path),
        "kind": "akshare-registry",
        "renames": {"renamed": len(renames["renamed"]), "removed": len(renames["removed"])},
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
