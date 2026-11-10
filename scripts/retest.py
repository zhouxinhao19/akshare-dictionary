"""Full/batch live re-test that writes results back into data/catalog.json.

catalog 的 status/columns/row_count 是实测快照。上游 akshare 升级、接口改名、参数变更、
数据源改版后，这些字段必须现场重跑才可信。本脚本把这一步做成可重复执行的维护动作：

  python retest.py --status untested            # 只测 catalog 标为 untested 的（升级后首选）
  python retest.py --all --jobs 6               # 全量重测（慢，几百次真实请求）
  python retest.py --names stock_zh_a_hist      # 指定接口
  python retest.py --status error --limit 30    # 限量试跑
  python retest.py --all --resume               # 断点续跑（跳过已测过的）

实现：主进程按接口分片启动 N 个 worker 子进程；每个接口再套一层"独立子进程 + 硬超时"
（复用 check.check_one），挂死的接口会被 terminate 掉，不会拖垮整轮。worker 边跑边把结果
以 JSONL 追加到 data/.retest-<shard>.jsonl，主进程汇总后一次性写回 catalog.json，
中途被 Ctrl-C 或断电，已完成的结果仍在检查点里，`--resume` 可继续。

⚠️ 这是对公开数据源的真实请求：注意频率（--delay）与场合，别在别人的网络/CI 里跑全量。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from check import build_call, check_one  # noqa: E402
from lookup import CATALOG, load_catalog  # noqa: E402

CHECKPOINT_GLOB = ".retest-*.jsonl"


def _slim_error(err: object, limit: int = 240) -> str | None:
    if not err:
        return None
    text = " ".join(str(err).split())
    return text[:limit] + "..." if len(text) > limit else text


def columns_match_doc(item: dict[str, Any], columns: list[str]) -> bool | None:
    """实测列集合 与 文档列集合 是否一致；无文档列则 None（与历史 catalog 同定义）。"""
    doc = [str(o.get("name")) for o in (item.get("outputs") or []) if o.get("name")]
    if not doc:
        return None
    return {str(c) for c in columns} == {c for c in doc}


# ---------- worker：一个分片的顺序实测 ----------

def run_shard(names: list[str], timeout: float, delay: float, out: Path,
              resume_from: set[str]) -> None:
    data = load_catalog()
    by_name = {str(i.get("name")): i for i in (data.get("interfaces") or [])}
    done = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as fh:
        for name in names:
            if name in resume_from:
                continue
            item = by_name.get(name)
            if item is None:
                continue
            rec = {"name": name, "at": datetime.now(timezone.utc).isoformat()}
            try:
                res = check_one(item, timeout)
            except Exception as e:  # 单个接口意外不能让整轮崩
                res = {"ok": False, "timeout": False, "seconds": 0.0,
                       "rows": None, "ncols": None, "columns": [],
                       "error": f"{type(e).__name__}: {e}"}
            rec.update({
                "ok": bool(res.get("ok")),
                "timeout": bool(res.get("timeout")),
                "seconds": res.get("seconds"),
                "rows": res.get("rows"),
                "ncols": res.get("ncols"),
                "columns": res.get("columns") or [],
                "error": _slim_error(res.get("error")),
            })
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            done += 1
            verdict = "OK" if rec["ok"] else ("TO" if rec["timeout"] else "ERR")
            print(f"[{verdict}] {name} ({rec['seconds']}s)", flush=True)
            if delay > 0:
                time.sleep(delay)
    print(f"shard done: {out.name} tested={done}", flush=True)


# ---------- 选择要测的接口 ----------

def select(data: dict[str, Any], args: argparse.Namespace) -> list[str]:
    items = data.get("interfaces") or []
    if args.names:
        wanted = set(args.names)
        unknown = wanted - {str(i.get("name")) for i in items}
        if unknown:
            sys.exit(f"catalog 中没有接口: {', '.join(sorted(unknown))}")
        pool = [str(i.get("name")) for i in items if str(i.get("name")) in wanted]
    elif args.all:
        pool = [str(i.get("name")) for i in items]
    elif args.status:
        statuses = set(args.status.split(","))
        pool = [str(i.get("name")) for i in items if i.get("status") in statuses]
    else:
        sys.exit("需要 --all / --status <s> / --names ...")
    if args.category:
        by_name = {str(i.get("name")): i for i in items}
        cats = {c.strip() for c in args.category.split(",")}
        pool = [n for n in pool
                if any(str(by_name[n].get("category") or "").startswith(c) for c in cats)]
    if args.include_ignored:
        pool = list(pool)  # 默认不排除任何接口；此标志仅为可读性保留
    pool.sort()
    if args.limit:
        pool = pool[: args.limit]
    return pool


def read_checkpoints(directory: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob(CHECKPOINT_GLOB)):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:  # 最后一行可能被中断截断
                continue
            if rec.get("name"):
                latest[str(rec["name"])] = rec
    return latest


# ---------- 写回 catalog ----------

def apply_results(data: dict[str, Any], results: dict[str, dict[str, Any]],
                  akshare_version: str) -> dict[str, int]:
    by_name = {str(i.get("name")): i for i in (data.get("interfaces") or [])}
    now = datetime.now(timezone.utc).isoformat()
    changed = {"success": 0, "error": 0, "timeout": 0, "flipped": 0}
    for name, rec in results.items():
        item = by_name.get(name)
        if item is None:
            continue
        was = item.get("status")
        status = "success" if rec.get("ok") else ("timeout" if rec.get("timeout") else "error")
        item["status"] = status
        item["tested_at"] = rec.get("at") or now
        item["test_seconds"] = rec.get("seconds")
        item["status_source"] = "retest"
        if rec.get("ok"):
            cols = [str(c) for c in (rec.get("columns") or [])]
            item["columns"] = cols
            item["row_count"] = rec.get("rows")
            item["columns_match_doc"] = columns_match_doc(item, cols)
            item["error"] = None
        else:
            item["error"] = rec.get("error")
        changed[status] = changed.get(status, 0) + 1
        if was != status:
            changed["flipped"] += 1
    meta = data.setdefault("meta", {})
    counts: dict[str, int] = {}
    for item in data.get("interfaces") or []:
        st = str(item.get("status") or "untested")
        counts[st] = counts.get(st, 0) + 1
    meta["status_counts"] = counts
    meta["retest_at"] = now
    meta["retest_akshare_version"] = akshare_version
    meta["retest_count"] = len(results)
    return changed


def installed_akshare() -> str:
    import importlib.metadata

    try:
        return importlib.metadata.version("akshare")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def write_catalog(data: dict[str, Any]) -> None:
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CATALOG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    tmp.replace(CATALOG)


def cmd_worker(args: argparse.Namespace) -> int:
    data = load_catalog()
    pool = select(data, args)
    shard = pool[args.shard:: args.jobs] if args.jobs > 1 else pool
    resume_from: set[str] = set()
    if args.resume:
        resume_from = set(read_checkpoints(CATALOG.parent))
    out = CATALOG.parent / f".retest-{args.shard}.jsonl"
    if args.shard == 0 and args.jobs == 1 and out.exists() and not args.resume:
        out.unlink()
    run_shard(shard, args.timeout, args.delay, out, resume_from)
    return 0


def cmd_master(args: argparse.Namespace) -> int:
    data = load_catalog()
    pool = select(data, args)
    if not pool:
        print("没有需要测的接口")
        return 0

    already = read_checkpoints(CATALOG.parent) if args.resume else {}
    todo = [n for n in pool if n not in already]
    print(f"# 目标 {len(pool)} 个接口；已有检查点 {len(pool) - len(todo)}；本轮待测 {len(todo)}")
    if not todo:
        print("全部已在检查点里；直接写回结果")
    else:
        version = installed_akshare()
        if version == "unknown":
            sys.exit("当前环境没有安装 akshare；retest 需要真实调用（见 check.sh 的 python 探测）")
        args.workers = True
        if args.dry_run:
            for i in range(args.jobs):
                print(f"  shard {i}: {len(todo[i::args.jobs])} 个")
            print("--dry-run: 未启动 worker")
            return 0
        procs: list[subprocess.Popen] = []
        outdir = CATALOG.parent
        for i in range(args.jobs):
            shard_file = outdir / f".retest-{i}.jsonl"
            if not args.resume and shard_file.exists():
                shard_file.unlink()
            cmd = [sys.executable, str(SCRIPT_DIR / "retest.py"), "--worker",
                   "--jobs", str(args.jobs), "--shard", str(i),
                   "--timeout", str(args.timeout), "--delay", str(args.delay)]
            if args.resume:
                cmd.append("--resume")
            if args.all:
                cmd.append("--all")
            if args.status:
                cmd += ["--status", str(args.status)]
            if args.names:
                cmd += ["--names"] + list(args.names)
            if args.category:
                cmd += ["--category", str(args.category)]
            if args.limit:
                cmd += ["--limit", str(args.limit)]
            procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.STDOUT))
        est = len(todo) * max(args.timeout, 5.0) / max(args.jobs, 1)
        print(f"# 启动 {args.jobs} 个 worker；最坏耗时约 {est/60:.0f} 分钟"
              f"（远快于此，取决于数据源响应）")
        try:
            while any(p.poll() is None for p in procs):
                done = len(read_checkpoints(outdir))
                print(f"\r  进度 {done}/{len(pool)} 个接口已出结果", end="", flush=True)
                time.sleep(10)
        except KeyboardInterrupt:
            print("\n中断：已完成的结果保留在检查点，用 --resume 继续")
            for p in procs:
                p.terminate()
            return 130
        print()

    results = read_checkpoints(CATALOG.parent)
    relevant = {n: results[n] for n in pool if n in results}
    if args.no_write:
        for name, rec in sorted(relevant.items()):
            verdict = "OK" if rec["ok"] else ("TO" if rec["timeout"] else "ERR")
            print(f"[{verdict}] {name} rows={rec.get('rows')} "
                  f"cols={len(rec.get('columns') or [])} {(rec.get('error') or '')[:70]}")
        print(f"# 共 {len(relevant)} 条结果，未写回（--no-write）")
        return 0
    changed = apply_results(data, relevant, installed_akshare())
    write_catalog(data)
    counts = data["meta"].get("status_counts", {})
    print(f"写回 {CATALOG}  实测 {len(relevant)} 个："
          f"success={changed.get('success', 0)} error={changed.get('error', 0)} "
          f"timeout={changed.get('timeout', 0)} 状态翻转={changed.get('flipped', 0)}")
    print("# catalog 现状: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if not args.keep_checkpoints:
        for path in CATALOG.parent.glob(CHECKPOINT_GLOB):
            path.unlink(missing_ok=True)
        print("# 检查点已清理（--keep-checkpoints 可保留）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch live re-test that updates data/catalog.json")
    ap.add_argument("--all", action="store_true", help="测全部接口")
    ap.add_argument("--status", help="只测这些状态的接口，逗号分隔，如 untested,error")
    ap.add_argument("--names", nargs="*", help="指定接口名")
    ap.add_argument("--category", help="限定类目（前缀匹配），逗号分隔")
    ap.add_argument("--limit", type=int, help="最多测多少个（排序后取前 N，便于试跑）")
    ap.add_argument("--include-ignored", action="store_true", help="保留位：不排除任何接口")
    ap.add_argument("--timeout", type=float, default=45.0, help="单接口硬超时秒数（默认 45）")
    ap.add_argument("--delay", type=float, default=0.5, help="同一 worker 内两次请求间隔秒数（默认 0.5）")
    ap.add_argument("--jobs", type=int, default=4, help="并行 worker 数（默认 4）")
    ap.add_argument("--resume", action="store_true", help="跳过检查点里已有结果的接口")
    ap.add_argument("--dry-run", action="store_true", help="只打印分片计划，不启动 worker")
    ap.add_argument("--no-write", action="store_true", help="只汇总打印，不写回 catalog")
    ap.add_argument("--keep-checkpoints", action="store_true", help="写回后保留检查点文件")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--shard", type=int, default=0, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.jobs < 1:
        sys.exit("--jobs 至少为 1")
    if args.worker:
        return cmd_worker(args)
    if not any([args.all, args.status, args.names]):
        sys.exit("需要 --all / --status <s> / --names ...（这是全量真实请求，显式指定范围）")
    return cmd_master(args)


if __name__ == "__main__":
    sys.exit(main())
