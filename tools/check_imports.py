"""Fails if main.py (or a file in py_modules) imports a module that is not known to work in Decky's bundled Python.

Decky runs plugins with a trimmed-down Python: parts of the standard library are missing there
(for example `xml.etree`), and a missing module makes the whole plugin fail to start.
Add a module to ALLOWED only after checking that it works on a real device.
Run from the project folder:  python3 tools/check_imports.py
"""
import ast
import glob
import sys

ALLOWED = {
    "asyncio", "base64", "ctypes", "decky", "functools", "json", "os", "re", "shutil", "socket",
    "subprocess", "threading", "time", "urllib.error", "urllib.parse", "urllib.request",
    "sys", "struct",  # struct: Decky itself (aiohttp) needs it
    "netcec_adb",     # our own ADB client in py_modules
}

paths = sys.argv[1:] or ["main.py"] + sorted(glob.glob("py_modules/*.py"))
bad = []
for path in paths:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        bad += [(path, node.lineno, n) for n in names if n not in ALLOWED]

if bad:
    for path, line, name in bad:
        print(f"{path}:{line}: '{name}' is not in the list of modules known to work in Decky's Python")
    sys.exit(1)
print("imports ok")
