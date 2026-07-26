# Локализация pyResolv (gettext).
#
# Компиляция .po -> .mo не требует внешних зависимостей (встроенный
# tools/msgfmt.py). Извлечение/обновление .po требуют Babel:
#     ./.venv/bin/pip install -e '.[i18n]'

PY      ?= ./.venv/bin/python
PYBABEL ?= ./.venv/bin/pybabel
DOMAIN  := pyresolv
POT     := po/$(DOMAIN).pot
LANGS   := ru

.PHONY: i18n-extract i18n-update i18n-compile

## Извлечь все переводимые строки из кода в po/pyresolv.pot (нужен Babel)
i18n-extract:
	$(PYBABEL) extract -o $(POT) -k _ -k ngettext:1,2 --no-wrap \
		--project=$(DOMAIN) pyresolv/

## Подмешать новые/изменённые строки из .pot в существующие .po (нужен Babel)
i18n-update:
	@for lang in $(LANGS); do \
		$(PYBABEL) update -i $(POT) -d po -D $(DOMAIN) -l $$lang --no-wrap ; \
	done

## Скомпилировать .po -> .mo (без внешних зависимостей)
i18n-compile:
	@for lang in $(LANGS); do \
		$(PY) tools/msgfmt.py po/$$lang.po -o pyresolv/locale/$$lang/LC_MESSAGES/$(DOMAIN).mo ; \
	done
