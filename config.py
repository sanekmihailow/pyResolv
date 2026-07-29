"""Typed pyresolv configuration: pydantic-settings on top of .env.

All operational constants that used to be hardcoded (OPENSEARCH_URL, INDEX,
STREAM_ID, SRC_IP_LIST, SRC_IP_CIDR, GUNTER_BASE_URL, timeouts, worker counts)
are read from here.

Nested per-integration settings use the `__` delimiter:

    GRAYLOG__URL=http://localhost:9200
    GRAYLOG__STREAM_ID=69a0061e05036e88c213d8be
    GUNTER__BASE_URL=https://gunter-resolve.example.ru/api

See `.env.example` in the repo root.

The `graylog`/`gunter` sections are deliberately Optional: stages that don't
need a given integration (trim/merge/aggregate) must not require it to be
configured. Stages that do (collect -> graylog, resolve -> gunter) must access
it via `require_graylog()`/`require_gunter()`, which raise a clear `ConfigError`
if the section is entirely absent. If a section is PARTIALLY filled in (e.g.
GRAYLOG__URL set but the required GRAYLOG__STREAM_ID missing), pydantic fails
validation at Settings() construction time — i.e. at the very start of the run,
not mid-stream.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pyresolv.i18n import _


class ConfigError(RuntimeError):
    """Configuration for a specific integration (source/resolver) is missing."""


class GraylogSettings(BaseModel):
    url: str = Field(description="Base OpenSearch/Graylog URL, e.g. http://localhost:9200")
    index: str = Field(default="device_net", description="Index name without the date suffix")
    stream_id: str = Field(description="Graylog stream ID used to filter documents")
    search_size: int = Field(default=5000, description="search_after page size")
    request_timeout: int = Field(default=300, description="HTTP request timeout to OpenSearch, seconds")
    src_ip_list: List[str] = Field(default_factory=list, description="List of SrcIPs for the terms filter")
    src_ip_cidr: List[str] = Field(
        default_factory=list,
        description=(
            "List of source subnets in CIDR (e.g. 10.2.83.0/24, 10.2.83.0/25). Filters "
            "collect (server-side prefix + exact client-side check) and defines the "
            "aggregate --out-dir per-subnet buckets. Empty -> collect filter not applied"
        ),
    )


class GunterSettings(BaseModel):
    base_url: str = Field(description="Base URL of the Gunter API")
    request_timeout: int = Field(default=30, description="HTTP request timeout to Gunter, seconds")
    max_workers: int = Field(default=3, description="Default number of resolving threads")


class ResolveSettings(BaseModel):
    """Settings for the native resolvers (rdap/whois/geo_maxmind/default). All
    optional with defaults, so it constructs with no .env."""
    mmdb_path: Optional[str] = Field(
        default=None,
        description="Path to a local MaxMind GeoLite2-City .mmdb for the geo_maxmind resolver. "
        "Unset -> geo_maxmind yields nothing (country falls back to RDAP/WHOIS codes)",
    )
    rdap_timeout: int = Field(default=10, description="Socket timeout for the rdap resolver, seconds")
    whois_timeout: int = Field(default=15, description="Socket timeout for the whois resolver, seconds")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    default_source: str = Field(default="graylog", description="Default source for --type collect")
    default_resolver: str = Field(default="default", description="Default resolver for --type resolve")
    min_uniq_count: int = Field(
        default=1,
        ge=1,
        description=(
            "count threshold for --type aggregate: groups with count < this value "
            "are dropped from the result. 1 = no filtering (keep everything). "
            "Overridden by the --min-count flag."
        ),
    )

    graylog: Optional[GraylogSettings] = None
    gunter: Optional[GunterSettings] = None
    resolve: ResolveSettings = Field(default_factory=ResolveSettings)

    def require_graylog(self) -> GraylogSettings:
        if self.graylog is None:
            raise ConfigError(_(
                "Source 'graylog' is not configured: set GRAYLOG__URL and "
                "GRAYLOG__STREAM_ID in .env (see .env.example)."
            ))
        return self.graylog

    def require_gunter(self) -> GunterSettings:
        if self.gunter is None:
            raise ConfigError(_(
                "Resolver 'gunter' is not configured: set GUNTER__BASE_URL "
                "in .env (see .env.example)."
            ))
        return self.gunter


@lru_cache
def get_settings() -> Settings:
    return Settings()
