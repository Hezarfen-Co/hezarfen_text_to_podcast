from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import re
import sys

LOCAL = {"router", "script", "ses", "ingest", "bilgi", "config", "tools", "src", "tests"}

ALIASES = {
    "PIL": "pillow",
    "pdfminer": "pdfminer.six",
    "lingua": "lingua_language_detector",
    "faster_whisper": "faster_whisper",
    "yaml": "pyyaml",
    "sklearn": "scikit_learn",
    "cv2": "opencv_python_headless",
}

DELIBERATELY_ABSENT = {
    "fitz": "PyMuPDF AGPL-3.0 - ingest/backends/mupdf.py, try icinde",
    "espeakng_loader": "GPL-3.0 - ses/motor.py, try icinde",
    "rapidocr_onnxruntime": "opsiyonel OCR backend B - ingest/ocr.py, try icinde",
}

CONTAINERFILE_INSTALLED = {
    "onnxruntime": "Containerfile pip adimi: onnxruntime==1.23.2 (ACIKCA kurulur)",
    "transformers": "Containerfile KUR_AGIR blogu: pip install transformers==5.15.0",
    "torch": "Containerfile KUR_AGIR blogu: pip install torch torchvision (CPU tekerlegi)",
    "torchvision": "Containerfile KUR_AGIR blogu: torch ile birlikte",
}


def top_module(name: str) -> str:
    return name.split(".")[0]


def collect_imports(root: str) -> set[str]:
    found: set[str] = set()
    for directory, _, filenames in os.walk(root):
        if "__pycache__" in directory:
            continue
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(directory, filename)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for name in node.names:
                        found.add(top_module(name.name))
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        found.add(top_module(node.module))
    return found


def declared_packages(root: str) -> set[str]:
    names: set[str] = set()
    for filename in os.listdir(root):
        if not (filename.startswith("requirements") and filename.endswith(".txt")):
            continue
        for line in open(os.path.join(root, filename), encoding="utf-8"):
            line = line.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            name = re.split(r"[=<>!\[;]", line)[0].strip()
            if name:
                names.add(name.lower().replace("-", "_").replace(".", "_"))
    return names


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hattin import ettigi ama beyan edilmeyen bagimliliklari bulur"
    )
    parser.add_argument("root", nargs="?", default="vendor/pipeline")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        print("[scanner] HATA: dizin yok: %s" % args.root, flush=True)
        return 2

    used = collect_imports(args.root)
    declared = declared_packages(args.root)
    stdlib = getattr(sys, "stdlib_module_names", frozenset())

    missing = []
    transitive = []
    for module in sorted(used):
        if module in LOCAL or module in stdlib or module in DELIBERATELY_ABSENT:
            continue
        if module in CONTAINERFILE_INSTALLED:
            continue
        package = ALIASES.get(module, module).lower().replace("-", "_").replace(".", "_")
        if package in declared or module.lower().replace("-", "_") in declared:
            continue
        if importlib.util.find_spec(module) is not None:
            transitive.append(module)
            continue
        missing.append(module)

    print("[scanner] kok=%s  import=%d  beyan=%d" % (args.root, len(used), len(declared)), flush=True)
    for module, reason in sorted(DELIBERATELY_ABSENT.items()):
        if module in used:
            print("[scanner] bilerek yok: %-22s %s" % (module, reason), flush=True)
    for module, reason in sorted(CONTAINERFILE_INSTALLED.items()):
        if module in used:
            print("[scanner] imajda kurulu: %-20s %s" % (module, reason), flush=True)
    if transitive:
        print("[scanner] beyan yok ama KURULU (gecisli): %s" % ", ".join(transitive), flush=True)
        print("[scanner] bunlar baska bir paket uzerinden geliyor; pin YOK, kirilgan.", flush=True)
    if missing:
        print("[scanner] BEYAN EDILMEMIS VE KURULU DEGIL: %s" % ", ".join(missing), flush=True)
        print("[scanner] kod bunlari import ediyor, ortamda YOK -> calisma-zamani hatasi.", flush=True)
        return 1
    if transitive and args.strict:
        return 1
    print("[scanner] TAMAM: eksik bagimlilik yok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
