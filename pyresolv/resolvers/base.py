"""Resolver abstraction for the `resolve` stage + a name registry.

Ported the shared mechanics from enrich_csv_with_gunter (api/gunter/resolve.py):
ThreadPool (max_workers), a cache keyed by the unique key-column value,
idempotent skipping of already-enriched rows (`_is_already_enriched`), and
writing the RESOLVE_COLUMNS back into the DataFrame. A subclass implements
only `resolve_one(key) -> dict`.
"""
from __future__ import annotations

import abc
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Type

import pandas as pd
from tqdm import tqdm

from pyresolv.i18n import _
from pyresolv.io import open_input, open_output
from pyresolv.schema import PANDAS_READ_KWARGS, RESOLVE_COLUMNS

RESOLVERS: Dict[str, Type["Resolver"]] = {}


def register_resolver(name: str):
    """Decorator that registers a resolver in the RESOLVERS registry by name."""

    def _decorator(cls: Type["Resolver"]) -> Type["Resolver"]:
        RESOLVERS[name] = cls
        return cls

    return _decorator


def get_resolver(name: str) -> "Resolver":
    try:
        cls = RESOLVERS[name]
    except KeyError:
        available = ", ".join(sorted(RESOLVERS)) or _("<no resolvers registered>")
        raise ValueError(
            _("Unknown resolver '%(name)s'. Available resolvers: %(available)s")
            % {"name": name, "available": available}
        ) from None
    return cls()


def _normalize_key(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _is_already_enriched(row: pd.Series) -> bool:
    return all(str(row.get(col, "")).strip() for col in RESOLVE_COLUMNS)


class Resolver(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def resolve_one(self, key: str) -> dict:
        """Return a dict with the RESOLVE_COLUMNS for a single key value."""
        raise NotImplementedError

    def _empty_result(self) -> dict:
        return {col: "" for col in RESOLVE_COLUMNS}

    def enrich(
        self,
        df: pd.DataFrame,
        key_column: str,
        max_workers: int,
        skip_already_enriched: bool = True,
    ) -> pd.DataFrame:
        """Enrich an in-memory DataFrame in place (adds/fills RESOLVE_COLUMNS)
        and return it. This is the shared core used both by the path-based
        `resolve` (read -> enrich -> write) and by the single-process pipeline
        engine (Variant B), which passes the running frame directly."""
        if max_workers < 1:
            raise ValueError(_("max_workers must be >= 1"))

        if key_column not in df.columns:
            raise ValueError(_("CSV has no column '%(col)s'") % {"col": key_column})

        for col in RESOLVE_COLUMNS:
            if col not in df.columns:
                df[col] = ""

        # Nothing to enrich on an empty frame (e.g. collect returned 0 rows) —
        # and boolean-indexing a 0-row DataFrame collapses away all columns,
        # so short-circuit before the mask logic below.
        if df.empty:
            print(_("Nothing to enrich, the file is already filled or empty."), file=sys.stderr)
            return df

        if skip_already_enriched:
            already_enriched_mask = df.apply(_is_already_enriched, axis=1)
            rows_to_enrich = df[~already_enriched_mask]
        else:
            already_enriched_mask = None
            rows_to_enrich = df

        normalized_key_series = df[key_column].map(_normalize_key)
        keys_to_enrich: List[str] = sorted(
            {k for k in rows_to_enrich[key_column].map(_normalize_key).tolist() if k}
        )

        print(_("Total rows: %(n)s") % {"n": f"{len(df):,}"}, file=sys.stderr)
        print(
            _("Unique '%(col)s' values to resolve: %(n)s")
            % {"col": key_column, "n": f"{len(keys_to_enrich):,}"},
            file=sys.stderr,
        )
        print(_("Threads: %(n)d") % {"n": max_workers}, file=sys.stderr)

        if not keys_to_enrich:
            print(_("Nothing to enrich, the file is already filled or empty."), file=sys.stderr)
        else:
            cache: Dict[str, dict] = {}

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_key = {executor.submit(self.resolve_one, k): k for k in keys_to_enrich}

                for future in tqdm(
                    as_completed(future_to_key),
                    total=len(future_to_key),
                    desc=_("Resolving via %(name)s") % {"name": self.name},
                ):
                    key = future_to_key[future]
                    try:
                        cache[key] = future.result()
                    except Exception as e:
                        print(
                            _("[%(name)s][%(key)s] unexpected error: %(err)s")
                            % {"name": self.name, "key": key, "err": e},
                            file=sys.stderr,
                        )
                        cache[key] = self._empty_result()

            mask = normalized_key_series.isin(cache.keys())
            if already_enriched_mask is not None:
                mask &= ~already_enriched_mask

            for col in RESOLVE_COLUMNS:
                df.loc[mask, col] = normalized_key_series[mask].map(lambda k: cache[k][col])

        return df

    def resolve(
        self,
        input_path: Optional[str],
        output_path: Optional[str],
        key_column: str,
        max_workers: int,
        skip_already_enriched: bool = True,
    ) -> int:
        with open_input(input_path) as in_f:
            df = pd.read_csv(in_f, **PANDAS_READ_KWARGS)

        df = self.enrich(df, key_column, max_workers, skip_already_enriched)

        with open_output(output_path) as out_f:
            df.to_csv(out_f, index=False)

        return len(df)
