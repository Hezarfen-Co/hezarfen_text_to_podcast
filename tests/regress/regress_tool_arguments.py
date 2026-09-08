from __future__ import annotations

import ast
import io
import pathlib
import unittest

TOOLS = sorted(pathlib.Path("tools").glob("*.py"))


def _declared_dests(tree: ast.AST) -> set[str]:
    dests: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        explicit = None
        for keyword in node.keywords:
            if keyword.arg == "dest" and isinstance(keyword.value, ast.Constant):
                explicit = str(keyword.value.value)
        if explicit is not None:
            dests.add(explicit)
            continue
        for arg in node.args:
            if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                continue
            name = arg.value
            if name.startswith("--"):
                dests.add(name[2:].replace("-", "_"))
            elif not name.startswith("-"):
                dests.add(name.replace("-", "_"))
    return dests


def _accessed_names(tree: ast.AST) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "args"
        ):
            used.add(node.attr)
    return used


class ToolArgumentsMatchTheirParser(unittest.TestCase):
    def test_every_args_attribute_has_a_declared_argument(self):
        self.assertTrue(TOOLS, "tools/*.py bulunamadi")
        for path in TOOLS:
            with self.subTest(tool=path.name):
                with io.open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
                orphan = sorted(_accessed_names(tree) - _declared_dests(tree))
                self.assertEqual(
                    orphan,
                    [],
                    f"{path.name}: parser'da tanimli OLMAYAN args ozniteligi "
                    f"{orphan}. Bayrak yeniden adlandirilip atif guncellenmemis "
                    f"olabilir; bu sinif hicbir testte gorunmez, yalnizca araci "
                    f"gercekten kosturunca AttributeError verir.",
                )

    def test_declared_arguments_are_ascii_and_lowercase(self):
        for path in TOOLS:
            with io.open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for dest in sorted(_declared_dests(tree)):
                with self.subTest(tool=path.name, dest=dest):
                    self.assertTrue(dest.isascii(), f"ASCII disi arguman: {dest}")
                    self.assertEqual(dest, dest.lower())


if __name__ == "__main__":
    unittest.main()
