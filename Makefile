# pyResolv localization (gettext).
#
# Compiling .po -> .mo needs no external dependencies (the bundled
# tools/msgfmt.py). Extracting/updating .po requires Babel:
#     ./.venv/bin/pip install -e '.[i18n]'

PY      ?= ./.venv/bin/python
PYBABEL ?= ./.venv/bin/pybabel
DOMAIN  := pyresolv
POT     := po/$(DOMAIN).pot
LANGS   := ru

.PHONY: i18n-extract i18n-update i18n-compile

## Extract all translatable strings from the code into po/pyresolv.pot (needs Babel)
i18n-extract:
	$(PYBABEL) extract -o $(POT) -k _ -k ngettext:1,2 --no-wrap \
		--project=$(DOMAIN) pyresolv/

## Merge new/changed strings from .pot into the existing .po files (needs Babel)
i18n-update:
	@for lang in $(LANGS); do \
		$(PYBABEL) update -i $(POT) -d po -D $(DOMAIN) -l $$lang --no-wrap ; \
	done

## Compile .po -> .mo (no external dependencies)
i18n-compile:
	@for lang in $(LANGS); do \
		$(PY) tools/msgfmt.py po/$$lang.po -o pyresolv/locale/$$lang/LC_MESSAGES/$(DOMAIN).mo ; \
	done
