"""pyresolv localization via gettext.

Strings in the code are English (that IS the msgid). The Russian translation
lives in the compiled catalog `pyresolv/locale/ru/LC_MESSAGES/pyresolv.mo`
(the human-readable source is `po/ru.po`).

The language is chosen in priority order:
    1. the `lang` argument (the `--lang` flag);
    2. the environment — LANGUAGE / LC_ALL / LC_MESSAGES / LANG (read by gettext);
    3. English — via `fallback=True` the msgid is returned as-is.

Usage in modules:
    from pyresolv.i18n import _, ngettext
    print(_("Aggregated %(n)s rows") % {"n": n})

`_`/`ngettext` read the current translation from a module global, so calling
`setup(lang)` once at the very start of `main()` is enough — every subsequent
call (including building argparse help) sees the chosen language.
"""
from __future__ import annotations

import gettext as _gettext
from pathlib import Path
from typing import Optional

DOMAIN = "pyresolv"
LOCALEDIR = Path(__file__).parent / "locale"

# Default: an identity "translation" — the msgid is returned as-is (English).
# Overridden in setup().
_translation: _gettext.NullTranslations = _gettext.NullTranslations()


def setup(lang: Optional[str] = None) -> _gettext.NullTranslations:
    """Select the language and activate the translation.

    If lang is given (the --lang flag) -> load that language; otherwise
    languages=None and gettext reads the environment itself. fallback=True: if
    the catalog isn't found, the English msgid is returned and nothing raises.
    """
    global _translation
    languages = [lang] if lang else None
    _translation = _gettext.translation(
        DOMAIN, localedir=str(LOCALEDIR), languages=languages, fallback=True
    )
    return _translation


def _(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)
