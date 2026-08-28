"""Rebuild data/catalog.json from the full dictionary JSON.

  python build_catalog.py
  python build_catalog.py --src /path/to/akshare_dictionary.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SRC = Path.home() / "Desktop" / "akshare数据字典" / "output" / "akshare_dictionary.json"
OUT = SKILL_DIR / "data" / "catalog.json"


def slim_error(err: object) -> str | None:
    if not err:
        return None
    text = str(err)
    return text[:240] + "..." if len(text) > 240 else text


def slim_params(raw: list | None) -> list[dict]:
    out = []
    for p in raw or []:
        name = p.get("name")
        if name in (None, "-", ""):
            continue
        out.append({"name": name, "type": p.get("type"), "desc": p.get("desc")})
    return out


def slim_outputs(raw: list | None) -> list[dict]:
    out = []
    for o in raw or []:
        name = o.get("name")
        if name in (None, "-", ""):
            continue
        desc = o.get("desc")
        out.append(
            {
                "name": name,
                "type": o.get("type"),
                "desc": None if desc in (None, "-") else desc,
            }
        )
    return out


def convert(src: Path) -> dict:
    data = json.loads(src.read_text(encoding="utf-8"))
    interfaces = []
    for it in data.get("interfaces") or []:
        test = it.get("test") or {}
        interfaces.append(
            {
                "name": it.get("name"),
                "category": it.get("category"),
                "desc": it.get("desc") or "",
                "url": it.get("url") or "",
                "limit": it.get("limit_desc") or "",
                "params": slim_params(it.get("params")),
                "outputs": slim_outputs(it.get("outputs")),
                "example": it.get("example") or "",
                "status": test.get("status") or "unknown",
                "params_used": test.get("params_used") or {},
                "columns": test.get("columns") or [],
                "row_count": test.get("row_count"),
                "columns_match_doc": test.get("columns_match_doc"),
                "error": slim_error(test.get("error")),
            }
        )
    return {
        "meta": data.get("meta") or {},
        "categories": data.get("categories") or {},
        "interfaces": interfaces,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    args = parser.parse_args()
    if not args.src.exists():
        sys.exit(f"source not found: {args.src}")
    slim = convert(args.src)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(slim, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"wrote {OUT}  interfaces={len(slim['interfaces'])}  bytes={OUT.stat().st_size}")


if __name__ == "__main__":
    main()
