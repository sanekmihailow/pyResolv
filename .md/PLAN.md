# План: рефакторинг pyResolv в расширяемый набор стадий-фильтров

## Context

Сейчас проект — два почти дублирующихся монолита:
- `get_dst_ip_ranges.py` — сбор из OpenSearch (Graylog-бэкенд) по окнам времени + merge + trim + aggregate + enrich, всё внутри `main()` и переключается булевыми флагами (`--merge`, `--aggregate`, `--enrich`).
- `resolver.py` — отдельно тот же trim + aggregate для готового CSV.
- `api/gunter/resolve.py` — обогащение IP через Gunter.

Проблемы, которые устраняем:
1. Операционные параметры (`OPENSEARCH_URL`, `INDEX`, `STREAM_ID`, `SRC_IP_LIST`, `SRC_IP_REGEX`, `GUNTER_BASE_URL`, таймауты, число воркеров) зашиты в код.
2. Единственный источник (OpenSearch/Graylog) и единственный резолвер (Gunter) жёстко вшиты — нельзя добавить Elasticsearch/Mongo или самописный резолвер без правки ядра.
3. Схема данных (`DROP_COLS`, `GROUP_COLS`) продублирована в двух файлах.
4. Набор булевых флагов вместо чёткой модели «одна стадия = одно действие».

Целевой результат: набор composable-стадий (Unix-фильтры), соединяемых шелл-пайпами; источники и резолверы — плагины по имени; конфигурация типизирована и вынесена из кода.

## Зафиксированные решения

- **Композиция:** шелл-пайпы. Одна стадия = один запуск, читает stdin/`-i`, пишет stdout/`-o`. In-process пайплайн по конфигу — НЕ делаем (можно добавить позже поверх тех же стадий).
- **Конфиг:** `pydantic-settings` поверх `.env`, вложенные модели с namespace на интеграцию (`GRAYLOG__URL`, `GUNTER__BASE_URL` и т.д.).
- **Aggregate:** по умолчанию текущий pandas full-load (быстро, векторно). Флаг `--streaming` (+`--chunk-size`, дефолт 500_000) переключает в чанковый pandas для очень больших файлов (сотни млн строк) — читаем кусками, `groupby` на каждом куске, суммируем частичные счётчики.
- **merge:** остаётся как узкая стадия «склеить несколько входов в один поток».
- **Выбор источника:** `--source` необязателен. Приоритет: CLI `--source` → `settings.default_source` → `"graylog"`.
- **Выбор резолвера:** `--resolver` необязателен. Приоритет: CLI `--resolver` → `settings.default_resolver` → `"gunter"`.
- **Формат «провода» между стадиями:** CSV с фиксированным заголовком (заголовок несёт схему).

## Целевая структура

```
pyresolv/
  config.py          # pydantic-settings: вложенные настройки на интеграцию, load из .env
  schema.py          # CANONICAL_COLUMNS, DROP_COLS, GROUP_COLS, порядок сортировки — единый источник
  io.py              # read_records(path|stdin)->Iterator[dict]; write_records(iter, path|stdout); CSV
  pipeline.py        # диспетчер --type -> стадия
  cli.py             # argparse; экспортирует main(), точка входа: if __name__ == "__main__": main()
  sources/
    base.py          # ABC Source + реестр SOURCES + @register_source; общая итерация окон времени
    graylog.py       # перенос логики get_dst_ip_ranges (OpenSearch _search + search_after)
  stages/
    trim.py          # перенос trim_csv (чанковое чтение уже есть)
    aggregate.py     # aggregate_csv + режим --streaming (чанковый)
    merge.py         # перенос merge_files_by_creation_time -> склейка нескольких -i/stdin
  resolvers/
    base.py          # ABC Resolver + реестр RESOLVERS; ОБЩЕЕ: ThreadPool, кэш по ключу, идемпотентный пропуск
    gunter.py        # перенос api/gunter/resolve.py: только geo-lookup + whois (resolve_one)
```

Пакет делаем настоящим (`__init__.py`), т.к. сейчас `api/` — namespace-пакет без `__init__.py` и требует запуска из корня. Точку входа оставляем в классической идиоме: `cli.py` определяет `main()`, вызов через `if __name__ == "__main__": main()` (без `__main__.py`).

## CLI (spec)

Общие флаги:
- `--type {collect,trim,merge,aggregate,resolve}` — обязательный, какая стадия.
- `-i, --input PATH` — вход; по умолчанию stdin. Для `collect` игнорируется (источник генерирует). Для `merge` допускается несколько раз.
- `-o, --output PATH` — выход; по умолчанию stdout.

Пер-стадийные:
- `collect`: `--source NAME` (дефолт из конфига → graylog), `--start`, `--end`, `--time-unit {d,h}`. Фильтры источника (список/regex SrcIP) — из конфига.
- `aggregate`: `--streaming` (флаг, дефолт off), `--chunk-size N` (дефолт 500_000).
- `resolve`: `--resolver NAME` (дефолт из конфига → gunter), `--key-column` (дефолт `DstIP`), `--workers N`.

Пример композиции:
```bash
pyresolv --type collect --source graylog --start 5 --end 0 --time-unit h \
  | pyresolv --type trim \
  | pyresolv --type aggregate \
  | pyresolv --type resolve --resolver gunter -o out.csv
```

## Ключевые абстракции (переиспользуем существующую логику)

**Source** (`sources/base.py`): ABC с `fetch(windows) -> Iterator[dict]`; база даёт итерацию окон времени (`build_time_windows`, `shift_now`, `build_time_expr` из текущего `get_dst_ip_ranges.py`). `graylog.py` реализует только `fetch_window(gte, lt)` — перенос `build_payload` + `process_window` (пагинация search_after, фильтр IPv4/не-приватный DstIP). Реестр `SOURCES[name]` + декоратор `@register_source("graylog")`.

**Resolver** (`resolvers/base.py`): вся общая механика из `enrich_csv_with_gunter` — ThreadPool (`max_workers`, дефолт из конфига), кэш по ключу, идемпотентный пропуск через `_is_already_enriched`, обратная запись колонок `country/asn/asn_descr/contacts`. Подкласс реализует только `resolve_one(key) -> dict`. `gunter.py` = `_fetch_country` + `_fetch_whois` + `_extract_contacts`. Реестр `RESOLVERS[name]`.

**Schema** (`schema.py`): переносим `DROP_COLS`, `GROUP_COLS`, кандидаты сортировки — единожды. Источники маппят сырые поля → каноничные.

**Config** (`config.py`): вложенные `BaseSettings` — `GraylogSettings`, `GunterSettings`, плюс корневые `default_source`, `default_resolver`. Значения из `.env` (создать `.env.example`). Валидация на старте.

## Миграция текущего кода

- `get_dst_ip_ranges.py`: окна → `sources/base.py`; payload+пагинация → `sources/graylog.py`; `trim_csv` → `stages/trim.py`; `aggregate_csv` → `stages/aggregate.py`; `merge_files_by_creation_time` → `stages/merge.py`; константы → `config.py`.
- `api/gunter/resolve.py`: generic → `resolvers/base.py`, HTTP-специфика → `resolvers/gunter.py`.
- `main()`: сохраняется как единая точка входа в `cli.py` (идиома `if __name__ == "__main__": main()`); из старого `main()` в `get_dst_ip_ranges.py` переносится только диспетчеризация аргументов в стадии, сама функция не удаляется.
- `resolver.py`: удаляем (его функциональность = `trim` + `aggregate` стадии).
- `requirements.txt`: добавить `pydantic-settings` (тянет `python-dotenv`).
- Починить попутно: `SRC_IP_REGEX` с лишним `\` (`r"10\.8\.\139\.\d+"`); tqdm total в байтах vs update по in-memory размеру.

## Verification

- Малый эталонный CSV (5–10 строк, как в обсуждении) прогнать через каждую стадию по отдельности, сверить выход.
- Композиция пайпом end-to-end: `collect|trim|aggregate|resolve`.
- **Ключевой тест эквивалентности:** `aggregate` без флага и `aggregate --streaming` на одном входе должны дать идентичный отсортированный результат.
- `resolve` дважды подряд по одному файлу — второй прогон не дёргает сеть (идемпотентность `_is_already_enriched`).
- `--source` не задан → падаем в `graylog`; заданный неизвестный `--source`/`--resolver` → понятная ошибка из реестра.
- Отсутствие обязательной настройки в `.env` → валидатор pydantic падает с внятным сообщением на старте, а не в середине.
- `collect` требует живой OpenSearch — проверяем на реальном стенде либо мокаем HTTP; остальные стадии проверяются офлайн.
