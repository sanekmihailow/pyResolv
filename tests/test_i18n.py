"""Localization tests: language selection (--lang / environment / EN default),
translating simple strings and plurals (3 Russian forms). The ru catalog is
taken from the compiled pyresolv/locale/ru/LC_MESSAGES/pyresolv.mo — so the test
also checks that the .po -> .mo compilation is correct."""
from __future__ import annotations

import pytest

from pyresolv import i18n
from pyresolv.cli import _preparse_lang

_ENV_VARS = ["LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"]


@pytest.fixture(autouse=True)
def restore_translation():
    """Don't leak the selected language into other tests."""
    saved = i18n._translation
    yield
    i18n._translation = saved


def _clear_locale_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_default_is_english(monkeypatch):
    _clear_locale_env(monkeypatch)
    i18n.setup(None)  # no flag, no environment -> English msgid
    assert i18n._("Which stage to run") == "Which stage to run"


def test_lang_ru_translates():
    i18n.setup("ru")
    assert i18n._("Which stage to run") == "Какую стадию запустить"
    assert i18n._("Error: %(err)s") % {"err": "x"} == "Ошибка: x"


def test_lang_en_returns_msgid():
    i18n.setup("en")  # no en catalog -> fallback to the msgid
    assert i18n._("Which stage to run") == "Which stage to run"


def test_env_selects_ru(monkeypatch):
    _clear_locale_env(monkeypatch)
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    i18n.setup(None)
    assert i18n._("Which stage to run") == "Какую стадию запустить"


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "Агрегировано 1 строка"),
        (2, "Агрегировано 2 строки"),
        (5, "Агрегировано 5 строк"),
        (21, "Агрегировано 21 строка"),
        (11, "Агрегировано 11 строк"),
    ],
)
def test_russian_plural_forms(n, expected):
    i18n.setup("ru")
    got = i18n.ngettext("Aggregated %(n)s row", "Aggregated %(n)s rows", n) % {"n": n}
    assert got == expected


def test_preparse_lang():
    assert _preparse_lang(["--lang", "ru", "--type", "trim"]) == "ru"
    assert _preparse_lang(["--type", "aggregate"]) is None
    # the flag is present even among other arguments
    assert _preparse_lang(["--type", "resolve", "--lang", "en"]) == "en"
