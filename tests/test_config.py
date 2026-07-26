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
    assert s.default_resolver == "gunter"

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


def test_src_ip_regex_parsed_as_list_from_json_env(monkeypatch):
    _settings_with_env_file(monkeypatch, None)
    monkeypatch.setenv("GRAYLOG__URL", "http://localhost:9200")
    monkeypatch.setenv("GRAYLOG__STREAM_ID", "abc123")
    # Inside JSON the backslash is escaped twice.
    monkeypatch.setenv("GRAYLOG__SRC_IP_REGEX", r'["10\\.8\\.139\\.\\d+","10\\.9\\..*"]')

    s = Settings()
    assert s.graylog.src_ip_regex == [r"10\.8\.139\.\d+", r"10\.9\..*"]


def test_src_ip_regex_defaults_to_empty_list(monkeypatch):
    _settings_with_env_file(monkeypatch, None)
    monkeypatch.setenv("GRAYLOG__URL", "http://localhost:9200")
    monkeypatch.setenv("GRAYLOG__STREAM_ID", "abc123")
    monkeypatch.delenv("GRAYLOG__SRC_IP_REGEX", raising=False)

    s = Settings()
    assert s.graylog.src_ip_regex == []


def test_regex_bug_fixed_no_stray_backslash():
    """Historical bug: r"10\\.8\\.\\139\\.\\d+" had a stray "\\" before "139",
    so "\\1" was read as a backreference group. Now GRAYLOG__SRC_IP_REGEX in
    .env.example is a JSON array; we parse it and check that the patterns have
    no spurious "\\139" (we deliberately skip the comment that quotes the old
    bug)."""
    import json
    from pathlib import Path

    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    regex_lines = [
        line for line in env_example.read_text(encoding="utf-8").splitlines()
        if line.startswith("GRAYLOG__SRC_IP_REGEX=")
    ]
    assert len(regex_lines) == 1
    patterns = json.loads(regex_lines[0].split("=", 1)[1])
    assert isinstance(patterns, list) and patterns
    for pattern in patterns:
        assert r"\139" not in pattern
    assert patterns[0] == r"10\.8\.139\.\d+"
