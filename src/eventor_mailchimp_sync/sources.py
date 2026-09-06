"""Pull the *desired* audience state from Eventor.

Every run does a full pull: current-year memberships (plus last year's early in
the year), everyone who entered one of the club's events in a trailing window,
and optionally everyone on published start lists. Observations are then merged
on normalised email into :class:`DesiredContact` records.

Non-member entrants: contact details on ``/entries`` are only guaranteed for the
key owner's own organisation. When an entrant's ``Person`` carries no email, the
member index (built from the membership pull) is consulted by person ID, so
members who entered are always matched. Non-members without an email end up in
:attr:`PullResult.no_email` for the report rather than silently vanishing.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime

from eventor_client import EventorClient, EventorHTTPError, EventorParseError
from eventor_client.models import Event, Organisation
from eventor_mailchimp_sync.config import SyncConfig

log = logging.getLogger(__name__)

EVENT_ID_CHUNK = 50
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalise_email(email: str | None) -> str | None:
    """Lower-case and strip; return ``None`` for empty or clearly invalid values."""
    if email is None:
        return None
    value = email.strip().lower()
    if not value or not _EMAIL_RE.match(value):
        return None
    return value


def normalise_mobile(value: str | None, country_code: str | None) -> str | None:
    """Tidy a phone number into E.164 where possible (``0412 345 678`` -> ``+61412345678``).

    Only digits and a leading ``+`` survive. A leading ``0`` is swapped for the
    country code when one is known; a number that already starts with the country
    code gains a ``+``. Anything shorter than eight digits is treated as absent.
    """
    if value is None:
        return None
    raw = value.strip()
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 8:
        return None
    if plus:
        return f"+{digits}"
    if country_code:
        if digits.startswith("0"):
            return f"+{country_code}{digits[1:]}"
        if digits.startswith(country_code) and len(digits) > len(country_code) + 6:
            return f"+{digits}"
    return digits


def member_tag_for_year(year: int) -> str:
    return f"member-{year}"


def membership_years(now: datetime | date, config: SyncConfig) -> tuple[int, ...]:
    """Years whose memberships count as current: this year, plus last year early on."""
    years = [now.year]
    if now.month <= config.previous_year_until_month:
        years.append(now.year - 1)
    return tuple(years)


def months_before(when: datetime, months: int) -> datetime:
    """Calendar-month subtraction that clamps the day (31 Mar - 1 month -> 28 Feb)."""
    month_index = when.year * 12 + (when.month - 1) - months
    year, month = divmod(month_index, 12)
    month += 1
    last_day = [31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return when.replace(year=year, month=month, day=min(when.day, last_day))


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def managed_tags(config: SyncConfig, years: Iterable[int]) -> frozenset[str]:
    """Every tag this tool owns and will add/remove. Anything else is left alone."""
    tags = {member_tag_for_year(y) for y in years}
    tags.add(config.entrant_tag)
    if config.member_tag:
        tags.add(config.member_tag)
    tags.update(rule.tag for rule in config.series)
    return frozenset(tags)


@dataclass(frozen=True, slots=True)
class Observation:
    """One sighting of a person in one source."""

    person_id: int | None
    given_name: str
    family_name: str
    email: str | None
    source: str
    tags: frozenset[str]
    membership_years: frozenset[int] = frozenset()
    birth_date: date | None = None
    club: str | None = None
    is_member: bool = False
    mobile: str | None = None


@dataclass(frozen=True, slots=True)
class DesiredContact:
    """What one Mailchimp contact should look like after the sync."""

    email: str
    first_name: str
    last_name: str
    tags: frozenset[str]
    membership_years: frozenset[int] = frozenset()
    club: str | None = None
    person_ids: frozenset[int] = frozenset()
    sources: tuple[str, ...] = ()
    shared: bool = False
    mobile: str | None = None

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass(frozen=True, slots=True)
class NoEmailPerson:
    person_id: int | None
    name: str
    sources: tuple[str, ...]
    is_member: bool


@dataclass(slots=True)
class PullResult:
    organisation: Organisation
    membership_years: tuple[int, ...]
    member_source: str
    window_start: datetime
    window_end: datetime
    events: list[Event]
    contacts: list[DesiredContact]
    no_email: list[NoEmailPerson]
    managed_tags: frozenset[str]
    stats: dict[str, int] = field(default_factory=dict)


# -- merging -------------------------------------------------------------------


def _person_key(obs: Observation) -> tuple[object, ...]:
    if obs.person_id is not None:
        return ("id", obs.person_id)
    return ("name", obs.given_name.casefold(), obs.family_name.casefold())


def _primary(group: list[Observation]) -> Observation:
    """Pick the person whose name goes on a shared contact: members first, then oldest."""
    return min(
        enumerate(group),
        key=lambda pair: (
            not pair[1].is_member,
            pair[1].birth_date or date.max,
            pair[1].person_id if pair[1].person_id is not None else float("inf"),
            pair[0],
        ),
    )[1]


def merge_observations(
    observations: Iterable[Observation],
    *,
    phone_country_code: str | None = None,
) -> tuple[list[DesiredContact], list[NoEmailPerson], dict[str, int]]:
    """Group observations by normalised email; collect people with no usable email."""
    by_email: dict[str, list[Observation]] = defaultdict(list)
    no_email: dict[tuple[object, ...], list[Observation]] = defaultdict(list)
    invalid = 0
    for obs in observations:
        email = normalise_email(obs.email)
        if email is None:
            if obs.email and obs.email.strip():
                invalid += 1
            no_email[_person_key(obs)].append(obs)
        else:
            by_email[email].append(obs)

    contacts: list[DesiredContact] = []
    shared = 0
    for email in sorted(by_email):
        group = by_email[email]
        persons = {_person_key(o) for o in group}
        primary = _primary(group)
        is_shared = len(persons) > 1
        shared += is_shared
        tags: set[str] = set()
        years: set[int] = set()
        ids: set[int] = set()
        sources: list[str] = []
        club = primary.club
        mobile = normalise_mobile(primary.mobile, phone_country_code)
        for obs in group:
            tags |= obs.tags
            years |= obs.membership_years
            if obs.person_id is not None:
                ids.add(obs.person_id)
            if obs.source not in sources:
                sources.append(obs.source)
            if club is None and obs.club:
                club = obs.club
            if mobile is None and obs.mobile:
                mobile = normalise_mobile(obs.mobile, phone_country_code)
        contacts.append(
            DesiredContact(
                email=email,
                first_name=primary.given_name.strip(),
                last_name=primary.family_name.strip(),
                tags=frozenset(tags),
                membership_years=frozenset(years),
                club=club,
                person_ids=frozenset(ids),
                sources=tuple(sources),
                shared=is_shared,
                mobile=mobile,
            )
        )

    missing: list[NoEmailPerson] = []
    # Someone seen without an email in one source but with one elsewhere is fine.
    known = {_person_key(o) for group in by_email.values() for o in group}
    for key, group in no_email.items():
        if key in known:
            continue
        first = group[0]
        missing.append(
            NoEmailPerson(
                person_id=first.person_id,
                name=f"{first.given_name} {first.family_name}".strip(),
                sources=tuple(dict.fromkeys(o.source for o in group)),
                is_member=any(o.is_member for o in group),
            )
        )
    missing.sort(key=lambda p: (not p.is_member, p.name.casefold()))
    stats = {"shared_emails": shared, "invalid_emails": invalid, "no_email": len(missing)}
    return contacts, missing, stats


# -- pulling -------------------------------------------------------------------


def _member_observations(
    client: EventorClient,
    config: SyncConfig,
    org: Organisation,
    years: tuple[int, ...],
    now: datetime,
) -> tuple[list[Observation], str, dict[str, int]]:
    assert org.id is not None
    club = org.name or org.short_name
    source = config.effective_member_source
    stats: dict[str, int] = {"memberships": 0, "memberships_unpaid_skipped": 0}
    observations: list[Observation] = []
    base_tags = {config.member_tag} if config.member_tag else set()

    if source == "memberships":
        try:
            for year in years:
                log.info("fetching %d memberships from Eventor", year)
                for m in client.memberships(org.id, year, include_contact_details=True):
                    if config.require_paid and not m.paid:
                        stats["memberships_unpaid_skipped"] += 1
                        continue
                    stats["memberships"] += 1
                    observations.append(
                        Observation(
                            person_id=m.person.id,
                            given_name=m.person.given_name,
                            family_name=m.person.family_name,
                            email=m.email,
                            source=f"membership:{m.year}",
                            tags=frozenset(base_tags | {member_tag_for_year(m.year)}),
                            membership_years=frozenset({m.year}),
                            birth_date=m.person.birth_date,
                            club=club,
                            is_member=True,
                            mobile=m.mobile,
                        )
                    )
            return observations, "memberships", stats
        except EventorHTTPError as exc:
            if exc.status_code != 404 or config.member_source == "memberships":
                raise
            log.warning("/memberships not available on this instance; falling back to /persons")
            observations.clear()

    year = now.year
    log.info("fetching persons attached to organisation %d from Eventor", org.id)
    for p in client.persons_in_organisation(org.id, include_contact_details=True):
        stats["memberships"] += 1
        observations.append(
            Observation(
                person_id=p.id,
                given_name=p.given_name,
                family_name=p.family_name,
                email=p.email,
                source=f"persons:{year}",
                tags=frozenset(base_tags | {member_tag_for_year(year)}),
                membership_years=frozenset({year}),
                birth_date=p.birth_date,
                club=club,
                is_member=True,
                mobile=p.mobile,
            )
        )
    return observations, "persons", stats


def _series_tags(event: Event, config: SyncConfig) -> set[str]:
    return {rule.tag for rule in config.series if rule.matches(event)}


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def pull(client: EventorClient, config: SyncConfig, now: datetime | None = None) -> PullResult:
    """Fetch members, events and entrants and merge them into desired contacts."""
    now = now or datetime.now()
    log.info("asking Eventor which organisation the API key belongs to")
    org = client.organisation_for_api_key()
    log.info("organisation: %s (#%s)", org.name or org.short_name, org.id)
    if org.id is None:
        raise EventorParseError("/organisation/apiKey returned no OrganisationId")
    years = membership_years(now, config)
    window_start = months_before(now, config.window_months)
    window_end = now.replace(hour=23, minute=59, second=59, microsecond=0)

    observations, member_source, stats = _member_observations(client, config, org, years, now)
    member_index: dict[int, Observation] = {}
    for obs in observations:
        if obs.person_id is not None:
            member_index.setdefault(obs.person_id, obs)

    # /entries carries no contact details at all (verified on the AU instance), so an
    # entrant's email has to come from somewhere the club is allowed to see. Members are
    # covered by member_index; optionally everyone still attached to the club in Eventor
    # (lapsed members included) is used as a secondary lookup for entrants only.
    contact_index: dict[int, Observation] = {}
    if config.persons_contact_index and member_source != "persons":
        stats["persons_contact_index"] = 0
        log.info("fetching persons attached to organisation %d for the contact index", org.id)
        for p in client.persons_in_organisation(org.id, include_contact_details=True):
            if p.id is None or p.id in member_index or not p.email:
                continue
            stats["persons_contact_index"] += 1
            contact_index[p.id] = Observation(
                person_id=p.id,
                given_name=p.given_name,
                family_name=p.family_name,
                email=p.email,
                source="persons",
                tags=frozenset(),
                birth_date=p.birth_date,
                club=org.name or org.short_name,
                mobile=p.mobile,
            )

    log.info(
        "fetching events organised by #%d between %s and %s",
        org.id,
        window_start.date(),
        window_end.date(),
    )
    all_events = client.events(
        organisation_ids=[org.id], from_date=window_start, to_date=window_end
    )
    log.info("%d events found", len(all_events))
    events = [
        e
        for e in all_events
        if not e.is_cancelled and (not e.organiser_ids or org.id in e.organiser_ids)
    ]
    stats["events"] = len(events)
    stats["events_cancelled_skipped"] = len(all_events) - len(events)
    by_event_id = {e.id: e for e in events}
    by_race_id = {race.id: e for e in events for race in e.races}

    stats.update(
        {
            "entries": 0,
            "entries_team_skipped": 0,
            "entries_without_person": 0,
            "entrant_email_from_member_index": 0,
            "entrants_other_org": 0,
            "entrants_other_org_with_email": 0,
        }
    )
    entrant_tags = {config.entrant_tag}
    chunks = list(_chunks(sorted(by_event_id), EVENT_ID_CHUNK))
    for index, chunk in enumerate(chunks, 1):
        log.info(
            "fetching entries for %d events (request %d of %d); this is the slow part",
            len(chunk),
            index,
            len(chunks),
        )
        # includeOrganisationElement is deliberately off: it roughly doubles the payload
        # and only names other clubs, whose entrants can never be synced anyway.
        for entry in client.entries(event_ids=chunk, include_person_element=True):
            if entry.is_team:
                stats["entries_team_skipped"] += 1
                continue
            stats["entries"] += 1
            event = by_event_id.get(entry.event_id) if entry.event_id else None
            if event is None:
                for rid in entry.event_race_ids:
                    event = by_race_id.get(rid)
                    if event is not None:
                        break
            tags = set(entrant_tags)
            if event is not None:
                tags |= _series_tags(event, config)
            source = f"entry:{event.id if event else entry.event_id}"

            person = entry.person
            member = member_index.get(entry.person_id) if entry.person_id is not None else None
            known = member or (contact_index.get(entry.person_id) if entry.person_id else None)
            if person is None and known is None:
                stats["entries_without_person"] += 1
                continue
            email = person.email if person is not None else None
            if email is None and known is not None and known.email:
                email = known.email
                key = "entrant_email_from_member_index" if member else "entrant_email_from_persons"
                stats[key] = stats.get(key, 0) + 1
            club = None
            if entry.organisation is not None:
                club = entry.organisation.name or entry.organisation.short_name
            elif entry.organisation_id == org.id or member is not None or known is not None:
                club = org.name or org.short_name
            if member is None and entry.organisation_id != org.id:
                stats["entrants_other_org"] += 1
                if email:
                    stats["entrants_other_org_with_email"] += 1
            observations.append(
                Observation(
                    person_id=entry.person_id,
                    given_name=(person.given_name if person else known.given_name),  # type: ignore[union-attr]
                    family_name=(person.family_name if person else known.family_name),  # type: ignore[union-attr]
                    email=email,
                    source=source,
                    tags=frozenset(tags),
                    birth_date=person.birth_date if person else None,
                    club=club,
                    is_member=member is not None,
                    mobile=(person.mobile if person else None) or (known.mobile if known else None),
                )
            )

    if config.include_starts:
        stats["starts"] = 0
        for index, event in enumerate(events, 1):
            log.info("fetching start list %d of %d (event %d)", index, len(events), event.id)
            start_list = client.event_starts(event.id)
            tags = frozenset(entrant_tags | _series_tags(event, config))
            for start in start_list.starts:
                person = start.person
                member = member_index.get(start.person_id) if start.person_id is not None else None
                known = member or (contact_index.get(start.person_id) if start.person_id else None)
                if person is None and known is None:
                    continue
                stats["starts"] += 1
                email = person.email if person is not None else None
                if email is None and known is not None:
                    email = known.email
                club = org.name if start.organisation_id == org.id else None
                observations.append(
                    Observation(
                        person_id=start.person_id,
                        given_name=(person.given_name if person else known.given_name),  # type: ignore[union-attr]
                        family_name=(person.family_name if person else known.family_name),  # type: ignore[union-attr]
                        email=email,
                        source=f"start:{event.id}",
                        tags=tags,
                        birth_date=person.birth_date if person else None,
                        club=club,
                        is_member=member is not None,
                        mobile=(person.mobile if person else None)
                        or (known.mobile if known else None),
                    )
                )

    contacts, no_email, merge_stats = merge_observations(
        observations, phone_country_code=config.phone_country_code
    )
    stats.update(merge_stats)
    stats["contacts"] = len(contacts)
    return PullResult(
        organisation=org,
        membership_years=years,
        member_source=member_source,
        window_start=window_start,
        window_end=window_end,
        events=events,
        contacts=contacts,
        no_email=no_email,
        managed_tags=managed_tags(config, years),
        stats=stats,
    )
