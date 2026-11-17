"""Fails if main.py imports a module that is not known to work in Decky's bundled Python.

Decky runs plugins with a trimmed-down Python: parts of the standard library are missing there
(for example `xml.etree`), and a missing module makes the whole plugin fail to start.
Add a module to ALLOWED only after checking that it works on a real device.
Run from the project folder:  python3 tools/check_imports.py
"""
import ast
import sys

ALLOWED = {
    "asyncio", "base64", "ctypes", "decky", "functools", "json", "os", "re", "shutil", "socket",
    "subprocess", "threading", "time", "urllib.error", "urllib.parse", "urllib.request",
}

path = sys.argv[1] if len(sys.argv) > 1 else "main.py"
with open(path, encoding="utf-8") as f:
    tree = ast.parse(f.read())

bad = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        names = [a.name for a in node.names]
    elif isinstance(node, ast.ImportFrom):
        names = [node.module or ""]
    else:
        continue
    bad += [(node.lineno, n) for n in names if n not in ALLOWED]

if bad:
    for line, name in bad:
        print(f"{path}:{line}: '{name}' is not in the list of modules known to work in Decky's Python")
    sys.exit(1)
print("imports ok")
