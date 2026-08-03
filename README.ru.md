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
  оставить всё) выбрасывает группы с `count` ниже порога. `--out-dir DIR`
  (вместо `-o`) раскладывает результат по одному CSV на подсеть из
  `GRAYLOG__SRC_IP_CIDR` (строки вне подсетей → файл `other`); в имени файла —
  временной срез, напр.
  `aggregation_10.2.83.0-24__2026-07-23__2026-07-28__time-12-10.csv`.
- **resolve** — обогащает строки по ключевой колонке (`--key-column`, по
  умолчанию `DstIP`) через резолвер (`--resolver`), добавляя `country`, `asn`,
  `asn_descr`, `contacts`. Резолверы: **`default`** (по умолчанию —
  самодостаточная цепочка GEO → RDAP → WHOIS), `rdap` / `whois` (один
  провайдер, через `ipwhois`), `geo_maxmind` (страна из локального MaxMind
  `.mmdb` — задать `RESOLVE__MMDB_PATH` и `pip install -e '.[geo]'`) и `gunter`
  (внешний HTTP-сервис). Идемпотентно: уже заполненные строки не резолвятся
  повторно и не дёргают сеть. Результаты также кэшируются между прогонами
  (персистентный кэш, `--cache` включён по умолчанию): каждый уникальный ключ
  резолвится не чаще одного раза до истечения записи — дата истечения из
  резолва + 1 день, либо 1-е число следующего месяца, если даты нет (пустые/
  сбойные ответы не кэшируются). Backend — `RESOLVE__CACHE` (`default` SQLite /
  `redis` / `none`); `--no-cache` выключает.

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
│   ├── subnets.py               ipaddress helpers: CIDR parsing, octet prefix, labels, split filenames
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
│   │   ├── default_chain.py     `default` resolver: chain GEO -> RDAP -> WHOIS
│   │   ├── rdap.py              ASN/contacts/country via ipwhois RDAP
│   │   ├── whois.py             same via legacy port-43 WHOIS
│   │   ├── geo_maxmind.py       country from a local MaxMind .mmdb (geoip2)
│   │   ├── _rdap.py             shared RDAP helpers (contacts extraction)
│   │   └── gunter.py            external Gunter HTTP service (geo-lookup + whois)
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

Вложенные настройки используют разделитель `__` (`GRAYLOG__URL`, `RESOLVE__RDAP_TIMEOUT`, …).
Списочные значения (`SRC_IP_LIST`, `SRC_IP_CIDR`) — это JSON-массивы, напр. `["10.2.83.0/24"]`.

**Верхний уровень**

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DEFAULT_SOURCE` | `graylog` | Источник для `collect`, когда не задан `--source` |
| `DEFAULT_RESOLVER` | `default` | Резолвер для `resolve`, когда не задан `--resolver` |
| `MIN_UNIQ_COUNT` | `1` | Значение по умолчанию для `aggregate --min-count` (отбрасывать группы ниже порога; `1` = оставить все) |

**`GRAYLOG__*`** (нужны для `collect`)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `GRAYLOG__URL` | — *(обязательно)* | Базовый URL OpenSearch/Graylog, напр. `http://localhost:9200` |
| `GRAYLOG__STREAM_ID` | — *(обязательно)* | ID потока Graylog для фильтрации документов |
| `GRAYLOG__INDEX` | `device_net` | Имя индекса без суффикса-даты |
| `GRAYLOG__SEARCH_SIZE` | `5000` | Размер страницы `search_after` |
| `GRAYLOG__REQUEST_TIMEOUT` | `300` | Таймаут HTTP-запроса к OpenSearch, секунды |
| `GRAYLOG__SRC_IP_LIST` | `[]` | JSON-массив точных SrcIP (фильтр `terms`) |
| `GRAYLOG__SRC_IP_CIDR` | `[]` | JSON-массив подсетей-источников в CIDR; также задаёт корзины для `aggregate --out-dir` |
| `GRAYLOG__SRC_IP_MATCH_MODE` | `or` | Как объединять LIST и CIDR, если заданы оба: `or` (в любом) / `and` (в обоих) |

**`GUNTER__*`** (нужны для `resolve --resolver gunter`)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `GUNTER__BASE_URL` | — *(обязательно)* | Базовый URL API Gunter |
| `GUNTER__REQUEST_TIMEOUT` | `30` | Таймаут HTTP-запроса к Gunter, секунды |

**`RESOLVE__*`** (нативные резолверы `default`/`rdap`/`whois`/`geo_maxmind`; все опциональны)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `RESOLVE__MMDB_PATH` | *(не задано)* | Путь к локальному MaxMind GeoLite2 `.mmdb` для `geo_maxmind`; не задано → geo ничего не даёт |
| `RESOLVE__RDAP_TIMEOUT` | `10` | Таймаут сокета для резолвера `rdap`, секунды |
| `RESOLVE__RDAPSS` | `true` | Фолбэк `rdap`: запрос к агрегатору rdap.ss (перед bootstrap), когда прямой лукап вернул пусто |
| `RESOLVE__RDAPSS_URL` | `https://rdap.ss/api/query?q=` | URL запроса rdap.ss (пустой тоже отключает фолбэк) |
| `RESOLVE__RDAPSS_TIMEOUT` | `15` | HTTP-таймаут фолбэка rdap.ss, секунды |
| `RESOLVE__RDAP_BOOTSTRAP` | `true` | При пустом RDAP-ответе повторить через RDAP-bootstrap (только как фолбэк) |
| `RESOLVE__WHOIS_TIMEOUT` | `15` | Таймаут сокета для резолвера `whois`, секунды |
| `RESOLVE__TCINET` | `false` | Фолбэк `whois`: доменный whois ТЦИ (`whois.tcinet.ru:43`) для .ru/.su/.рф — только домены, включать для прогонов с `--key-column url_domain` |
| `RESOLVE__TCINET_HOST` | `whois.tcinet.ru` | Хост доменного whois ТЦИ (порт 43) |
| `RESOLVE__TCINET_TIMEOUT` | `15` | Таймаут сокета фолбэка tcinet, секунды |
| `RESOLVE__WORKERS` | `3` | Число потоков резолвинга по умолчанию для **любого** резолвера (переопределяется `--workers`) |
| `RESOLVE__CACHE` | `default` | Backend персистентного кэша резолва: `default` (файл SQLite), `redis` или `none` |
| `RESOLVE__CACHE_PATH` | `~/.cache/pyresolv/resolve-cache.sqlite` | Файл SQLite для backend'а `default` |
| `RESOLVE__REDIS_URL` | `redis://localhost:6379/0` | URL Redis для backend'а `redis` |
| `RESOLVE__REDIS_PREFIX` | `pyresolv:resolve:` | Префикс ключей для backend'а `redis` |

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

## Параметры командной строки

**Общие** (для каждой стадии)

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--type {collect,trim,merge,aggregate,resolve}` | — *(обязательно)* | Какую стадию запускать |
| `-i, --input PATH` | stdin | Вход; можно повторять для `merge`; игнорируется для `collect` |
| `-o, --output PATH` | stdout | Выход |
| `--delete, --del` | выкл | После успеха удалить входные файлы `-i` (никогда stdin или `-o`) |
| `--lang {ru,en}` | окружение | Принудительный язык вывода |
| `--version` | | Показать версию и выйти |

**`collect`**

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--source NAME` | `DEFAULT_SOURCE` / `graylog` | Источник данных |
| `--start N` | `1` | На сколько единиц назад начинается диапазон |
| `--end N` | `0` | На сколько единиц назад заканчивается диапазон |
| `--time-unit {d,h}` | `h` | Единица времени (дни / часы) |

**`aggregate`**

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--streaming / --no-streaming` | `--streaming` | Потоково (ограниченная память) vs. полная загрузка в память |
| `--chunk-size N` | `500000` | Размер чанка для `--streaming` |
| `--min-count N` | `MIN_UNIQ_COUNT` / `1` | Отбрасывать агрегированные группы с count ниже `N` |
| `--out-dir DIR` | — | Разбить вывод на один CSV на подсеть (взаимоисключимо с `-o`) |

**`resolve`**

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--resolver NAME` | `DEFAULT_RESOLVER` / `default` | `default` / `rdap` / `whois` / `geo_maxmind` / `gunter` |
| `--key-column COL` | `DstIP` | Колонка с IP для резолвинга |
| `--workers N` | `RESOLVE__WORKERS` / `3` | Число потоков резолвинга |
| `--cache` / `--no-cache` | `--cache` | Использовать персистентный кэш резолва (backend из `RESOLVE__CACHE`); `--no-cache` выключает его для прогона |

**`run`** (Вариант B, см. ниже)

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--config, -c PATH` | — *(обязательно)* | YAML-конфиг пайплайна |
| `-i, --input PATH` | stdin | Начальный вход для первого шага |
| `-o, --output PATH` | stdout | Итоговый вывод |

> **Примечание:** для `run` это *единственные* флаги командной строки. Все
> параметры **стадий** (`source`, `start`, `min_count`, `out_dir`, `resolver`,
> `workers`, …) задаются **внутри YAML-конфига** как `имя: {параметр: значение}`,
> а **не** флагом CLI — передать, например, `--out-dir` в `pyresolv run` нельзя,
> это ошибка.

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
- collect: {source: graylog, start: 5, end: 0, time_unit: h}
- trim
- aggregate: {min_count: 20}
- resolve: {resolver: gunter}
```

**Параметры стадий задаются здесь, в YAML — не флагами CLI.** Параметры каждого
шага повторяют флаги стадий Варианта A (`--start` → `start` и т.д.):

| Шаг | Параметры в YAML |
|---|---|
| `collect` | `source`, `start`, `end`, `time_unit` |
| `trim` | *(нет)* |
| `merge` | `inputs` (список путей к CSV) |
| `aggregate` | `min_count`, `out_dir`, `start`, `end`, `time_unit` |
| `resolve` | `resolver`, `key_column`, `workers` |

Например, `aggregate --out-dir DIR` из Варианта A здесь превращается в
`- aggregate: {out_dir: DIR, start: 5, end: 0, time_unit: h}` (нужен заданный
`GRAYLOG__SRC_IP_CIDR`, а `start`/`end`/`time_unit` формируют временной срез в именах файлов).

- `-i/--input` — начальный вход для первого шага (по умолчанию stdin);
  игнорируется, если первый шаг `collect` (он генерирует данные сам).
- `-o/--output` — финальный выход (по умолчанию stdout); игнорируется, если шаг записал в `out_dir`.
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
