"""Tests for the configuration: nested env vars (the __ delimiter), Optional
sections for stages that don't need an integration, and clear errors on
incomplete/missing configuration."""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from pyresolv.config import ConfigError, Settings


def _settings_with_env_file(monkeypatch, path):
    # env_file=None -> don't mix in the developer's/CI's .env during the test.
    monkeypatch.setattr(
        Settings,
        "model_config",
        SettingsConfigDict(
            env_file=None,
            env_nested_delimiter="__",
            extra="ignore",
            case_sensitive=False,
        ),
    )


def test_no_env_at_all_leaves_sections_none(monkeypatch):
    _settings_with_env_file(monkeypatch, None)
    for k in list(__import__("os").environ):
        if k.startswith("GRAYLOG") or k.startswith("GUNTER"):
            monkeypatch.delenv(k, raising=False)

    s = Settings()
    assert s.graylog is None
    assert s.gunter is None
    assert s.default_source == "graylog"
    assert s.default_resolver == "default"

    with pytest.raises(ConfigError):
        s.require_graylog()
    with pytest.raises(ConfigError):
        s.require_gunter()


def test_full_graylog_env_populates_section(monkeypatch):
    _settings_with_env_file(monkeypatch, None)
    monkeypatch.setenv("GRAYLOG__URL", "http://localhost:9200")
    monkeypatch.setenv("GRAYLOG__STREAM_ID", "abc123")
    monkeypatch.delenv("GUNTER__BASE_URL", raising=False)

    s = Settings()
    assert s.graylog.url == "http://localhost:9200"
    assert s.graylog.stream_id == "abc123"
    assert s.graylog.index == "device_net"  # default
    assert s.gunter is None


def test_partial_graylog_env_raises_validation_error(monkeypatch):
    _settings_with_env_file(monkeypatch, None)
    monkeypatch.setenv("GRAYLOG__URL", "http://localhost:9200")
    monkeypatch.delenv("GRAYLOG__STREAM_ID", raising=False)

    with pytest.raises(ValidationError):
        Settings()


def test_src_ip_list_parsed_from_json_env(monkeypatch):
    _settings_with_env_file(monkeypatch, None)
    monkeypatch.setenv("GRAYLOG__URL", "http://localhost:9200")
    monkeypatch.setenv("GRAYLOG__STREAM_ID", "abc123")
    monkeypatch.setenv("GRAYLOG__SRC_IP_LIST", '["10.2.83.129","10.2.83.130"]')

    s = Settings()
    assert s.graylog.src_ip_list == ["10.2.83.129", "10.2.83.130"]


def test_src_ip_cidr_parsed_as_list_from_json_env(monkeypatch):
    _settings_with_env_file(monkeypatch, None)
    monkeypatch.setenv("GRAYLOG__URL", "http://localhost:9200")
    monkeypatch.setenv("GRAYLOG__STREAM_ID", "abc123")
    monkeypatch.setenv("GRAYLOG__SRC_IP_CIDR", '["10.2.83.0/24","10.2.83.0/25"]')

    s = Settings()
    assert s.graylog.src_ip_cidr == ["10.2.83.0/24", "10.2.83.0/25"]


def test_src_ip_cidr_defaults_to_empty_list(monkeypatch):
    _settings_with_env_file(monkeypatch, None)
    monkeypatch.setenv("GRAYLOG__URL", "http://localhost:9200")
    monkeypatch.setenv("GRAYLOG__STREAM_ID", "abc123")
    monkeypatch.delenv("GRAYLOG__SRC_IP_CIDR", raising=False)

    s = Settings()
    assert s.graylog.src_ip_cidr == []


def test_resolve_workers_defaults_to_three(monkeypatch):
    _settings_with_env_file(monkeypatch, None)
    monkeypatch.delenv("RESOLVE__WORKERS", raising=False)
    assert Settings().resolve.workers == 3


def test_resolve_workers_parsed_from_env(monkeypatch):
    _settings_with_env_file(monkeypatch, None)
    monkeypatch.setenv("RESOLVE__WORKERS", "7")
    assert Settings().resolve.workers == 7


def test_resolve_workers_rejects_below_one(monkeypatch):
    _settings_with_env_file(monkeypatch, None)
    monkeypatch.setenv("RESOLVE__WORKERS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_env_example_src_ip_cidr_is_valid():
    """GRAYLOG__SRC_IP_CIDR in .env.example is a JSON array of parseable CIDRs."""
    import json
    from pathlib import Path

    from pyresolv.subnets import parse_cidrs

    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    cidr_lines = [
        line for line in env_example.read_text(encoding="utf-8").splitlines()
        if line.startswith("GRAYLOG__SRC_IP_CIDR=")
    ]
    assert len(cidr_lines) == 1
    cidrs = json.loads(cidr_lines[0].split("=", 1)[1])
    assert isinstance(cidrs, list) and cidrs
    # All entries parse without raising.
    assert len(parse_cidrs(cidrs)) == len(set(cidrs))
