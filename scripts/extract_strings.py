"""List the translatable strings used in templates and Python.

    python scripts/extract_strings.py            # every key, one per line
    python scripts/extract_strings.py --missing  # keys missing from a locale
"""

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CALL = re.compile(r"""\b_\(\s*(['"])(?P<key>(?:[^'"\\]|\\.)*)\1""", re.DOTALL)


def from_templates():
    keys = set()
    for path in (ROOT / "inventory" / "templates").rglob("*.html"):
        for match in CALL.finditer(path.read_text()):
            keys.add(match.group("key").replace("\\'", "'").replace('\\"', '"'))
    return keys


def from_python():
    keys = set()
    for path in (ROOT / "inventory").rglob("*.py"):
        text = path.read_text()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "_" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
    return keys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--missing", nargs="*", help="Report keys missing from these locales")
    args = parser.parse_args()

    keys = sorted(from_templates() | from_python())
    if not args.missing:
        for key in keys:
            print(key)
        print(f"\n{len(keys)} strings", file=sys.stderr)
        return

    from inventory.i18n.catalog import CATALOGS

    for locale in args.missing:
        catalog = CATALOGS.get(locale, {})
        missing = [key for key in keys if key not in catalog]
        print(f"{locale}: {len(missing)} missing of {len(keys)}")
        for key in missing:
            print(f"    {key}")
        unused = sorted(set(catalog) - set(keys))
        if unused:
            print(f"  {len(unused)} unused entries:")
            for key in unused:
                print(f"    {key}")


if __name__ == "__main__":
    main()