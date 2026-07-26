#!/usr/bin/env python3
"""Minimal pure-Python .po -> .mo compiler (no external dependencies).

Needed because the environment may not have GNU gettext (`msgfmt`) or Babel.
It understands what our catalogs actually use: `msgid`/`msgstr`, plurals
(`msgid_plural` + `msgstr[N]`), multi-line quoted values, and the header
(empty `msgid ""`). The binary format is the one read by the standard
`gettext.GNUTranslations`.

Untranslated entries (empty msgstr) are NOT written to the .mo — so gettext
falls back to the English msgid rather than substituting an empty string. The
header (msgid "") is always included.

Usage:
    python tools/msgfmt.py po/ru.po -o pyresolv/locale/ru/LC_MESSAGES/pyresolv.mo
"""
from __future__ import annotations

import argparse
import array
import ast
import struct
import sys
from pathlib import Path
from typing import Dict, List


def _unquote(token: str) -> str:
    """Parse a quoted PO string (with escape sequences)."""
    token = token.strip()
    if not token.startswith('"'):
        raise ValueError(f"Expected a quoted string, got: {token!r}")
    return ast.literal_eval(token)


def parse_po(text: str) -> List[dict]:
    """Parse a .po into a list of entries: {msgid, [msgid_plural], msgstr|plural}."""
    entries: List[dict] = []
    cur: dict = {}
    state = None  # 'msgid' | 'msgid_plural' | 'msgstr' | ('plural', n)

    def flush() -> None:
        nonlocal cur
        if "msgid" in cur:
            entries.append(cur)
        cur = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("msgid_plural"):
            cur["msgid_plural"] = _unquote(line[len("msgid_plural"):])
            state = "msgid_plural"
        elif line.startswith("msgid"):
            flush()
            cur = {"msgid": _unquote(line[len("msgid"):])}
            state = "msgid"
        elif line.startswith("msgstr["):
            idx = int(line[line.index("[") + 1: line.index("]")])
            cur.setdefault("plural", {})[idx] = _unquote(line[line.index("]") + 1:])
            state = ("plural", idx)
        elif line.startswith("msgstr"):
            cur["msgstr"] = _unquote(line[len("msgstr"):])
            state = "msgstr"
        elif line.startswith('"'):
            val = _unquote(line)
            if state == "msgid":
                cur["msgid"] += val
            elif state == "msgid_plural":
                cur["msgid_plural"] += val
            elif state == "msgstr":
                cur["msgstr"] = cur.get("msgstr", "") + val
            elif isinstance(state, tuple):
                cur["plural"][state[1]] += val

    flush()
    return entries


def build_catalog(entries: List[dict]) -> Dict[bytes, bytes]:
    """List of entries -> {id_bytes: str_bytes} for serializing into a .mo."""
    catalog: Dict[bytes, bytes] = {}
    for e in entries:
        msgid = e["msgid"]
        if "msgid_plural" in e:
            forms = e.get("plural", {})
            count = (max(forms) + 1) if forms else 0
            str_val = "\x00".join(forms.get(i, "") for i in range(count))
            id_val = msgid + "\x00" + e["msgid_plural"]
            has_translation = any(forms.get(i) for i in range(count))
        else:
            str_val = e.get("msgstr", "")
            id_val = msgid
            has_translation = bool(str_val)

        # The header (empty msgid) is always kept; other empty translations are
        # skipped so there is a fallback to the msgid.
        if msgid == "" or has_translation:
            catalog[id_val.encode("utf-8")] = str_val.encode("utf-8")
    return catalog


def generate_mo(catalog: Dict[bytes, bytes]) -> bytes:
    """Serialize the catalog into the binary .mo format (as in CPython Tools/i18n)."""
    keys = sorted(catalog)
    offsets = []
    ids = strs = b""
    for key in keys:
        offsets.append((len(ids), len(key), len(strs), len(catalog[key])))
        ids += key + b"\x00"
        strs += catalog[key] + b"\x00"

    keystart = 7 * 4 + 16 * len(keys)
    valuestart = keystart + len(ids)
    koffsets = []
    voffsets = []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]

    output = struct.pack(
        "Iiiiiii",
        0x950412DE,          # magic
        0,                   # version
        len(keys),           # number of entries
        7 * 4,               # offset of table with original strings
        7 * 4 + len(keys) * 8,  # offset of table with translations
        0, 0,                # size/offset of hash table (unused)
    )
    output += array.array("i", koffsets + voffsets).tobytes()
    output += ids
    output += strs
    return output


def compile_po(po_path: Path, mo_path: Path) -> int:
    entries = parse_po(po_path.read_text(encoding="utf-8"))
    catalog = build_catalog(entries)
    mo_path.parent.mkdir(parents=True, exist_ok=True)
    mo_path.write_bytes(generate_mo(catalog))
    return len(catalog)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile a .po file into a .mo file.")
    parser.add_argument("po", help="path to input .po")
    parser.add_argument("-o", "--output", required=True, help="path to output .mo")
    args = parser.parse_args()

    n = compile_po(Path(args.po), Path(args.output))
    print(f"Compiled {args.po} -> {args.output} ({n} entries)", file=sys.stderr)


if __name__ == "__main__":
    main()
