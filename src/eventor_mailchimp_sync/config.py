"""Configuration from environment variables plus an optional TOML file.

Everything a run needs comes from the environment (see ``.env.example``);
``config.toml`` only adds event-series tag mappings, which are too structured
for environment variables. Secrets never live in the repository.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from eventor_client import AU_BASE_URL
from eventor_client.models import Event

MemberSource = Literal["auto", "memberships", "persons"]

# Mailchimp merge-field *tags* are limited to 10 characters.
MERGE_TAG_MAX_LENGTH = 10
# Explicit "switch this off" value. An empty variable means "use the default", because
# GitHub Actions passes unset repository variables through as empty strings.
DISABLED = "none"


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class SeriesRule:
    """Map events to a series tag by ID and/or by name pattern."""

    name: str
    tag: str
    event_ids: frozenset[int] = frozenset()
    name_patterns: tuple[re.Pattern[str], ...] = ()

    def matches(self, event: Event) -> bool:
        if event.id in self.event_ids or event.parent_event_id in self.event_ids:
            return True
        return any(p.search(event.name) for p in self.name_patterns)


@dataclass(frozen=True, slots=True)
class SyncConfig:
    eventor_base_url: str
    eventor_api_key: str
    mailchimp_api_key: str
    mailchimp_server_prefix: str
    mailchimp_list_id: str
    eventor_cache_dir: Path | None = None
    eventor_cache_ttl_seconds: float = 3600.0
    window_months: int = 12
    previous_year_until_month: int = 3
    member_source: MemberSource = "auto"
    require_paid: bool = True
    member_tag: str | None = "member"
    entrant_tag: str = "entrant"
    include_starts: bool = False
    persons_contact_index: bool = False
    update_names: bool = True
    merge_club: str = "CLUB"
    merge_membership_year: str = "MEMBERYEAR"
    merge_eventor_id: str = "EVENTORID"
    merge_mobile: str | None = "PHONE"
    phone_country_code: str | None = None
    series: tuple[SeriesRule, ...] = ()
    report_path: Path = Path("report.json")
    config_path: Path | None = None
    fields_missing: tuple[str, ...] = field(default=(), repr=False)

    @property
    def is_australian_instance(self) -> bool:
        host = urlparse(self.eventor_base_url).hostname or ""
        return host.endswith("orienteering.asn.au")

    @property
    def effective_member_source(self) -> Literal["memberships", "persons"]:
        if self.member_source == "auto":
            return "memberships" if self.is_australian_instance else "persons"
        return self.member_source


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _merge_tag(env: Mapping[str, str], name: str, default: str) -> str:
    value = (env.get(name) or default).strip().upper()
    if len(value) > MERGE_TAG_MAX_LENGTH:
        raise ConfigError(
            f"{name} must be at most {MERGE_TAG_MAX_LENGTH} characters (Mailchimp limit), "
            f"got {value!r}"
        )
    return value


def default_phone_country_code(base_url: str) -> str | None:
    """Country calling code implied by the Eventor instance, for E.164 formatting."""
    host = urlparse(base_url).hostname or ""
    if host.endswith("orienteering.asn.au"):
        return "61"
    if host.endswith("orientering.se"):
        return "46"
    if host.endswith("orientering.no"):
        return "47"
    return None


def server_prefix_from_key(api_key: str) -> str | None:
    """Mailchimp keys end in ``-usNN``; that suffix is the data centre prefix."""
    if "-" in api_key:
        suffix = api_key.rsplit("-", 1)[1].strip()
        if re.fullmatch(r"[a-z]{2}\d{1,3}", suffix):
            return suffix
    return None


def load_series(path: Path) -> tuple[SeriesRule, ...]:
    """Read ``[series.<name>]`` tables from a TOML file."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    rules: list[SeriesRule] = []
    for name, table in (data.get("series") or {}).items():
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: [series.{name}] must be a table")
        tag = str(table.get("tag") or f"series-{name}").strip()
        ids = table.get("event_ids") or []
        patterns = table.get("name_patterns") or []
        try:
            event_ids = frozenset(int(i) for i in ids)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{path}: [series.{name}].event_ids must be integers") from exc
        compiled = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(str(pattern), re.IGNORECASE))
            except re.error as exc:
                raise ConfigError(
                    f"{path}: [series.{name}] bad name pattern {pattern!r}: {exc}"
                ) from exc
        if not event_ids and not compiled:
            raise ConfigError(f"{path}: [series.{name}] needs event_ids or name_patterns")
        rules.append(
            SeriesRule(name=name, tag=tag, event_ids=event_ids, name_patterns=tuple(compiled))
        )
    return tuple(rules)


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    config_path: Path | None = None,
    require_mailchimp: bool = True,
) -> SyncConfig:
    """Build a :class:`SyncConfig` from ``env`` (default: ``os.environ``).

    ``config_path`` overrides ``SYNC_CONFIG``; when neither is set and a
    ``config.toml`` exists in the working directory it is used.
    """
    env = os.environ if env is None else env
    missing: list[str] = []

    eventor_key = (env.get("EVENTOR_API_KEY") or "").strip()
    if not eventor_key:
        missing.append("EVENTOR_API_KEY")
    mailchimp_key = (env.get("MAILCHIMP_API_KEY") or "").strip()
    list_id = (env.get("MAILCHIMP_LIST_ID") or "").strip()
    if require_mailchimp:
        if not mailchimp_key:
            missing.append("MAILCHIMP_API_KEY")
        if not list_id:
            missing.append("MAILCHIMP_LIST_ID")
    prefix = (env.get("MAILCHIMP_SERVER_PREFIX") or "").strip() or server_prefix_from_key(
        mailchimp_key
    )
    if require_mailchimp and not prefix:
        missing.append("MAILCHIMP_SERVER_PREFIX (could not derive it from MAILCHIMP_API_KEY)")
    if missing:
        raise ConfigError("missing required configuration: " + ", ".join(missing))

    base_url = (env.get("EVENTOR_BASE_URL") or AU_BASE_URL).strip().rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise ConfigError(f"EVENTOR_BASE_URL must be an http(s) URL, got {base_url!r}")

    source = (env.get("SYNC_MEMBER_SOURCE") or "auto").strip().lower()
    if source not in {"auto", "memberships", "persons"}:
        raise ConfigError(
            f"SYNC_MEMBER_SOURCE must be auto, memberships or persons; got {source!r}"
        )

    cache_dir_raw = (env.get("EVENTOR_CACHE_DIR") or "").strip()
    member_tag: str | None = (env.get("SYNC_MEMBER_TAG") or "member").strip()
    if member_tag.lower() == DISABLED:
        member_tag = None
    entrant_tag = (env.get("SYNC_ENTRANT_TAG") or "entrant").strip()

    chosen_config = config_path
    if chosen_config is None:
        raw = (env.get("SYNC_CONFIG") or "").strip()
        if raw:
            chosen_config = Path(raw)
        elif Path("config.toml").is_file():
            chosen_config = Path("config.toml")
    series: tuple[SeriesRule, ...] = ()
    if chosen_config is not None:
        if not chosen_config.is_file():
            raise ConfigError(f"config file not found: {chosen_config}")
        series = load_series(chosen_config)

    mobile_raw = (env.get("MAILCHIMP_MERGE_MOBILE") or "").strip()
    merge_mobile = (
        None
        if mobile_raw.lower() == DISABLED
        else _merge_tag(env, "MAILCHIMP_MERGE_MOBILE", "PHONE")
    )
    country_raw = (env.get("SYNC_PHONE_COUNTRY_CODE") or "").strip().lstrip("+")
    if not country_raw:
        phone_country_code: str | None = default_phone_country_code(base_url)
    elif country_raw.lower() == DISABLED:
        phone_country_code = None
    else:
        if not country_raw.isdigit():
            raise ConfigError(
                f"SYNC_PHONE_COUNTRY_CODE must be digits or 'none', got {country_raw!r}"
            )
        phone_country_code = country_raw

    previous_until = _int(env, "SYNC_PREVIOUS_YEAR_UNTIL_MONTH", 3)
    if previous_until > 12:
        raise ConfigError("SYNC_PREVIOUS_YEAR_UNTIL_MONTH must be between 0 and 12")

    return SyncConfig(
        eventor_base_url=base_url,
        eventor_api_key=eventor_key,
        mailchimp_api_key=mailchimp_key,
        mailchimp_server_prefix=prefix or "",
        mailchimp_list_id=list_id,
        eventor_cache_dir=Path(cache_dir_raw) if cache_dir_raw else None,
        eventor_cache_ttl_seconds=float(_int(env, "EVENTOR_CACHE_TTL_SECONDS", 3600)),
        window_months=_int(env, "SYNC_WINDOW_MONTHS", 12, minimum=1),
        previous_year_until_month=previous_until,
        member_source=source,  # type: ignore[arg-type]
        require_paid=_bool(env.get("SYNC_REQUIRE_PAID"), True),
        member_tag=member_tag or None,
        entrant_tag=entrant_tag,
        include_starts=_bool(env.get("SYNC_INCLUDE_STARTS"), False),
        persons_contact_index=_bool(env.get("SYNC_PERSONS_CONTACT_INDEX"), False),
        update_names=_bool(env.get("SYNC_UPDATE_NAMES"), True),
        merge_club=_merge_tag(env, "MAILCHIMP_MERGE_CLUB", "CLUB"),
        merge_membership_year=_merge_tag(env, "MAILCHIMP_MERGE_MEMBERSHIP_YEAR", "MEMBERYEAR"),
        merge_eventor_id=_merge_tag(env, "MAILCHIMP_MERGE_EVENTOR_ID", "EVENTORID"),
        merge_mobile=merge_mobile,
        phone_country_code=phone_country_code,
        series=series,
        report_path=Path((env.get("SYNC_REPORT_PATH") or "report.json").strip()),
        config_path=chosen_config,
    )
