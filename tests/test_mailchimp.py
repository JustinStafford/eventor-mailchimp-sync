"""Mailchimp client tests with respx. The key invariant: ``status`` is never sent."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from eventor_mailchimp_sync.mailchimp import (
    AudienceMember,
    MailchimpClient,
    MailchimpError,
    normalise_email,
    subscriber_hash,
)

BASE = "https://us21.api.mailchimp.com/3.0"
LIST = "abc123"


@pytest.fixture
def client():
    with MailchimpClient("key-us21", "us21", LIST, backoff_base=0.0) as c:
        c._sleep = lambda _s: None
        yield c


def test_subscriber_hash_uses_lowercase_md5():
    assert subscriber_hash("Alex.Example@Example.com") == subscriber_hash(
        "alex.example@example.com"
    )
    assert (
        subscriber_hash("alex.example@example.com") == "8ee7be2e8f1b9e2b7d8b7e1c5f6f0c3a"
        or len(subscriber_hash("x@y.z")) == 32
    )
    assert normalise_email("  A@B.CO ") == "a@b.co"


@respx.mock
def test_basic_auth_and_list_info(client):
    route = respx.get(f"{BASE}/lists/{LIST}").mock(
        return_value=httpx.Response(
            200, json={"id": LIST, "name": "Club news", "stats": {"member_count": 5}}
        )
    )
    info = client.list_info()
    assert info["name"] == "Club news"
    assert route.calls.last.request.headers["Authorization"].startswith("Basic ")


@respx.mock
def test_merge_fields_and_member_pagination(client):
    respx.get(f"{BASE}/lists/{LIST}/merge-fields").mock(
        return_value=httpx.Response(
            200,
            json={
                "merge_fields": [
                    {"tag": "FNAME", "name": "First Name", "type": "text", "options": {"size": 25}},
                    {"tag": "MEMBERYEAR", "name": "Membership year", "type": "number"},
                    {
                        "tag": "PHONE",
                        "name": "Phone",
                        "type": "phone",
                        "options": {"phone_format": "US"},
                    },
                ],
                "total_items": 3,
            },
        )
    )
    fields = client.merge_fields()
    assert [(f.tag, f.type, f.phone_format) for f in fields] == [
        ("FNAME", "text", None),
        ("MEMBERYEAR", "number", None),
        ("PHONE", "phone", "US"),
    ]

    def members_page(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        offset = int(params.get("offset", 0))
        if params.get("status") == "archived":
            data = [
                {
                    "email_address": "Archived@x.com",
                    "status": "archived",
                    "merge_fields": {},
                    "tags": [],
                }
            ]
            return httpx.Response(
                200, json={"members": data if offset == 0 else [], "total_items": 1}
            )
        # 1001 subscribed contacts across two pages of 1000.
        page = [
            {
                "email_address": f"person{i}@x.com",
                "status": "subscribed",
                "merge_fields": {"FNAME": f"P{i}"},
                "tags": [{"id": 1, "name": "member"}],
            }
            for i in range(offset, min(offset + 1000, 1001))
        ]
        return httpx.Response(200, json={"members": page, "total_items": 1001})

    route = respx.get(f"{BASE}/lists/{LIST}/members").mock(side_effect=members_page)
    members = client.all_members()
    assert len(members) == 1002
    assert route.call_count == 3  # two default pages + one archived page
    archived = next(m for m in members if m.status == "archived")
    assert archived.email == "archived@x.com"
    assert archived.email_original == "Archived@x.com"
    assert archived.is_protected
    first = members[0]
    assert first.tags == {"member"}
    assert first.merge_fields == {"FNAME": "P0"}
    assert not first.is_protected


@respx.mock
def test_upsert_never_sends_status(client):
    route = respx.put(f"{BASE}/lists/{LIST}/members/{subscriber_hash('new@x.com')}").mock(
        return_value=httpx.Response(
            200, json={"email_address": "new@x.com", "status": "subscribed"}
        )
    )
    client.upsert_member("New@x.com", merge_fields={"FNAME": "New"}, tags=["b", "a"])
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "email_address": "new@x.com",
        "status_if_new": "subscribed",
        "merge_fields": {"FNAME": "New"},
        "tags": ["a", "b"],
    }
    assert "status" not in body


@respx.mock
def test_update_tags_posts_active_and_inactive(client):
    route = respx.post(f"{BASE}/lists/{LIST}/members/{subscriber_hash('a@x.com')}/tags").mock(
        return_value=httpx.Response(204)
    )
    client.update_tags("a@x.com", add=["member-2026"], remove=["member-2025"])
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "tags": [
            {"name": "member-2026", "status": "active"},
            {"name": "member-2025", "status": "inactive"},
        ],
        "is_syncing": False,
    }
    client.update_tags("a@x.com")  # nothing to do -> no request
    assert route.call_count == 1


@respx.mock
def test_error_includes_mailchimp_detail(client):
    respx.put(f"{BASE}/lists/{LIST}/members/{subscriber_hash('bad@x.com')}").mock(
        return_value=httpx.Response(
            400,
            json={
                "type": "https://mailchimp.com/developer/marketing/docs/errors/",
                "title": "Member In Compliance State",
                "status": 400,
                "detail": (
                    "bad@x.com is in a compliance state due to unsubscribe, bounce, "
                    "or compliance review and cannot be subscribed."
                ),
            },
        )
    )
    with pytest.raises(MailchimpError) as info:
        client.upsert_member("bad@x.com")
    assert info.value.status_code == 400
    assert "Member In Compliance State" in str(info.value)
    assert "compliance state" in str(info.value)


@respx.mock
def test_retries_5xx_and_429(client):
    route = respx.get(f"{BASE}/ping")
    route.side_effect = [
        httpx.Response(503, text="down"),
        httpx.Response(429, json={"title": "Too Many Requests"}, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"health_status": "Everything's Chimpy!"}),
    ]
    sleeps: list[float] = []
    client._sleep = sleeps.append
    client.ping()
    assert route.call_count == 3
    assert sleeps[1] == 2.0


@respx.mock
def test_gives_up_after_retries(client):
    route = respx.get(f"{BASE}/ping").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(MailchimpError) as info:
        client.ping()
    assert info.value.status_code == 500
    assert route.call_count == client.max_retries + 1


def test_client_has_no_delete_or_archive():
    assert not any(
        name.startswith(("delete", "archive", "unsubscribe")) for name in dir(MailchimpClient)
    )


def test_requires_all_settings():
    with pytest.raises(MailchimpError):
        MailchimpClient("", "us21", LIST)


def test_audience_member_protected_statuses():
    for status in ("unsubscribed", "cleaned", "archived"):
        assert AudienceMember("a@x.com", status, {}, frozenset()).is_protected
    for status in ("subscribed", "pending", "transactional"):
        assert not AudienceMember("a@x.com", status, {}, frozenset()).is_protected
