"""Plan computation: idempotent upserts, managed tags and the never-resubscribe rule."""

from __future__ import annotations

from eventor_mailchimp_sync.config import load_config
from eventor_mailchimp_sync.diff import MergeTargets, compute_plan, resolve_merge_targets
from eventor_mailchimp_sync.mailchimp import AudienceMember, MergeField
from eventor_mailchimp_sync.sources import DesiredContact

MANAGED = frozenset({"member", "member-2026", "entrant", "series-sprint"})
TARGETS = MergeTargets(
    club="CLUB",
    membership_year="MEMBERYEAR",
    eventor_id="EVENTORID",
    mobile="PHONE",
    types={"MEMBERYEAR": "number"},
)


def desired(email="a@x.com", **kwargs) -> DesiredContact:
    defaults = {
        "first_name": "Alex",
        "last_name": "Example",
        "tags": frozenset({"member", "member-2026"}),
        "membership_years": frozenset({2026}),
        "club": "Example Orienteers",
        "person_ids": frozenset({1001}),
        "sources": ("membership:2026",),
    }
    return DesiredContact(email=email, **{**defaults, **kwargs})


def existing(email="a@x.com", status="subscribed", tags=(), **merge) -> AudienceMember:
    return AudienceMember(
        email=email, status=status, merge_fields=dict(merge), tags=frozenset(tags)
    )


def plan_for(desired_list, existing_list, targets=TARGETS, **kwargs):
    return compute_plan(
        desired_list, existing_list, managed_tags=MANAGED, targets=targets, **kwargs
    )


def test_new_contact_is_created_with_merge_fields_and_tags():
    plan = plan_for([desired(mobile="+61400000001")], [])
    (change,) = plan.changes
    assert change.action == "create"
    assert change.merge_payload == {
        "FNAME": "Alex",
        "LNAME": "Example",
        "CLUB": "Example Orienteers",
        "MEMBERYEAR": 2026,
        "EVENTORID": "1001",
        "PHONE": "+61400000001",
    }
    assert change.tags_add == {"member", "member-2026"}
    assert plan.summary()["new_contacts"] == 1


def test_optional_merge_fields_only_sent_when_present():
    cfg = load_config(
        {"EVENTOR_API_KEY": "k", "MAILCHIMP_API_KEY": "k-us1", "MAILCHIMP_LIST_ID": "L"}
    )
    targets = resolve_merge_targets(
        [MergeField("FNAME", "First", "text"), MergeField("LNAME", "Last", "text")], cfg
    )
    assert targets.club is None and targets.membership_year is None and targets.eventor_id is None
    plan = plan_for([desired()], [], targets=targets)
    assert plan.changes[0].merge_payload == {"FNAME": "Alex", "LNAME": "Example"}


def test_unchanged_contact_produces_no_writes():
    member = existing(
        tags=("member", "member-2026", "Volunteer"),
        FNAME="Alex",
        LNAME="Example",
        CLUB="Example Orienteers",
        MEMBERYEAR=2026,
        EVENTORID="1001",
        PHONE="+61 400 000 001",  # same digits, different formatting: no change
    )
    plan = plan_for([desired(mobile="+61400000001")], [member])
    (change,) = plan.changes
    assert change.action == "unchanged"
    assert not change.has_writes
    assert plan.summary()["unchanged_contacts"] == 1


def test_tags_added_and_only_managed_tags_removed():
    member = existing(
        tags=("member-2025", "member", "Volunteer", "Entrant"),
        FNAME="Alex",
        LNAME="Example",
        CLUB="Example Orienteers",
        MEMBERYEAR=2026,
        EVENTORID="1001",
    )
    plan = plan_for([desired(tags=frozenset({"member", "member-2026"}))], [member])
    (change,) = plan.changes
    assert change.action == "update"
    assert change.tags_add == {"member-2026"}
    # "Entrant" matches the managed "entrant" tag case-insensitively and is no longer desired;
    # "Volunteer" is not managed and "member-2025" is not a synced year, so both stay.
    assert change.tags_remove == {"Entrant"}
    assert change.merge_changes == {}


def test_case_insensitive_tag_matching_avoids_duplicates():
    member = existing(
        tags=("Member", "MEMBER-2026"),
        FNAME="Alex",
        LNAME="Example",
        CLUB="Example Orienteers",
        MEMBERYEAR=2026,
        EVENTORID="1001",
    )
    plan = plan_for([desired()], [member])
    assert plan.changes[0].action == "unchanged"


def test_merge_field_changes():
    member = existing(
        tags=("member", "member-2026"),
        FNAME="alex",
        LNAME="",
        CLUB="",
        MEMBERYEAR=2025,
        EVENTORID="",
    )
    plan = plan_for([desired()], [member])
    (change,) = plan.changes
    assert change.action == "update"
    assert change.merge_changes == {
        "FNAME": ("alex", "Alex"),
        "LNAME": ("", "Example"),
        "CLUB": ("", "Example Orienteers"),
        "MEMBERYEAR": (2025, 2026),
        "EVENTORID": ("", "1001"),
    }


def test_mobile_changes_and_shared_fill_only():
    member = existing(
        tags=("member", "member-2026"),
        FNAME="Alex",
        LNAME="Example",
        CLUB="Example Orienteers",
        MEMBERYEAR=2026,
        EVENTORID="1001",
        PHONE="0400 000 009",
    )
    plan = plan_for([desired(mobile="+61400000001")], [member])
    assert plan.changes[0].merge_changes == {"PHONE": ("0400 000 009", "+61400000001")}
    # Shared address with an existing mobile: leave it alone.
    plan = plan_for([desired(mobile="+61400000001", shared=True)], [member])
    assert plan.changes[0].merge_changes == {}
    # Shared address with no mobile yet: fill it.
    member_blank = existing(
        tags=("member", "member-2026"),
        FNAME="Alex",
        LNAME="Example",
        CLUB="Example Orienteers",
        MEMBERYEAR=2026,
        EVENTORID="1001",
        PHONE="",
    )
    plan = plan_for([desired(mobile="+61400000001", shared=True)], [member_blank])
    assert plan.changes[0].merge_changes == {"PHONE": ("", "+61400000001")}
    # No mobile in Eventor: never blank an existing one.
    plan = plan_for([desired(mobile=None)], [member])
    assert plan.changes[0].merge_changes == {}


def test_us_format_phone_field_is_skipped_with_warning():
    cfg = load_config(
        {"EVENTOR_API_KEY": "k", "MAILCHIMP_API_KEY": "k-us1", "MAILCHIMP_LIST_ID": "L"}
    )
    fields = [
        MergeField("FNAME", "First", "text"),
        MergeField("LNAME", "Last", "text"),
        MergeField("PHONE", "Phone", "phone", phone_format="US"),
    ]
    targets = resolve_merge_targets(fields, cfg)
    assert targets.mobile is None
    assert "US phone format" in targets.warnings[0]
    fields[2] = MergeField("PHONE", "Phone", "phone", phone_format=None)
    targets = resolve_merge_targets(fields, cfg)
    assert targets.mobile == "PHONE"
    assert targets.warnings == ()


def test_names_not_updated_when_disabled():
    member = existing(
        tags=("member", "member-2026"),
        FNAME="Al",
        LNAME="Ex",
        CLUB="Example Orienteers",
        MEMBERYEAR=2026,
        EVENTORID="1001",
    )
    plan = plan_for([desired()], [member], update_names=False)
    assert plan.changes[0].action == "unchanged"


def test_shared_email_only_fills_empty_names():
    member = existing(
        tags=("member", "member-2026"),
        FNAME="Jo",
        LNAME="",
        CLUB="Example Orienteers",
        MEMBERYEAR=2026,
        EVENTORID="1001",
    )
    plan = plan_for([desired(first_name="Sam", last_name="Sample", shared=True)], [member])
    (change,) = plan.changes
    assert change.merge_changes == {"LNAME": ("", "Sample")}


def test_membership_lapse_is_tag_removal_only():
    lapsed = existing(
        email="old@x.com", tags=("member", "member-2026", "Committee"), FNAME="Old", LNAME="Timer"
    )
    plan = plan_for([], [lapsed])
    (change,) = plan.changes
    assert change.action == "update"
    assert change.email == "old@x.com"
    assert change.name == "Old Timer"
    assert change.tags_remove == {"member", "member-2026"}
    assert change.tags_add == frozenset()
    assert change.merge_changes == {}
    assert change.desired is None


def test_contact_with_no_managed_tags_is_left_alone():
    plan = plan_for([], [existing(email="other@x.com", tags=("Newsletter",))])
    assert plan.changes == []
    assert plan.summary()["audience_contacts"] == 1


def test_never_resubscribe_unsubscribed_cleaned_or_archived():
    for status in ("unsubscribed", "cleaned", "archived"):
        member = existing(status=status, tags=("member-2025",), FNAME="Alex", LNAME="Example")
        plan = plan_for([desired()], [member])
        (change,) = plan.changes
        assert change.action == "skip", status
        assert change.reason == status
        assert not change.has_writes
        assert change.would_add_tags == {"member", "member-2026"}
        assert change.merge_changes == {}
        assert change.tags_add == frozenset() and change.tags_remove == frozenset()


def test_protected_contacts_keep_stale_managed_tags():
    bounced = existing(
        email="b@x.com", status="cleaned", tags=("member", "member-2026"), FNAME="B", LNAME="Ounce"
    )
    plan = plan_for([], [bounced])
    (change,) = plan.changes
    assert change.action == "skip"
    assert change.reason == "cleaned"
    assert change.tags_remove == frozenset()


def test_pending_and_transactional_contacts_are_updated_without_status():
    for status in ("pending", "transactional"):
        member = existing(
            status=status,
            tags=(),
            FNAME="Alex",
            LNAME="Example",
            CLUB="Example Orienteers",
            MEMBERYEAR=2026,
            EVENTORID="1001",
        )
        plan = plan_for([desired()], [member])
        (change,) = plan.changes
        assert change.action == "update"
        assert change.tags_add == {"member", "member-2026"}


def test_changed_email_detected_by_eventor_id():
    old = existing(
        email="old@x.com",
        tags=("member", "member-2026"),
        FNAME="Alex",
        LNAME="Example",
        EVENTORID="1001",
    )
    plan = plan_for([desired(email="new@x.com")], [old])
    assert plan.changed_emails == [
        {
            "person_id": 1001,
            "name": "Alex Example",
            "old_email": "old@x.com",
            "old_status": "subscribed",
            "new_email": "new@x.com",
        }
    ]
    assert plan.possible_changed_emails == []
    actions = {c.email: c.action for c in plan.changes}
    assert actions == {"new@x.com": "create", "old@x.com": "update"}


def test_possible_changed_email_detected_by_name():
    old = existing(
        email="old@x.com", status="cleaned", tags=("member",), FNAME="alex", LNAME="EXAMPLE"
    )
    targets = MergeTargets()  # no EVENTORID field in this audience
    plan = plan_for([desired(email="new@x.com")], [old], targets=targets)
    assert plan.changed_emails == []
    assert plan.possible_changed_emails == [
        {
            "name": "Alex Example",
            "old_email": "old@x.com",
            "old_status": "cleaned",
            "new_email": "new@x.com",
        }
    ]


def test_plan_is_deterministic_and_idempotent():
    members = [
        existing(email="b@x.com", tags=("member",)),
        existing(email="a@x.com", tags=("entrant",)),
    ]
    wanted = [
        desired(email="b@x.com", tags=frozenset({"member"})),
        desired(email="c@x.com", tags=frozenset({"entrant"})),
    ]
    first = plan_for(wanted, members, targets=MergeTargets())
    second = plan_for(list(reversed(wanted)), list(reversed(members)), targets=MergeTargets())
    assert [(c.email, c.action) for c in first.changes] == [
        (c.email, c.action) for c in second.changes
    ]
    # Desired contacts first (sorted), then contacts losing managed tags (sorted).
    assert [c.email for c in first.changes] == ["b@x.com", "c@x.com", "a@x.com"]


def test_numeric_merge_values_compare_cleanly():
    member = existing(
        tags=("member", "member-2026"),
        FNAME="Alex",
        LNAME="Example",
        CLUB="Example Orienteers",
        MEMBERYEAR=2026.0,
        EVENTORID=1001.0,
    )
    plan = plan_for([desired()], [member])
    assert plan.changes[0].action == "unchanged"
