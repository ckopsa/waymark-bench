"""Finds the top-level definitions of a Clojure or a Python file.

A small reader in the rig, and no outside tool. Each definition comes as
{name, kind, line, end_line}: the lines start at 1, and end_line is the
last line of the definition.
"""

import ast
import re

CLOJURE = (".clj", ".cljs", ".cljc", ".edn")
PYTHON = (".py",)

# The head of a def-style form, (def, (defn-, (g/defguard, and the name
# after any ^metadata.
CLOJURE_HEAD = re.compile(
    r"\(\s*(?P<kind>(?:[\w.*+!?<>=-]+/)?def[\w*+!?<>=-]*)\s+"
    r"(?:\^(?:\{[^}]*\}|[^\s()\[\]{}]+)\s+)*"
    r"(?P<name>[^\s()\[\]{}\"^;]+)")
PYTHON_HEAD = re.compile(r"(?P<kind>async\s+def|def|class)\s+(?P<name>\w+)")
PYTHON_KINDS = {ast.FunctionDef: "def", ast.AsyncFunctionDef: "async def", ast.ClassDef: "class"}


def language_of(path):
    """Gives clojure, python, or None for a file this reader does not know."""
    lower = path.lower()
    if lower.endswith(CLOJURE):
        return "clojure"
    if lower.endswith(PYTHON):
        return "python"
    return None


def definitions(path, text):
    """Gives the definitions of one file, in the order of their lines."""
    language = language_of(path)
    if language == "clojure":
        found = _clojure(text)
    elif language == "python":
        found = _python(text)
    else:
        found = []
    return sorted(found, key=lambda item: item["line"])


def _clojure_forms(text):
    """Gives (offset, line, end_line) for each top-level form.

    It reads strings, comments and character literals, so a paren in a
    docstring, in a string or after a backslash does not count.
    """
    forms = []
    depth = 0
    line = 1
    start = None
    in_string = False
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        if char == "\n":
            line += 1
        elif in_string:
            if char == "\\":
                index += 1
                if index < size and text[index] == "\n":
                    line += 1
            elif char == '"':
                in_string = False
        elif char == ";":
            end = text.find("\n", index)
            if end < 0:
                break
            index = end
            continue
        elif char == "\\":
            index += 1
            if index < size and text[index] == "\n":
                line += 1
        elif char == '"':
            in_string = True
        elif char in "([{":
            if depth == 0:
                start = (index, line)
            depth += 1
        elif char in ")]}" and depth > 0:
            depth -= 1
            if depth == 0:
                forms.append((start[0], start[1], line))
        index += 1
    if depth > 0:
        # a form that never closes runs to the end of the file
        forms.append((start[0], start[1], line))
    return forms


def _clojure(text):
    found = []
    for offset, line, end_line in _clojure_forms(text):
        match = CLOJURE_HEAD.match(text, offset)
        if match:
            found.append({"name": match.group("name"), "kind": match.group("kind"),
                          "line": line, "end_line": end_line})
    return found


def _python(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _python_by_column(text)
    found = []

    def add(node, name, kind):
        start = min([node.lineno] + [item.lineno for item in node.decorator_list])
        found.append({"name": name, "kind": kind, "line": start, "end_line": node.end_lineno})

    for node in tree.body:
        if type(node) not in PYTHON_KINDS:
            continue
        add(node, node.name, PYTHON_KINDS[type(node)])
        if isinstance(node, ast.ClassDef):
            for inner in node.body:
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(inner, "%s.%s" % (node.name, inner.name), "method")
    return found


def _python_by_column(text):
    """A file that does not parse: the defs at column 0, each to the next."""
    lines = text.splitlines()
    tops = [index for index, row in enumerate(lines) if row.strip() and row[0] not in " \t#"]
    found = []
    for position, index in enumerate(tops):
        match = PYTHON_HEAD.match(lines[index])
        if not match:
            continue
        start = index
        while start > 0 and lines[start - 1].startswith("@"):
            start -= 1
        end = (tops[position + 1] if position + 1 < len(tops) else len(lines)) - 1
        while end > index and not lines[end].strip():
            end -= 1
        found.append({"name": match.group("name"), "kind": " ".join(match.group("kind").split()),
                      "line": start + 1, "end_line": end + 1})
    return found
