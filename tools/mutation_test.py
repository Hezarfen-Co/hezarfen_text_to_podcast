from __future__ import annotations

import argparse
import ast
import io
import os
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path

ROR = {
    ast.Lt: (ast.Gt, ast.LtE, ast.GtE, ast.Eq, ast.NotEq),
    ast.Gt: (ast.Lt, ast.LtE, ast.GtE, ast.Eq, ast.NotEq),
    ast.LtE: (ast.Lt, ast.Gt, ast.GtE),
    ast.GtE: (ast.Lt, ast.Gt, ast.LtE),
    ast.Eq: (ast.NotEq, ast.Lt, ast.Gt),
    ast.NotEq: (ast.Eq, ast.Lt, ast.Gt),
    ast.In: (ast.NotIn,),
    ast.NotIn: (ast.In,),
    ast.Is: (ast.IsNot,),
    ast.IsNot: (ast.Is,),
}

AOR = {
    ast.Add: (ast.Sub, ast.Mult),
    ast.Sub: (ast.Add, ast.Mult),
    ast.Mult: (ast.Add, ast.Div),
    ast.Div: (ast.Mult, ast.Sub),
    ast.Mod: (ast.Mult, ast.Div),
    ast.Pow: (ast.Mult,),
    ast.FloorDiv: (ast.Div,),
}

COR = {ast.And: ast.Or, ast.Or: ast.And}

SKIP_DELETE = (ast.Return, ast.Raise, ast.Pass, ast.Import, ast.ImportFrom,
               ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def sites(source: str) -> list[tuple[str, int, int, int, object]]:
    tree = ast.parse(source)
    found = []
    for order, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.Compare):
            for slot, op in enumerate(node.ops):
                for new in ROR.get(type(op), ()):
                    found.append(("ROR", order, slot, node.lineno, new))
        elif isinstance(node, ast.BinOp):
            for new in AOR.get(type(node.op), ()):
                found.append(("AOR", order, 0, node.lineno, new))
        elif isinstance(node, ast.BoolOp):
            new = COR.get(type(node.op))
            if new is not None:
                found.append(("COR", order, 0, node.lineno, new))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            found.append(("UOI", order, 0, node.lineno, None))
        body = getattr(node, "body", None)
        if isinstance(body, list) and len(body) > 1:
            for slot, statement in enumerate(body):
                if isinstance(statement, SKIP_DELETE):
                    continue
                found.append(("SDL", order, slot, statement.lineno, None))
    return found


def apply(source: str, operator: str, order: int, slot: int, new) -> str | None:
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    if order >= len(nodes):
        return None
    node = nodes[order]
    if operator == "ROR" and isinstance(node, ast.Compare):
        node.ops[slot] = new()
    elif operator == "AOR" and isinstance(node, ast.BinOp):
        node.op = new()
    elif operator == "COR" and isinstance(node, ast.BoolOp):
        node.op = new()
    elif operator == "UOI" and isinstance(node, ast.UnaryOp):
        parent_fixed = _replace_not(tree, order)
        if not parent_fixed:
            return None
    elif operator == "SDL":
        body = getattr(node, "body", None)
        if not isinstance(body, list) or slot >= len(body):
            return None
        body.pop(slot)
        if not body:
            body.append(ast.Pass())
    else:
        return None
    try:
        return ast.unparse(ast.fix_missing_locations(tree))
    except Exception:
        return None


def _replace_not(tree: ast.AST, order: int) -> bool:
    target = list(ast.walk(tree))[order]

    class Drop(ast.NodeTransformer):
        def __init__(self) -> None:
            self.done = False

        def visit_UnaryOp(self, node: ast.UnaryOp):
            self.generic_visit(node)
            if node is target:
                self.done = True
                return node.operand
            return node

    dropper = Drop()
    dropper.visit(tree)
    return dropper.done


def run_suite(workdir: Path, timeout: float, env: dict | None = None) -> str:
    try:
        done = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests",
             "-t", ".", "--failfast"],
            cwd=str(workdir), capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return "timeout"
    return "survived" if done.returncode == 0 else "killed"


def time_suite(workdir: Path, timeout: float, env: dict | None = None) -> tuple:
    started = time.time()
    status = run_suite(workdir, timeout, env)
    return status, time.time() - started


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Slayt operatorleriyle mutasyon testi: ROR, AOR, COR, UOI, SDL")
    parser.add_argument("target")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    repo = Path.cwd()
    target = Path(args.target)
    source = io.open(target, encoding="utf-8").read()
    plans = sites(source)
    if args.limit:
        plans = plans[: args.limit]

    print("[mutasyon] hedef       : %s" % target, flush=True)
    print("[mutasyon] mutant      : %d" % len(plans), flush=True)
    print("[mutasyon] operatorler : ROR AOR COR UOI SDL", flush=True)
    print("", flush=True)

    workdir = Path(tempfile.mkdtemp(prefix="mutasyon-"))
    for name in ("src", "tests", "tools"):
        shutil.copytree(repo / name, workdir / name,
                        ignore=shutil.ignore_patterns("__pycache__"))
    env = dict(os.environ)
    vendored = repo / "vendor" / "pipeline"
    if vendored.is_dir():
        env["PODCAST_PIPELINE_PATH"] = str(vendored)
    elif (repo.parent / "router" / "hat.py").is_file():
        env["PODCAST_PIPELINE_PATH"] = str(repo.parent)
    baseline, baseline_secs = time_suite(workdir, args.timeout, env)
    limit = max(args.timeout, baseline_secs * 10.0)
    print("[mutasyon] temel kosu : %.1f sn, mutant siniri %.1f sn"
          % (baseline_secs, limit), flush=True)
    if baseline != "survived":
        print("[mutasyon] HATA: mutasyonsuz paket GECMIYOR (%s)" % baseline, flush=True)
        print("[mutasyon] PODCAST_PIPELINE_PATH=%s"
              % env.get("PODCAST_PIPELINE_PATH", "(yok)"), flush=True)
        shutil.rmtree(workdir, ignore_errors=True)
        return 2

    tally = {"killed": 0, "survived": 0, "timeout": 0, "invalid": 0}
    alive = []
    for index, (operator, order, slot, lineno, new) in enumerate(plans, 1):
        mutated = apply(source, operator, order, slot, new)
        if mutated is None or mutated == source:
            tally["invalid"] += 1
            continue
        io.open(workdir / target, "w", encoding="utf-8").write(mutated)
        status = run_suite(workdir, limit, env)
        if status == "timeout":
            status = run_suite(workdir, limit * 2.0, env)
        tally[status] += 1
        if status == "survived":
            alive.append((operator, lineno))
        print("  [%3d/%3d] %-3s satir %-4d %s"
              % (index, len(plans), operator, lineno, status), flush=True)
    io.open(workdir / target, "w", encoding="utf-8").write(source)

    total = tally["killed"] + tally["survived"] + tally["timeout"]
    detected = tally["killed"] + tally["timeout"]
    print("", flush=True)
    print("=" * 58, flush=True)
    print("MUTASYON SONUCU: %s" % target, flush=True)
    print("  oldurulen  : %d" % tally["killed"], flush=True)
    print("  asili kaldi: %d (sonsuz dongu; paket sonlanmayarak yakaladi)"
          % tally["timeout"], flush=True)
    print("  hayatta    : %d" % tally["survived"], flush=True)
    print("  gecersiz   : %d" % tally["invalid"], flush=True)
    if total:
        print("  HAM SKOR   : %.1f%%" % (100.0 * detected / total), flush=True)
        print("  asili kalan mutant OLDURULMUS sayilir: paket onu sonlanmayarak", flush=True)
        print("  tespit eder (mutmut ve PIT ayni kurali kullanir).", flush=True)
        print("  slayt formulu: killed / (toplam - esdeger); esdeger ELLE isaretlenir",
              flush=True)
    if alive:
        print("", flush=True)
        print("HAYATTA KALANLAR (her biri icin karar gerekir):", flush=True)
        for operator, lineno in alive:
            print("  %-3s satir %d" % (operator, lineno), flush=True)
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
