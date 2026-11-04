"""Choosing the database target (local Docker vs hosted Supabase)."""

import pytest

from discourse_maps import config


def test_default_target_is_local(monkeypatch):
    monkeypatch.delenv("DM_TARGET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = config.get_settings()
    assert settings.target == "local"
    assert settings.database_url == config.LOCAL_DATABASE_URL


def test_cloud_target_uses_cloud_url(monkeypatch):
    monkeypatch.setenv("DM_TARGET", "cloud")
    monkeypatch.setenv("DATABASE_URL_CLOUD", "postgresql://u:p@db.example.supabase.co:5432/postgres")
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: False)
    settings = config.get_settings()
    assert settings.target == "cloud"
    assert "example.supabase.co" in settings.database_url


def test_cloud_target_without_url_fails(monkeypatch):
    monkeypatch.setenv("DM_TARGET", "cloud")
    monkeypatch.delenv("DATABASE_URL_CLOUD", raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: False)
    with pytest.raises(RuntimeError, match="DATABASE_URL_CLOUD"):
        config.get_settings()


def test_unknown_target_fails(monkeypatch):
    monkeypatch.setenv("DM_TARGET", "staging")
    with pytest.raises(ValueError):
        config.get_settings()
