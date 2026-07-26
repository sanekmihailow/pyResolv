# PyResolv

**Русский** | [English](README.md)

Обработка логов сетевого экрана набором composable-стадий-фильтров
(collect/trim/merge/aggregate/resolve), соединяемых шелл-пайпами — в духе
классических Unix-утилит: одна стадия = один процесс, читает stdin/`-i`,
пишет stdout/`-o`.

## Стадии

```
collect  ->  trim  ->  merge  ->  aggregate  ->  resolve
```

- **collect** — тянет соединения из источника (`--source`, по умолчанию
  `graylog`/OpenSearch) по окнам времени, пишет CSV с заголовком
  `timestamp,SrcIP,DstIP,DstPort,ac_action,ac_rule_name,url_domain,url_path`.
  Фильтрует IPv4 с не-приватным `DstIP`. Единственная стадия, требующая
  сетевого доступа к источнику.
- **trim** — читает CSV чанками, отбрасывает служебные колонки
  (`timestamp`, `SrcPort`, `source`, `message`).
- **merge** — склеивает несколько CSV-входов (несколько `-i`) в один поток.
- **aggregate** — группирует по
  `SrcIP -> DstIP -> DstPort -> ac_action -> url_domain -> ac_rule_name`,
  считает `count`, сортирует (`count` по убыванию, остальное по
  возрастанию). По умолчанию — потоковый режим `--streaming`
  (+`--chunk-size`, по умолчанию 500 000) с ограниченной памятью, безопасный
  для очень больших файлов; `--no-streaming` включает полный pandas full-load
  (быстрее на маленьких файлах). Оба режима дают побайтово идентичный
  результат. `--min-count` (или env `MIN_UNIQ_COUNT`, по умолчанию `1` =
  оставить всё) выбрасывает группы с `count` ниже порога.
- **resolve** — обогащает строки по ключевой колонке (`--key-column`, по
  умолчанию `DstIP`) через резолвер (`--resolver`, по умолчанию `gunter`):
  добавляет `country`, `asn`, `asn_descr`, `contacts`. Идемпотентно: уже
  заполненные строки не резолвятся повторно и не дёргают сеть.

Источники и резолверы — плагины, регистрируемые по имени
(`pyresolv/sources/`, `pyresolv/resolvers/`); чтобы добавить новый источник
или резолвер, не нужно менять ядро.

## Структура проекта

```
pyResolv/
├── pyresolv/                    package (installed via `pip install -e .`)
│   ├── __init__.py              package version and short description
│   ├── cli.py                   entry point: argparse, exports main()
│   ├── pipeline.py              --type -> stage dispatcher (Variant A)
│   ├── runner.py                single-process pipeline engine + YAML config (Variant B)
│   ├── config.py                typed configuration (pydantic-settings) from .env
│   ├── schema.py                single source of the CSV schema: columns, sort order, read kwargs
│   ├── io.py                    open_input/open_output: path -> file, None/'-' -> stdin/stdout
│   ├── i18n.py                  localization via gettext (_/ngettext, setup)
│   ├── stages/                  filter stages
│   │   ├── collect.py           pulls records from a source by time windows
│   │   ├── trim.py              drops service columns (chunked reading)
│   │   ├── merge.py             concatenates several CSV inputs into one stream
│   │   └── aggregate.py         group-by + count, sorting, --streaming/--min-count
│   ├── sources/                 source plugins for `collect`
│   │   ├── base.py              Source ABC + SOURCES registry + time windows
│   │   └── graylog.py           OpenSearch _search + search_after pagination
│   ├── resolvers/               resolver plugins for `resolve`
│   │   ├── base.py              Resolver ABC + RESOLVERS registry + ThreadPool/cache/idempotency
│   │   └── gunter.py            geo-lookup + whois HTTP calls
│   └── locale/                  compiled translation catalogs (.mo)
│       └── ru/LC_MESSAGES/pyresolv.mo
├── po/                          translation sources (.po) and template (.pot)
│   └── ru.po
├── tools/
│   └── msgfmt.py                pure-Python .po -> .mo compiler (no GNU gettext/Babel)
├── tests/                       pytest tests
├── .env.example                 configuration sample (copy to .env)
├── pyproject.toml               package metadata, `pyresolv` console script
├── requirements.txt             dependencies (pandas, tqdm, requests, pydantic-settings)
├── Makefile                     i18n-extract / i18n-update / i18n-compile targets
├── init.sh                      environment setup + .mo compilation
```

## Требования

- Python 3.10+

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# или как пакет с командой pyresolv:
pip install -e .
```

## Конфигурация

Конфигурация — типизированная (`pydantic-settings`), читается из `.env`.
Скопируйте `.env.example` в `.env` и заполните нужные секции:

```bash
cp .env.example .env
```

Секции `GRAYLOG__*`/`GUNTER__*` нужны только тем стадиям, которые
используют соответствующую интеграцию (`collect` -> graylog, `resolve` ->
gunter). `trim`/`merge`/`aggregate` работают вовсе без `.env`.

## Запуск

```bash
# Отдельная стадия
pyresolv --type trim -i input.csv -o trimmed.csv

# Композиция пайпом
pyresolv --type collect --source graylog --start 5 --end 0 --time-unit h \
  | pyresolv --type trim \
  | pyresolv --type aggregate \
  | pyresolv --type resolve --resolver gunter -o out.csv

# Большой файл, потоковая агрегация
pyresolv --type aggregate --streaming --chunk-size 500000 -i trimmed.csv -o aggregated.csv
```

Без установки пакета можно запускать как модуль:
`python -m pyresolv.cli --type trim ...` или через
`./.venv/bin/pyresolv ...`.

## Единый пайплайн в одном процессе (`pyresolv run`)

Кроме композиции шелл-пайпами (выше, **Вариант A**) есть **Вариант B** —
весь пайплайн выполняется в **одном процессе** по YAML-конфигу. Между шагами
течёт живой `pandas.DataFrame`, без пересериализации в CSV на каждом стыке.

```bash
pyresolv run --config pipeline.yaml -o out.csv
```

```yaml
# pipeline.yaml — список шагов. Шаг это либо имя стадии (`trim`),
# либо отображение «имя: {параметры}».
- collect: {source: graylog, start: 5, end: 0}
- trim
- aggregate: {min_count: 20}
- resolve: {resolver: gunter}
```

- `-i/--input` — начальный вход для первого шага (по умолчанию stdin);
  игнорируется, если первый шаг `collect` (он генерирует данные сам).
- `-o/--output` — финальный выход (по умолчанию stdout).
- Параметры шагов валидируются строго (`extra="forbid"`): опечатка вроде
  `min_counts` падает с понятной ошибкой **до** запуска, а не в середине.
- Один общий лог/прогресс: каждый шаг печатает `[i/n] <шаг>` в stderr.

**Когда что использовать:**

- **Вариант A** (шелл-пайпы `--type ... | --type ...`) — потоковый, с
  ограниченной памятью (`aggregate --streaming`), для очень больших файлов и
  разовых экспериментов из командной строки; шаги можно перезапускать по
  отдельности, промежуточные результаты остаются на диске.
- **Вариант B** (`run --config`) — быстрее (нет пересериализации между
  шагами) и с единым конфигом, но держит датасет **целиком в памяти**.
  Подходит для данных, влезающих в RAM; для гигантских входов берите
  Вариант A со streaming.

Оба варианта используют одни и те же стадии и дают одинаковый результат:
`run` с `trim -> aggregate` побайтово совпадает с
`trim | aggregate --no-streaming`.

Требует `PyYAML` (входит в `requirements.txt`).

## Удаление промежуточных файлов (`--delete`/`--del`)

Флаг `--delete` (алиас `--del`) доступен на любой стадии: после **успешной**
записи выхода стадия удаляет свои входные `-i` файлы. Так пошаговый прогон
через файлы оставляет на диске только финальный результат:

```bash
pyresolv --type collect                        -o connections.csv
pyresolv --type trim      --del -i connections.csv -o trimmed.csv    # удалит connections.csv
pyresolv --type aggregate --del -i trimmed.csv     -o aggregated.csv # удалит trimmed.csv
# на диске остаётся только aggregated.csv
```

Гарантии безопасности:

- удаление — только после успешного завершения стадии; если стадия упала,
  вход остаётся нетронутым;
- никогда не удаляет stdin и выходной файл (случай `-i` == `-o` пропускается);
- для `collect` не применяется (вход не используется);
- каждое удаление логируется в stderr.

## Язык вывода (RU/EN)

Все сообщения, ошибки и help локализованы через gettext. Язык выбирается в
порядке приоритета:

1. флаг `--lang {ru,en}`;
2. окружение — `LANGUAGE` / `LC_ALL` / `LC_MESSAGES` / `LANG`;
3. английский (по умолчанию).

```bash
pyresolv --lang ru --type aggregate -i trimmed.csv -o out.csv   # русский вывод
LANG=ru_RU.UTF-8 pyresolv --type aggregate -i trimmed.csv        # тоже русский
pyresolv --type aggregate -i trimmed.csv                         # английский
```

### Как добавить/поправить перевод

Строки в коде — на английском (это `msgid`), русские переводы лежат в
`po/ru.po`, компилируются в `pyresolv/locale/ru/LC_MESSAGES/pyresolv.mo`.

```bash
# 1. (опционально) вытащить новые строки из кода в шаблон — нужен Babel:
./.venv/bin/pip install -e '.[i18n]'
make i18n-extract       # обновляет po/pyresolv.pot
make i18n-update        # подмешивает новые строки в po/ru.po

# 2. вписать переводы в po/ru.po, затем скомпилировать (без внешних зависимостей):
make i18n-compile       # -> pyresolv/locale/ru/LC_MESSAGES/pyresolv.mo
```

Компиляция `.po → .mo` идёт встроенным `tools/msgfmt.py`, поэтому GNU gettext
и Babel для запуска и компиляции **не нужны** — Babel требуется только для
извлечения строк (`i18n-extract`). Чтобы добавить новый язык — заведите
`po/<lang>.po`, впишите его в `LANGS` в `Makefile` и запустите `make i18n-compile`.
