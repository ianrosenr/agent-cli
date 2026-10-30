"""Python structure from the standard-library ``ast`` module."""

from __future__ import annotations

import ast

from agent_cli.repository.analyzers import Analysis, Import, Symbol

LANGUAGE = "python"


class PythonAnalyzer:
    name = "python-ast"
    version = "1"
    suffixes = frozenset({".py", ".pyi"})

    def analyze(self, content: bytes) -> Analysis:
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, RecursionError) as error:
            return Analysis(LANGUAGE, error=f"cannot parse: {error}")
        exports = declared_exports(tree)

        def public(name: str) -> bool:
            return name in exports if exports is not None else not name.startswith("_")

        symbols: list[Symbol] = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                symbols.append(symbol(node, node.name, "function", public(node.name)))
            elif isinstance(node, ast.ClassDef):
                exported = public(node.name)
                symbols.append(symbol(node, node.name, "class", exported))
                for member in node.body:
                    if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                        qualname = f"{node.name}.{member.name}"
                        is_public = exported and not private_member(member.name)
                        symbols.append(symbol(member, qualname, "method", is_public))
        return Analysis(LANGUAGE, tuple(symbols), tuple(imports(tree)))


def symbol(node: ast.stmt, qualname: str, kind: str, exported: bool) -> Symbol:
    name = qualname.rpartition(".")[2]
    return Symbol(name, qualname, kind, node.lineno, node.end_lineno or node.lineno, exported)


def private_member(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def imports(tree: ast.Module) -> list[Import]:
    """Every import in the module, including ones nested in functions or ``TYPE_CHECKING``."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [Import(alias.name, (), 0, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = tuple(alias.name for alias in node.names if alias.name != "*")
            found.append(Import(node.module or "", names, node.level, node.lineno))
    return sorted(found, key=lambda item: item.line)


def declared_exports(tree: ast.Module) -> frozenset[str] | None:
    """The literal ``__all__`` list or tuple, when the module declares one."""
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        value = getattr(node, "value", None)
        if isinstance(value, ast.List | ast.Tuple) and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts
        ):
            return frozenset(
                item.value
                for item in value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    return None
