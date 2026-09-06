"""End-to-end runs through the CLI with both APIs mocked. Nothing touches the network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from eventor_client import AU_BASE_URL
from eventor_mailchimp_sync.cli import app
from eventor_mailchimp_sync.mailchimp import subscriber_hash

XML = {"Content-Type": "application/xml"}
MC = "https://us21.api.mailchimp.com/3.0"
LIST = "list1"
ENV = {
    "EVENTOR_API_KEY": "ev-key",
    "MAILCHIMP_API_KEY": "test-key-us21",
    "MAILCHIMP_LIST_ID": LIST,
}

AUDIENCE = [
    # Existing member with an old-year tag and a hand-made tag: gains member-2026 + entrant.
    {
        "email_address": "sample.family@example.com",
        "status": "subscribed",
        "merge_fields": {"FNAME": "Jo", "LNAME": "Sample", "CLUB": "", "MEMBERYEAR": 2025},
        "tags": [{"id": 1, "name": "member-2025"}, {"id": 2, "name": "Volunteer"}],
    },
    # Lapsed member: loses member + member-2026, keeps Committee.
    {
        "email_address": "old.member@example.com",
        "status": "subscribed",
        "merge_fields": {"FNAME": "Old", "LNAME": "Timer"},
        "tags": [
            {"id": 3, "name": "member"},
            {"id": 4, "name": "member-2026"},
            {"id": 5, "name": "Committee"},
        ],
    },
    # Unsubscribed entrant: must never be touched.
    {
        "email_address": "gus.guest@example.com",
        "status": "unsubscribed",
        "merge_fields": {"FNAME": "Gus", "LNAME": "Guest"},
        "tags": [],
    },
    # Bounced former member: skipped, stale tags stay.
    {
        "email_address": "bounced@example.com",
        "status": "cleaned",
        "merge_fields": {"FNAME": "Bo", "LNAME": "Unced"},
        "tags": [{"id": 3, "name": "member"}],
    },
    # Same name as Alex under an old address: flagged as a possible changed email.
    {
        "email_address": "alex.old@example.com",
        "status": "subscribed",
        "merge_fields": {"FNAME": "Alex", "LNAME": "Example"},
        "tags": [{"id": 4, "name": "member-2026"}],
    },
]
ARCHIVED = [
    {
        "email_address": "archived@example.com",
        "status": "archived",
        "merge_fields": {"FNAME": "Ar", "LNAME": "Chived"},
        "tags": [{"id": 6, "name": "entrant"}],
    }
]


@pytest.fixture
def routes(fixture_bytes):
    with respx.mock(assert_all_called=False) as mock:
        for path, name in (
            ("/organisation/apiKey", "organisation_apikey.xml"),
            ("/memberships", "memberships.xml"),
            ("/events", "events.xml"),
            ("/entries", "entries.xml"),
        ):
            mock.get(f"{AU_BASE_URL}{path}").mock(
                return_value=httpx.Response(200, content=fixture_bytes(name), headers=XML)
            )
        mock.get(f"{MC}/lists/{LIST}").mock(
            return_value=httpx.Response(200, json={"id": LIST, "name": "Club news", "stats": {}})
        )
        mock.get(f"{MC}/lists/{LIST}/merge-fields").mock(
            return_value=httpx.Response(
                200,
                json={
                    "merge_fields": [
                        {"tag": "FNAME", "name": "First", "type": "text"},
                        {"tag": "LNAME", "name": "Last", "type": "text"},
                        {"tag": "CLUB", "name": "Club", "type": "text"},
                        {"tag": "MEMBERYEAR", "name": "Membership year", "type": "number"},
                        {"tag": "PHONE", "name": "Phone", "type": "phone", "options": {}},
                    ],
                    "total_items": 5,
                },
            )
        )

        def members(request: httpx.Request) -> httpx.Response:
            archived = request.url.params.get("status") == "archived"
            data = ARCHIVED if archived else AUDIENCE
            return httpx.Response(200, json={"members": data, "total_items": len(data)})

        mock.get(f"{MC}/lists/{LIST}/members").mock(side_effect=members)
        mock.put(url__regex=rf"{MC}/lists/{LIST}/members/[0-9a-f]{{32}}$", name="put_member").mock(
            return_value=httpx.Response(200, json={"status": "subscribed"})
        )
        mock.post(url__regex=rf"{MC}/lists/{LIST}/members/[0-9a-f]{{32}}/tags$").mock(
            return_value=httpx.Response(204)
        )
        yield mock


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.chdir(tmp_path)
    # Skip the polite inter-request delay in tests.
    monkeypatch.setattr("eventor_client.client.time.sleep", lambda _s: None)
    for key in [*ENV, "SYNC_CONFIG", "SYNC_REPORT_PATH", "EVENTOR_BASE_URL"]:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "config.toml").write_text(
        '[series.sprint]\ntag = "series-sprint"\nname_patterns = ["sprint series"]\n'
    )
    return CliRunner()


def writes(routes) -> list[httpx.Request]:
    return [
        call.request
        for route in routes.routes
        for call in route.calls
        if call.request.method in ("PUT", "POST", "PATCH", "DELETE")
    ]


def test_dry_run_is_default_and_writes_nothing(routes, runner: CliRunner, tmp_path: Path):
    result = runner.invoke(app, ["sync", "--as-of", "2026-09-06"], env=ENV)
    assert result.exit_code == 0, result.output
    assert writes(routes) == []
    out = result.output
    assert "DRY RUN" in out
    assert "+ alex.example@example.com" in out
    assert "series-sprint" in out
    assert "~ sample.family@example.com" in out and "+member-2026" in out and "+entrant" in out
    assert "~ old.member@example.com" in out and "-member-2026" in out
    assert "- gus.guest@example.com" in out and "unsubscribed" in out
    assert "- bounced@example.com" in out and "cleaned" in out
    assert "- archived@example.com" in out and "archived" in out
    assert "Kim Noemail" in out
    assert "Vic Visitor" not in out  # non-members are summarised, not listed, in the diff
    assert "2 non-member entrants" in out
    assert "Possible changed email" in out and "alex.old@example.com" in out
    assert "Report written to report.json" in out

    report = json.loads((tmp_path / "report.json").read_text())
    assert report["ok"] is True
    assert report["mode"] == "dry-run"
    assert report["eventor"]["organisation_id"] == 123
    assert report["eventor"]["membership_years"] == [2026]
    assert [e["id"] for e in report["eventor"]["events"]] == [5001, 5002]
    assert report["eventor"]["events"][0]["series_tags"] == ["series-sprint"]
    assert report["mailchimp"]["audience_name"] == "Club news"
    assert report["mailchimp"]["merge_fields"]["EVENTORID"] is None
    assert report["mailchimp"]["merge_fields"]["MEMBERYEAR"] == "MEMBERYEAR"
    assert report["mailchimp"]["merge_fields"]["PHONE"] == "PHONE"
    assert report["mailchimp"]["warnings"] == []
    assert sorted(report["mailchimp"]["managed_tags"]) == [
        "entrant",
        "member",
        "member-2026",
        "series-sprint",
    ]
    summary = report["summary"]
    assert summary["new_contacts"] == 1
    assert summary["updated_contacts"] == 3  # sample.family, old.member, alex.old
    assert summary["skipped_contacts"] == 3  # gus (unsubscribed), bounced (cleaned), archived
    by_email = {c["email"]: c for c in report["changes"]}
    assert by_email["alex.example@example.com"]["action"] == "create"
    assert by_email["alex.example@example.com"]["merge_fields"]["MEMBERYEAR"]["new"] == 2026
    assert by_email["alex.example@example.com"]["applied"] is False
    assert by_email["sample.family@example.com"]["shared_email"] is True
    assert by_email["sample.family@example.com"]["merge_fields"] == {
        "CLUB": {"old": "", "new": "Example Orienteers"},
        "MEMBERYEAR": {"old": 2025, "new": 2026},
    }
    assert sorted(by_email["old.member@example.com"]["tags_remove"]) == ["member", "member-2026"]
    assert by_email["gus.guest@example.com"] == {
        "email": "gus.guest@example.com",
        "name": "Gus Guest",
        "action": "skip",
        "reason": "unsubscribed",
        "would_add_tags": ["entrant"],
        "existing_status": "unsubscribed",
        "sources": ["entry:5002"],
        "person_ids": [2002],
    }
    exceptions = report["exceptions"]
    assert [p["name"] for p in exceptions["no_email"]] == [
        "Kim Noemail",
        "Pat Unpaid",
        "Vic Visitor",
    ]
    assert [c["email"] for c in exceptions["unsubscribed"]] == ["gus.guest@example.com"]
    assert [c["email"] for c in exceptions["cleaned"]] == ["bounced@example.com"]
    assert [c["email"] for c in exceptions["archived"]] == ["archived@example.com"]
    assert exceptions["shared_email"][0]["email"] == "sample.family@example.com"
    assert exceptions["possible_changed_email"] == [
        {
            "name": "Alex Example",
            "old_email": "alex.old@example.com",
            "old_status": "subscribed",
            "new_email": "alex.example@example.com",
        }
    ]
    assert exceptions["api_errors"] == []


def test_apply_writes_only_planned_changes_and_never_status(
    routes, runner: CliRunner, tmp_path: Path
):
    result = runner.invoke(app, ["sync", "--apply", "--as-of", "2026-09-06", "--quiet"], env=ENV)
    assert result.exit_code == 0, result.output
    requests = writes(routes)
    by_key = {(r.method, r.url.path): json.loads(r.content) for r in requests}
    h = subscriber_hash
    assert set(by_key) == {
        ("PUT", f"/3.0/lists/{LIST}/members/{h('alex.example@example.com')}"),
        ("PUT", f"/3.0/lists/{LIST}/members/{h('sample.family@example.com')}"),
        ("POST", f"/3.0/lists/{LIST}/members/{h('sample.family@example.com')}/tags"),
        ("POST", f"/3.0/lists/{LIST}/members/{h('old.member@example.com')}/tags"),
        ("POST", f"/3.0/lists/{LIST}/members/{h('alex.old@example.com')}/tags"),
    }
    for body in by_key.values():
        assert "status" not in body, body
    create = by_key[("PUT", f"/3.0/lists/{LIST}/members/{h('alex.example@example.com')}")]
    assert create["status_if_new"] == "subscribed"
    assert create["merge_fields"] == {
        "FNAME": "Alex",
        "LNAME": "Example",
        "CLUB": "Example Orienteers",
        "MEMBERYEAR": 2026,
        "PHONE": "+61400000001",
    }
    assert create["tags"] == ["entrant", "member", "member-2026", "series-sprint"]
    family_put = by_key[("PUT", f"/3.0/lists/{LIST}/members/{h('sample.family@example.com')}")]
    assert family_put["merge_fields"] == {"CLUB": "Example Orienteers", "MEMBERYEAR": 2026}
    assert "tags" not in family_put
    lapse = by_key[("POST", f"/3.0/lists/{LIST}/members/{h('old.member@example.com')}/tags")]
    assert lapse["tags"] == [
        {"name": "member", "status": "inactive"},
        {"name": "member-2026", "status": "inactive"},
    ]
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["mode"] == "apply"
    assert report["ok"] is True
    assert all(c["applied"] for c in report["changes"] if c["action"] in ("create", "update"))


def test_apply_reports_write_failures_and_exits_nonzero(routes, runner: CliRunner, tmp_path: Path):
    bad = subscriber_hash("alex.example@example.com")

    def put_member(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(bad):
            return httpx.Response(400, json={"title": "Invalid Resource", "detail": "looks fake"})
        return httpx.Response(200, json={"status": "subscribed"})

    routes["put_member"].side_effect = put_member
    result = runner.invoke(app, ["sync", "--apply", "--as-of", "2026-09-06", "--quiet"], env=ENV)
    assert result.exit_code == 3, result.output
    assert "1 change(s) failed to apply" in result.output
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["ok"] is False
    assert report["exceptions"]["api_errors"][0]["email"] == "alex.example@example.com"
    assert "Invalid Resource" in report["exceptions"]["api_errors"][0]["error"]
    # The other writes still happened.
    assert len(writes(routes)) == 5


def test_eventor_failure_exits_2(routes, runner: CliRunner):
    routes.get(f"{AU_BASE_URL}/organisation/apiKey").mock(
        return_value=httpx.Response(403, text="denied")
    )
    result = runner.invoke(app, ["sync"], env=ENV)
    assert result.exit_code == 2
    assert "Eventor API failure" in result.output
    assert writes(routes) == []


def test_mailchimp_read_failure_exits_3(routes, runner: CliRunner):
    routes.get(f"{MC}/lists/{LIST}/members").mock(
        return_value=httpx.Response(401, json={"title": "API Key Invalid"})
    )
    result = runner.invoke(app, ["sync", "--as-of", "2026-09-06"], env=ENV)
    assert result.exit_code == 3
    assert "Mailchimp API failure" in result.output


def test_config_error_exits_1(routes, runner: CliRunner):
    result = runner.invoke(app, ["sync"], env={"EVENTOR_API_KEY": "k"})
    assert result.exit_code == 1
    assert "MAILCHIMP_API_KEY" in result.output
    result = runner.invoke(app, ["sync", "--apply", "--dry-run"], env=ENV)
    assert result.exit_code == 1


def test_redact_masks_personal_data(routes, runner: CliRunner, tmp_path: Path):
    result = runner.invoke(
        app, ["sync", "--as-of", "2026-09-06", "--redact", "--report", "out/r.json"], env=ENV
    )
    assert result.exit_code == 0, result.output
    assert "alex.example@example.com" not in result.output
    assert "a***@example.com" in result.output
    text = (tmp_path / "out" / "r.json").read_text()
    assert "alex.example@example.com" not in text
    assert "Noemail" not in text
    assert json.loads(text)["redacted"] is True


def test_whoami_and_audience(routes, runner: CliRunner):
    result = runner.invoke(app, ["whoami"], env={"EVENTOR_API_KEY": "k"})
    assert result.exit_code == 0, result.output
    assert "Example Orienteers (organisation #123)" in result.output
    assert "/memberships" in result.output
    result = runner.invoke(app, ["audience"], env=ENV)
    assert result.exit_code == 0, result.output
    assert "Club news" in result.output
    assert "EVENTORID" in result.output and "not in audience" in result.output
    assert "MEMBERYEAR" in result.output and "will be synced" in result.output
    assert "PHONE" in result.output and "+61" in result.output


def test_env_file_is_loaded(routes, runner: CliRunner, tmp_path: Path):
    (tmp_path / ".env").write_text("EVENTOR_API_KEY=from-dotenv\n")
    result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 0, result.output
    request = routes.get(f"{AU_BASE_URL}/organisation/apiKey").calls.last.request
    assert request.headers["ApiKey"] == "from-dotenv"
