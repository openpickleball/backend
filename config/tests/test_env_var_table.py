"""The README's environment variable table lists every variable settings read (#64).

The files that read the process environment at startup are
``config/settings.py`` and ``gunicorn.conf.py``. This test reads them as
source text, finds every variable they read (directly, through a helper
such as ``_raw`` or ``_days``, or in a loop over names), and checks the
README table against that list and against ``.do/production.yaml``.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent
README = BASE_DIR / "README.md"
APP_SPEC = BASE_DIR / ".do" / "production.yaml"
READ_FILES = (BASE_DIR / "config" / "settings.py", BASE_DIR / "gunicorn.conf.py")

SECTION_HEADING = "### Environment variables"
REQUIRED_VALUES = frozenset({"yes", "no", "DEBUG off", "Resend, DEBUG off"})
SECRET_VALUES = frozenset({"yes", "no"})
# Where production gets each value: written in the spec, a secret declared
# in the spec and set in the App Platform console, bound by App Platform
# from a database component or set by it at run time, left to the
# default, or never set (settings refuse it).
PRODUCTION_VALUES = frozenset(
    {"spec", "console", "bound", "App Platform", "default", "never"}
)
PLACEHOLDER = re.compile(r"<[^<>]+>")


class UnresolvedReadError(Exception):
    """A read of the environment whose variable name the test cannot tell."""


def _is_os_environ(node: ast.AST) -> bool:
    return ast.unparse(node) == "os.environ"


def _calls(node: ast.AST) -> list[ast.Call]:
    return [child for child in ast.walk(node) if isinstance(child, ast.Call)]


def _first_param(function: ast.FunctionDef) -> str | None:
    params = function.args.posonlyargs + function.args.args
    return params[0].arg if params else None


def _read_name(node: ast.AST, readers: set[str]) -> ast.expr | None:
    """The expression naming the variable, when ``node`` reads one.

    A read is ``os.environ.get(x)``, ``os.getenv(x)``, ``os.environ[x]``,
    ``x in os.environ``, or a call to a reader helper with ``x`` first.
    """
    if isinstance(node, ast.Call):
        func = node.func
        is_read = (
            (
                (isinstance(func, ast.Attribute) and func.attr == "get")
                and _is_os_environ(func.value)
            )
            or ast.unparse(func) == "os.getenv"
            or (isinstance(func, ast.Name) and func.id in readers)
        )
        if not is_read:
            return None
        if not node.args:
            raise UnresolvedReadError(f"line {node.lineno}: a read with no name")
        return node.args[0]
    if isinstance(node, ast.Subscript) and _is_os_environ(node.value):
        return node.slice
    if (
        isinstance(node, ast.Compare)
        and len(node.comparators) == 1
        and isinstance(node.ops[0], ast.In | ast.NotIn)
        and _is_os_environ(node.comparators[0])
    ):
        return node.left
    return None


def _readers(tree: ast.Module) -> set[str]:
    """Module-level functions that read the variable their first parameter names.

    Found transitively: ``_days(name)`` calls ``_positive_days(name)``,
    which calls ``_raw(name)``, which calls ``os.environ.get(name)``.
    """
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    readers: set[str] = set()
    changed = True
    while changed:
        changed = False
        for function in functions:
            param = _first_param(function)
            if function.name in readers or param is None:
                continue
            for node in ast.walk(function):
                name = _read_name(node, readers)
                if isinstance(name, ast.Name) and name.id == param:
                    readers.add(function.name)
                    changed = True
                    break
    return readers


# Spellings that reach the environment without ``os.environ`` or
# ``os.getenv`` by those names. The test refuses them outright.
_OS_ENVIRONMENT_NAMES = frozenset({"environ", "environb", "getenv", "getenvb"})


def _refuse_other_spellings(tree: ast.Module, readers: set[str]) -> None:
    func_names = {
        id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    for node in ast.walk(tree):
        line = getattr(node, "lineno", "?")
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            if any(alias.name in _OS_ENVIRONMENT_NAMES for alias in node.names):
                raise UnresolvedReadError(f"line {line}: import os, then os.environ")
        elif isinstance(node, ast.Import):
            if any(alias.name == "os" and alias.asname for alias in node.names):
                raise UnresolvedReadError(f"line {line}: import os without an alias")
        elif isinstance(node, ast.Attribute) and (
            node.attr == "ENVIRON"
            or (
                node.attr in {"environb", "getenvb"} and ast.unparse(node.value) == "os"
            )
        ):
            raise UnresolvedReadError(
                f"line {line}: read os.environ, not {ast.unparse(node)}"
            )
        elif (
            isinstance(node, ast.Name)
            and node.id in readers
            and id(node) not in func_names
        ):
            raise UnresolvedReadError(
                f"line {line}: the reader {node.id} used other than as a call"
            )


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _names(
    arg: ast.expr,
    parents: dict[ast.AST, ast.AST],
    readers: set[str],
) -> set[str]:
    """The variable names a read's argument can hold, or raise."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return {arg.value}
    if isinstance(arg, ast.Name):
        node: ast.AST = arg
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.FunctionDef):
                # A helper's own parameter: its callers name the variable.
                if node.name in readers and _first_param(node) == arg.id:
                    return set()
                break
            if (
                isinstance(node, ast.For)
                and isinstance(node.target, ast.Name)
                and node.target.id == arg.id
                and isinstance(node.iter, ast.Tuple | ast.List)
            ):
                values = [
                    item.value
                    for item in node.iter.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                ]
                if len(values) == len(node.iter.elts):
                    return set(values)
                break
    raise UnresolvedReadError(
        f"line {arg.lineno}: cannot tell which variable {ast.unparse(arg)} names"
    )


def read_variables(source: str) -> set[str]:
    """Every environment variable a module reads, from its source text.

    Raises ``UnresolvedReadError`` for a read whose name is not a string
    literal, a loop over string literals, or a reader helper's own
    parameter, for any other use of ``os.environ``, and for other
    spellings of it (``from os import environ``, an alias of ``os``, an
    alias of a reader helper), so a read in this module's style cannot
    slip past the table. A read hidden behind a function from another
    module would still slip past: settings use only their own helpers.
    """
    tree = ast.parse(source)
    readers = _readers(tree)
    parents = _parents(tree)
    names: set[str] = set()
    handled: set[ast.AST] = set()
    _refuse_other_spellings(tree, readers)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "environ.Env":
            raise UnresolvedReadError(
                f"line {node.lineno}: django-environ's Env getters are not "
                "allowed (CLAUDE.md, Settings)"
            )
        name = _read_name(node, readers)
        if name is None:
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            handled.add(node.func.value)
        elif isinstance(node, ast.Subscript):
            handled.add(node.value)
        elif isinstance(node, ast.Compare):
            handled.add(node.comparators[0])
        names |= _names(name, parents, readers)
    for node in ast.walk(tree):
        if isinstance(node, ast.expr) and _is_os_environ(node) and node not in handled:
            raise UnresolvedReadError(
                f"line {node.lineno}: os.environ used other than to read one "
                "named variable"
            )
    return names


def settings_variables() -> set[str]:
    names: set[str] = set()
    for path in READ_FILES:
        names |= read_variables(path.read_text())
    return names


@dataclass(frozen=True)
class Row:
    name: str
    purpose: str
    required: str
    secret: str
    example: str
    production: str


def _section() -> str:
    text = README.read_text()
    start = text.index(SECTION_HEADING)
    rest = text[start + len(SECTION_HEADING) :]
    end = re.search(r"^#{1,3} ", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table(header: str) -> list[list[str]]:
    """The rows of the section's table whose first header cell is ``header``."""
    lines = _section().splitlines()
    for index, line in enumerate(lines):
        if line.startswith("|") and _cells(line)[0] == header:
            rows = []
            for row in lines[index + 2 :]:
                if not row.startswith("|"):
                    break
                rows.append(_cells(row))
            return rows
    raise AssertionError(f"No table headed {header!r} in {SECTION_HEADING}")


def _unquote(cell: str) -> str:
    match = re.fullmatch(r"`([^`]*)`", cell)
    return match.group(1) if match else cell


def table_rows() -> dict[str, Row]:
    rows: dict[str, Row] = {}
    for cells in _table("Variable"):
        assert len(cells) == 6, f"A row needs six cells: {cells}"
        row = Row(
            name=_unquote(cells[0]),
            purpose=cells[1],
            required=cells[2],
            secret=cells[3],
            example=_unquote(cells[4]),
            production=cells[5],
        )
        assert row.name not in rows, f"{row.name} is listed twice"
        rows[row.name] = row
    return rows


def _spec() -> dict[str, Any]:
    spec: dict[str, Any] = yaml.safe_load(APP_SPEC.read_text())
    return spec


# The extractor itself, on made-up source.

SAMPLE = """
import os


def _raw(name):
    return os.environ.get(name, "")


def _wrap(name, default):
    return _raw(name) or default


def _check(url):
    return url


def _strict(name):
    return os.environ[name]


A = _raw("ALPHA")
B = _wrap("BETA", 1)
C = _check(_wrap("GAMMA", ""))
D = os.environ["DELTA"]
D2 = _strict("KAPPA")
E = "EPSILON" in os.environ
for _name in ("ZETA", "ETA"):
    _raw(_name)
"""


def test_finds_reads_through_helpers_loops_and_os_environ() -> None:
    assert read_variables(SAMPLE) == {
        "ALPHA",
        "BETA",
        "GAMMA",
        "DELTA",
        "EPSILON",
        "ZETA",
        "ETA",
        "KAPPA",
    }


@pytest.mark.parametrize(
    "line",
    [
        'X = _wrap("PRE" + "FIX", 1)',
        "X = _raw(NAME)",
        "X = os.environ.copy()",
        "X = dict(os.environ)",
        'X = environ.Env().str("THETA")',
        'X = os.getenv(f"{NAME}")',
        'from os import environ\nX = environ.get("IOTA")',
        'from os import getenv\nX = getenv("IOTA")',
        'import os as o\nX = o.environ.get("IOTA")',
        '_r = _raw\nX = _r("IOTA")',
        'X = environ.Env.ENVIRON["IOTA"]',
        'X = os.environb[b"IOTA"]',
    ],
)
def test_refuses_a_read_it_cannot_name(line: str) -> None:
    with pytest.raises(UnresolvedReadError):
        read_variables(SAMPLE + line + "\n")


def test_settings_reads_are_found() -> None:
    names = settings_variables()
    # One of each read style: a helper, a nested helper, the refusal
    # loop, and gunicorn's own reader.
    assert {"DJANGO_SECRET_KEY", "CLUB_INVITE_DAYS", "CELERY_BROKER_URL"} <= names
    assert {"PORT", "WEB_CONCURRENCY"} <= names


# The table.


def test_every_read_variable_is_in_the_table() -> None:
    missing = settings_variables() - table_rows().keys()
    assert not missing, (
        f"Add these to the README's {SECTION_HEADING} table: {sorted(missing)}"
    )


def test_every_row_is_read() -> None:
    stale = table_rows().keys() - settings_variables()
    assert not stale, f"Nothing reads these table rows: {sorted(stale)}"


def test_cells_use_the_fixed_words() -> None:
    for row in table_rows().values():
        assert row.purpose, row.name
        assert row.required in REQUIRED_VALUES, row
        assert row.secret in SECRET_VALUES, row
        assert row.production in PRODUCTION_VALUES, row


def test_secret_examples_are_placeholders() -> None:
    for row in table_rows().values():
        if row.secret == "yes":
            assert PLACEHOLDER.search(row.example), (
                f"{row.name}'s example needs an <angle bracket> placeholder "
                "where the secret goes, never a real value"
            )


def test_production_spec_agrees_with_the_table() -> None:
    rows = table_rows()
    spec_envs = {env["key"]: env for env in _spec()["envs"]}
    for key, env in spec_envs.items():
        assert key in rows, f"{key} is in .do/production.yaml but not the table"
        row = rows[key]
        if env.get("type") == "SECRET":
            assert row.production == "console", row
            assert row.secret == "yes", row
        elif "${" in str(env.get("value", "")):
            assert row.production == "bound", row
        else:
            assert row.production == "spec", row
            assert row.secret == "no", row
    for row in rows.values():
        if row.production in {"spec", "console", "bound"}:
            assert row.name in spec_envs, (
                f"{row.name} says {row.production} but is not in .do/production.yaml"
            )


def test_build_time_variables_are_listed() -> None:
    listed = {_unquote(cells[0]) for cells in _table("Build variable")}
    spec_build = {
        env["key"] for site in _spec()["static_sites"] for env in site.get("envs", [])
    }
    assert listed == spec_build


def test_env_example_names_every_variable() -> None:
    example = (BASE_DIR / ".env.example").read_text()
    missing = [name for name in table_rows() if not re.search(rf"\b{name}\b", example)]
    assert not missing, f"Add these to .env.example: {missing}"
