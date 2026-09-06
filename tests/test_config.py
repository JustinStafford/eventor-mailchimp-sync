"""Configuration loading from environment variables and TOML."""

from __future__ import annotations

from pathlib import Path

import pytest

from eventor_client.models import Event
from eventor_mailchimp_sync.config import (
    ConfigError,
    load_config,
    load_series,
    server_prefix_from_key,
)

BASE_ENV = {
    "EVENTOR_API_KEY": "ev-key",
    "MAILCHIMP_API_KEY": "test-key-us21",
    "MAILCHIMP_LIST_ID": "abc123",
}


def test_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(BASE_ENV)
    assert cfg.eventor_base_url == "https://eventor.orienteering.asn.au/api"
    assert cfg.is_australian_instance
    assert cfg.effective_member_source == "memberships"
    assert cfg.mailchimp_server_prefix == "us21"
    assert cfg.window_months == 12
    assert cfg.previous_year_until_month == 3
    assert cfg.require_paid is True
    assert cfg.member_tag == "member"
    assert cfg.entrant_tag == "entrant"
    assert cfg.merge_club == "CLUB"
    assert cfg.merge_membership_year == "MEMBERYEAR"
    assert cfg.merge_mobile == "PHONE"
    assert cfg.phone_country_code == "61"
    assert cfg.series == ()
    assert cfg.report_path == Path("report.json")
    assert cfg.eventor_cache_dir is None


def test_missing_required_lists_every_variable():
    with pytest.raises(ConfigError) as info:
        load_config({})
    message = str(info.value)
    assert "EVENTOR_API_KEY" in message
    assert "MAILCHIMP_API_KEY" in message
    assert "MAILCHIMP_LIST_ID" in message


def test_mailchimp_optional_for_eventor_only_commands():
    cfg = load_config({"EVENTOR_API_KEY": "k"}, require_mailchimp=False)
    assert cfg.mailchimp_api_key == ""


def test_server_prefix_derivation_and_override():
    assert server_prefix_from_key("abc-us21") == "us21"
    assert server_prefix_from_key("no-suffix-here") is None
    cfg = load_config({**BASE_ENV, "MAILCHIMP_SERVER_PREFIX": "us5"})
    assert cfg.mailchimp_server_prefix == "us5"
    with pytest.raises(ConfigError, match="MAILCHIMP_SERVER_PREFIX"):
        load_config({**BASE_ENV, "MAILCHIMP_API_KEY": "nodash"})


def test_phone_settings():
    cfg = load_config({**BASE_ENV, "EVENTOR_BASE_URL": "https://eventor.orientering.se/api/"})
    assert cfg.phone_country_code == "46"
    cfg = load_config({**BASE_ENV, "EVENTOR_BASE_URL": "https://eventor.orienteering.sport/api"})
    assert cfg.phone_country_code is None
    cfg = load_config(
        {**BASE_ENV, "SYNC_PHONE_COUNTRY_CODE": "+64", "MAILCHIMP_MERGE_MOBILE": "mobile"}
    )
    assert cfg.phone_country_code == "64"
    assert cfg.merge_mobile == "MOBILE"
    cfg = load_config(
        {**BASE_ENV, "SYNC_PHONE_COUNTRY_CODE": "none", "MAILCHIMP_MERGE_MOBILE": "NONE"}
    )
    assert cfg.phone_country_code is None
    assert cfg.merge_mobile is None


def test_empty_variables_mean_default_not_disabled():
    """GitHub Actions passes unset repository variables as empty strings."""
    cfg = load_config(
        {
            **BASE_ENV,
            "SYNC_MEMBER_TAG": "",
            "MAILCHIMP_MERGE_MOBILE": "",
            "SYNC_PHONE_COUNTRY_CODE": "",
            "SYNC_WINDOW_MONTHS": "",
            "SYNC_MEMBER_SOURCE": "",
            "EVENTOR_BASE_URL": "",
            "MAILCHIMP_SERVER_PREFIX": "",
            "SYNC_CONFIG": "",
        }
    )
    assert cfg.member_tag == "member"
    assert cfg.merge_mobile == "PHONE"
    assert cfg.phone_country_code == "61"
    assert cfg.window_months == 12
    assert cfg.effective_member_source == "memberships"
    assert cfg.mailchimp_server_prefix == "us21"
    with pytest.raises(ConfigError, match="SYNC_PHONE_COUNTRY_CODE"):
        load_config({**BASE_ENV, "SYNC_PHONE_COUNTRY_CODE": "AU"})


def test_non_australian_instance_uses_persons():
    cfg = load_config({**BASE_ENV, "EVENTOR_BASE_URL": "https://eventor.orientering.se/api/"})
    assert cfg.eventor_base_url == "https://eventor.orientering.se/api"
    assert not cfg.is_australian_instance
    assert cfg.effective_member_source == "persons"
    cfg = load_config({**BASE_ENV, "SYNC_MEMBER_SOURCE": "memberships"})
    assert cfg.effective_member_source == "memberships"
    with pytest.raises(ConfigError, match="SYNC_MEMBER_SOURCE"):
        load_config({**BASE_ENV, "SYNC_MEMBER_SOURCE": "magic"})


def test_booleans_ints_and_tags():
    cfg = load_config(
        {
            **BASE_ENV,
            "SYNC_REQUIRE_PAID": "false",
            "SYNC_INCLUDE_STARTS": "yes",
            "SYNC_WINDOW_MONTHS": "6",
            "SYNC_PREVIOUS_YEAR_UNTIL_MONTH": "0",
            "SYNC_MEMBER_TAG": "none",
            "SYNC_ENTRANT_TAG": "raced",
            "EVENTOR_CACHE_DIR": ".cache",
            "EVENTOR_CACHE_TTL_SECONDS": "60",
            "MAILCHIMP_MERGE_CLUB": "club",
        }
    )
    assert cfg.require_paid is False
    assert cfg.include_starts is True
    assert cfg.window_months == 6
    assert cfg.previous_year_until_month == 0
    assert cfg.member_tag is None
    assert cfg.entrant_tag == "raced"
    assert cfg.eventor_cache_dir == Path(".cache")
    assert cfg.eventor_cache_ttl_seconds == 60.0
    assert cfg.merge_club == "CLUB"
    with pytest.raises(ConfigError, match="SYNC_WINDOW_MONTHS"):
        load_config({**BASE_ENV, "SYNC_WINDOW_MONTHS": "0"})
    with pytest.raises(ConfigError, match="10 characters"):
        load_config({**BASE_ENV, "MAILCHIMP_MERGE_MEMBERSHIP_YEAR": "MEMBERSHIP_YEAR"})


def test_series_from_toml(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[series.sprint]
tag = "series-sprint"
event_ids = [5001]
name_patterns = ["sprint series"]

[series.bush]
name_patterns = ["^bush"]
"""
    )
    rules = load_series(path)
    assert [r.tag for r in rules] == ["series-sprint", "series-bush"]
    sprint, bush = rules
    assert sprint.matches(Event(id=5001, name="Anything"))
    assert sprint.matches(Event(id=9, name="Twilight SPRINT SERIES 4"))
    assert not sprint.matches(Event(id=9, name="Bush Classic"))
    assert sprint.matches(Event(id=10, name="Day 2", parent_event_id=5001))
    assert bush.matches(Event(id=9, name="Bush Classic"))
    assert not bush.matches(Event(id=9, name="The bush"))

    cfg = load_config(BASE_ENV, config_path=path)
    assert len(cfg.series) == 2
    cfg = load_config({**BASE_ENV, "SYNC_CONFIG": str(path)})
    assert cfg.config_path == path


def test_series_validation(tmp_path: Path):
    bad = tmp_path / "bad.toml"
    bad.write_text('[series.x]\ntag = "t"\n')
    with pytest.raises(ConfigError, match="needs event_ids or name_patterns"):
        load_series(bad)
    bad.write_text('[series.x]\nname_patterns = ["("]\n')
    with pytest.raises(ConfigError, match="bad name pattern"):
        load_series(bad)
    bad.write_text("not toml ][")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_series(bad)
    with pytest.raises(ConfigError, match="not found"):
        load_config(BASE_ENV, config_path=tmp_path / "missing.toml")


def test_config_toml_in_cwd_is_picked_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("[series.s]\nevent_ids = [1]\n")
    cfg = load_config(BASE_ENV)
    assert cfg.series[0].tag == "series-s"
