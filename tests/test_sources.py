"""Eventor pull: year/window maths, observation merging and the end-to-end pull."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
import respx

from eventor_client import AU_BASE_URL, EventorClient
from eventor_mailchimp_sync.config import load_config
from eventor_mailchimp_sync.sources import (
    Observation,
    managed_tags,
    membership_years,
    merge_observations,
    months_before,
    normalise_email,
    normalise_mobile,
    pull,
)

XML = {"Content-Type": "application/xml"}
ENV = {
    "EVENTOR_API_KEY": "ev-key",
    "MAILCHIMP_API_KEY": "k-us21",
    "MAILCHIMP_LIST_ID": "L",
}


def obs(**kwargs) -> Observation:
    defaults = {
        "person_id": None,
        "given_name": "A",
        "family_name": "B",
        "email": None,
        "source": "test",
        "tags": frozenset(),
    }
    return Observation(**{**defaults, **kwargs})


def test_membership_years():
    cfg = load_config(ENV)
    assert membership_years(date(2026, 2, 1), cfg) == (2026, 2025)
    assert membership_years(date(2026, 3, 31), cfg) == (2026, 2025)
    assert membership_years(date(2026, 4, 1), cfg) == (2026,)
    cfg = load_config({**ENV, "SYNC_PREVIOUS_YEAR_UNTIL_MONTH": "0"})
    assert membership_years(date(2026, 1, 1), cfg) == (2026,)


def test_months_before_clamps_day():
    assert months_before(datetime(2026, 3, 31), 1) == datetime(2026, 2, 28)
    assert months_before(datetime(2024, 3, 31), 1) == datetime(2024, 2, 29)
    assert months_before(datetime(2026, 1, 15, 10, 30), 12) == datetime(2025, 1, 15, 10, 30)
    assert months_before(datetime(2026, 9, 6), 24) == datetime(2024, 9, 6)


def test_managed_tags():
    cfg = load_config({**ENV})
    assert managed_tags(cfg, (2026, 2025)) == {"member", "member-2026", "member-2025", "entrant"}
    cfg = load_config({**ENV, "SYNC_MEMBER_TAG": "none"})
    assert managed_tags(cfg, (2026,)) == {"member-2026", "entrant"}


def test_normalise_email():
    assert normalise_email("  Alex.Example@Example.COM ") == "alex.example@example.com"
    assert normalise_email("") is None
    assert normalise_email(None) is None
    assert normalise_email("not an email") is None
    assert normalise_email("a@b") is None


def test_normalise_mobile():
    assert normalise_mobile("0412 345 678", "61") == "+61412345678"
    assert normalise_mobile("0412345678", "61") == "+61412345678"
    assert normalise_mobile("+61 412 345 678", "61") == "+61412345678"
    assert normalise_mobile("61412345678", "61") == "+61412345678"
    assert normalise_mobile("(02) 4900-0000", "61") == "+61249000000"
    assert normalise_mobile("0412345678", None) == "0412345678"
    assert normalise_mobile("+44 7700 900123", "61") == "+447700900123"
    assert normalise_mobile("", "61") is None
    assert normalise_mobile("1234", "61") is None
    assert normalise_mobile(None, "61") is None


def test_merge_prefers_primary_persons_mobile():
    contacts, _, _ = merge_observations(
        [
            obs(
                person_id=2,
                given_name="Kid",
                family_name="S",
                email="fam@x.com",
                birth_date=date(2015, 1, 1),
                is_member=True,
                mobile="0400 000 002",
            ),
            obs(
                person_id=1,
                given_name="Parent",
                family_name="S",
                email="fam@x.com",
                birth_date=date(1980, 1, 1),
                is_member=True,
                mobile=None,
            ),
            obs(
                person_id=1,
                given_name="Parent",
                family_name="S",
                email="fam@x.com",
                source="entry:1",
                mobile="0400 000 001",
            ),
        ],
        phone_country_code="61",
    )
    # Primary (oldest member) has no mobile on the membership record; the first other
    # observation with one wins.
    assert contacts[0].first_name == "Parent"
    assert contacts[0].mobile == "+61400000002"


def test_merge_dedupes_on_normalised_email():
    contacts, missing, stats = merge_observations(
        [
            obs(
                person_id=1,
                given_name="Alex",
                family_name="Example",
                email="A@X.COM",
                source="membership:2026",
                tags=frozenset({"member"}),
                membership_years=frozenset({2026}),
                is_member=True,
            ),
            obs(
                person_id=1,
                given_name="Alex",
                family_name="Example",
                email=" a@x.com",
                source="entry:5",
                tags=frozenset({"entrant"}),
            ),
        ]
    )
    assert len(contacts) == 1
    c = contacts[0]
    assert c.email == "a@x.com"
    assert c.tags == {"member", "entrant"}
    assert c.person_ids == {1}
    assert c.sources == ("membership:2026", "entry:5")
    assert c.membership_years == {2026}
    assert not c.shared
    assert missing == []
    assert stats["shared_emails"] == 0


def test_merge_shared_email_prefers_member_then_oldest():
    contacts, _, stats = merge_observations(
        [
            obs(
                person_id=3,
                given_name="Kid",
                family_name="Sample",
                email="fam@x.com",
                birth_date=date(2015, 1, 1),
                is_member=True,
                source="membership:2026",
            ),
            obs(
                person_id=2,
                given_name="Parent",
                family_name="Sample",
                email="fam@x.com",
                birth_date=date(1980, 1, 1),
                is_member=True,
                source="membership:2026",
            ),
            obs(
                person_id=9,
                given_name="Guest",
                family_name="Person",
                email="fam@x.com",
                birth_date=date(1950, 1, 1),
                source="entry:1",
            ),
        ]
    )
    assert contacts[0].shared
    assert contacts[0].first_name == "Parent"
    assert contacts[0].person_ids == {2, 3, 9}
    assert stats["shared_emails"] == 1


def test_merge_reports_people_without_email():
    contacts, missing, stats = merge_observations(
        [
            obs(
                person_id=5,
                given_name="Kim",
                family_name="Noemail",
                email=None,
                source="membership:2026",
                is_member=True,
            ),
            obs(person_id=5, given_name="Kim", family_name="Noemail", email=None, source="entry:1"),
            obs(
                person_id=6, given_name="Bad", family_name="Address", email="nope", source="entry:1"
            ),
            obs(person_id=7, given_name="Fine", family_name="Later", email=None, source="entry:1"),
            obs(
                person_id=7,
                given_name="Fine",
                family_name="Later",
                email="fine@x.com",
                source="entry:2",
            ),
        ]
    )
    assert [c.email for c in contacts] == ["fine@x.com"]
    assert [(m.name, m.is_member, m.sources) for m in missing] == [
        ("Kim Noemail", True, ("membership:2026", "entry:1")),
        ("Bad Address", False, ("entry:1",)),
    ]
    assert stats["invalid_emails"] == 1
    assert stats["no_email"] == 2


@pytest.fixture
def eventor_routes(fixture_bytes):
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{AU_BASE_URL}/organisation/apiKey").mock(
            return_value=httpx.Response(
                200, content=fixture_bytes("organisation_apikey.xml"), headers=XML
            )
        )
        mock.get(f"{AU_BASE_URL}/memberships").mock(
            return_value=httpx.Response(200, content=fixture_bytes("memberships.xml"), headers=XML)
        )
        mock.get(f"{AU_BASE_URL}/persons/organisations/123").mock(
            return_value=httpx.Response(
                200, content=fixture_bytes("persons_organisations.xml"), headers=XML
            )
        )
        mock.get(f"{AU_BASE_URL}/events").mock(
            return_value=httpx.Response(200, content=fixture_bytes("events.xml"), headers=XML)
        )
        mock.get(f"{AU_BASE_URL}/entries").mock(
            return_value=httpx.Response(200, content=fixture_bytes("entries.xml"), headers=XML)
        )
        mock.get(f"{AU_BASE_URL}/starts/event").mock(
            return_value=httpx.Response(200, content=fixture_bytes("starts_event.xml"), headers=XML)
        )
        yield mock


def test_pull_end_to_end(eventor_routes, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[series.sprint]\ntag = "series-sprint"\nname_patterns = ["sprint series"]\n'
    )
    cfg = load_config(ENV, config_path=config_path)
    now = datetime(2026, 9, 6, 12, 0, 0)
    with EventorClient(AU_BASE_URL, "ev-key") as client:
        result = pull(client, cfg, now)

    assert result.organisation.id == 123
    assert result.membership_years == (2026,)
    assert result.member_source == "memberships"
    assert result.window_start == datetime(2025, 9, 6, 12, 0, 0)
    assert result.window_end == datetime(2026, 9, 6, 23, 59, 59)
    assert [e.id for e in result.events] == [5001, 5002]  # cancelled 5003 dropped
    assert result.managed_tags == {"member", "member-2026", "entrant", "series-sprint"}

    events_call = eventor_routes.get(f"{AU_BASE_URL}/events").calls.last.request.url.params
    assert events_call["organisationIds"] == "123"
    assert events_call["fromDate"] == "2025-09-06 12:00:00"
    entries_call = eventor_routes.get(f"{AU_BASE_URL}/entries").calls.last.request.url.params
    assert entries_call["eventIds"] == "5001,5002"
    assert entries_call["includePersonElement"] == "true"
    assert entries_call["includeOrganisationElement"] == "false"
    assert not eventor_routes.get(f"{AU_BASE_URL}/starts/event").called

    by_email = {c.email: c for c in result.contacts}
    assert set(by_email) == {
        "alex.example@example.com",
        "sample.family@example.com",
        "gus.guest@example.com",
    }

    alex = by_email["alex.example@example.com"]
    assert alex.first_name == "Alex" and alex.last_name == "Example"
    assert alex.mobile == "+61400000001"
    assert alex.tags == {"member", "member-2026", "entrant", "series-sprint"}
    assert alex.membership_years == {2026}
    assert alex.club == "Example Orienteers"
    assert alex.person_ids == {1001}
    assert alex.sources == ("membership:2026", "entry:5001")

    family = by_email["sample.family@example.com"]
    assert family.shared
    assert family.person_ids == {1002, 1003}
    assert family.first_name == "Sam"  # oldest member, from the membership record
    assert family.tags == {"member", "member-2026", "entrant", "series-sprint"}

    gus = by_email["gus.guest@example.com"]
    assert gus.tags == {"entrant"}  # Bush Classic is not in the sprint series
    assert gus.mobile == "+61400000999"
    assert gus.membership_years == frozenset()
    assert gus.club is None

    assert [(p.name, p.is_member, p.sources) for p in result.no_email] == [
        ("Kim Noemail", True, ("membership:2026", "entry:5001")),
        ("Pat Unpaid", False, ("entry:5002",)),
        ("Vic Visitor", False, ("entry:5002",)),
    ]
    stats = result.stats
    assert stats["memberships"] == 4
    assert stats["memberships_unpaid_skipped"] == 1
    assert stats["events"] == 2
    assert stats["events_cancelled_skipped"] == 1
    assert stats["entries"] == 6
    assert stats["entries_team_skipped"] == 1
    assert stats["entrants_other_org"] == 2
    assert stats["entrants_other_org_with_email"] == 1
    # Kim is a member without an email anywhere, and the other member entrants carry
    # their email on the entry itself in this fixture, so nothing is recovered here.
    assert stats["entrant_email_from_member_index"] == 0
    assert "persons_contact_index" not in stats
    assert stats["shared_emails"] == 1
    assert not eventor_routes.get(f"{AU_BASE_URL}/persons/organisations/123").called


def test_persons_contact_index_recovers_club_attached_entrants(eventor_routes):
    cfg = load_config({**ENV, "SYNC_PERSONS_CONTACT_INDEX": "true"})
    with EventorClient(AU_BASE_URL, "ev-key") as client:
        result = pull(client, cfg, datetime(2026, 9, 6))
    by_email = {c.email: c for c in result.contacts}
    pat = by_email["pat.unpaid@example.com"]
    assert pat.tags == {"entrant"}  # attached to the club, but not a paid member
    assert pat.membership_years == frozenset()
    assert pat.sources == ("entry:5002",)
    assert pat.club == "Example Orienteers"
    assert {p.name for p in result.no_email} == {"Kim Noemail", "Vic Visitor"}
    assert result.stats["persons_contact_index"] == 1  # only Pat: others are members or lack email
    assert result.stats["entrant_email_from_persons"] == 1


def test_pull_with_persons_source_and_starts(eventor_routes):
    cfg = load_config({**ENV, "SYNC_MEMBER_SOURCE": "persons", "SYNC_INCLUDE_STARTS": "true"})
    with EventorClient(AU_BASE_URL, "ev-key") as client:
        result = pull(client, cfg, datetime(2026, 2, 1))
    assert result.member_source == "persons"
    assert result.membership_years == (2026, 2025)
    assert not eventor_routes.get(f"{AU_BASE_URL}/memberships").called
    assert eventor_routes.get(f"{AU_BASE_URL}/starts/event").call_count == 2
    by_email = {c.email: c for c in result.contacts}
    assert "start:5001" in by_email["alex.example@example.com"].sources
    # Unpaid Pat is not in the persons fixture; Kim has no email in either.
    assert {p.name for p in result.no_email} >= {"Kim Noemail", "Vic Visitor"}
    # Per event: Alex (Person + ID) and Wendy (Person, no ID) count; the bare unknown
    # PersonId 3001 has no name or email anywhere and is skipped. Two events -> 4.
    assert result.stats["starts"] == 4
    assert "Wendy Walkup" in {p.name for p in result.no_email}


def test_pull_falls_back_to_persons_when_memberships_missing(eventor_routes, fixture_bytes):
    eventor_routes.get(f"{AU_BASE_URL}/memberships").mock(
        return_value=httpx.Response(404, text="Not found")
    )
    cfg = load_config(ENV)
    with EventorClient(AU_BASE_URL, "ev-key", max_retries=0) as client:
        result = pull(client, cfg, datetime(2026, 9, 6))
    assert result.member_source == "persons"
    assert eventor_routes.get(f"{AU_BASE_URL}/persons/organisations/123").called
