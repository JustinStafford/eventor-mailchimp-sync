"""Compute the idempotent change set between desired contacts and the audience.

The plan is pure data so it can be printed as a dry run, serialised into the
JSON report, and executed. Rules:

* Contacts that are unsubscribed, cleaned (bounced) or archived are never
  written to, whatever Eventor says. They are reported as skipped.
* Only *managed* tags (``member-YYYY`` for the synced years, the aggregate
  member tag, the entrant tag and configured series tags) are ever removed.
  Tags the club added by hand are untouched.
* Merge fields are only written when the Eventor value is non-empty and
  differs. For a shared email (one address, several people) names are only
  filled in when empty, never overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from eventor_mailchimp_sync.config import SyncConfig
from eventor_mailchimp_sync.mailchimp import AudienceMember, MergeField
from eventor_mailchimp_sync.sources import DesiredContact

Action = Literal["create", "update", "unchanged", "skip"]


@dataclass(frozen=True, slots=True)
class MergeTargets:
    """Merge-field tags that exist in the audience, so optional ones are only sent when present."""

    first_name: str = "FNAME"
    last_name: str = "LNAME"
    club: str | None = None
    membership_year: str | None = None
    eventor_id: str | None = None
    mobile: str | None = None
    types: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def coerce(self, tag: str, value: Any) -> Any:
        if self.types.get(tag) == "number":
            try:
                return int(value)
            except (TypeError, ValueError):
                return value
        return str(value)


def resolve_merge_targets(fields: list[MergeField], config: SyncConfig) -> MergeTargets:
    present = {f.tag.upper(): f for f in fields}
    types = {f.tag.upper(): f.type for f in fields}
    mobile: str | None = None
    warnings: list[str] = []
    if config.merge_mobile and config.merge_mobile in present:
        field_info = present[config.merge_mobile]
        if field_info.type == "phone" and (field_info.phone_format or "").upper() == "US":
            warnings.append(
                f"merge field {config.merge_mobile} is set to US phone format; mobiles are not "
                "synced. Change it to international format in Mailchimp audience settings."
            )
        else:
            mobile = config.merge_mobile
    return MergeTargets(
        first_name="FNAME",
        last_name="LNAME",
        club=config.merge_club if config.merge_club in present else None,
        membership_year=config.merge_membership_year
        if config.merge_membership_year in present
        else None,
        eventor_id=config.merge_eventor_id if config.merge_eventor_id in present else None,
        mobile=mobile,
        types=types,
        warnings=tuple(warnings),
    )


@dataclass(slots=True)
class ContactChange:
    email: str
    action: Action
    name: str = ""
    reason: str | None = None
    merge_changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    tags_add: frozenset[str] = frozenset()
    tags_remove: frozenset[str] = frozenset()
    would_add_tags: frozenset[str] = frozenset()
    desired: DesiredContact | None = None
    existing: AudienceMember | None = None
    applied: bool = False
    error: str | None = None

    @property
    def has_writes(self) -> bool:
        return self.action in ("create", "update")

    @property
    def merge_payload(self) -> dict[str, Any]:
        return {tag: new for tag, (_old, new) in self.merge_changes.items()}


@dataclass(slots=True)
class Plan:
    changes: list[ContactChange]
    managed_tags: frozenset[str]
    changed_emails: list[dict[str, Any]] = field(default_factory=list)
    possible_changed_emails: list[dict[str, Any]] = field(default_factory=list)
    audience_size: int = 0

    def by_action(self, action: Action) -> list[ContactChange]:
        return [c for c in self.changes if c.action == action]

    def summary(self) -> dict[str, int]:
        creates = self.by_action("create")
        updates = self.by_action("update")
        return {
            "audience_contacts": self.audience_size,
            "desired_contacts": sum(1 for c in self.changes if c.desired is not None),
            "new_contacts": len(creates),
            "updated_contacts": len(updates),
            "unchanged_contacts": len(self.by_action("unchanged")),
            "skipped_contacts": len(self.by_action("skip")),
            "tag_additions": sum(len(c.tags_add) for c in creates + updates),
            "tag_removals": sum(len(c.tags_remove) for c in updates),
            "merge_field_changes": sum(len(c.merge_changes) for c in updates),
            "changed_emails": len(self.changed_emails),
            "possible_changed_emails": len(self.possible_changed_emails),
        }


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # Mailchimp number fields may come back as 1001.0
    return str(value).strip()


def _desired_merge_fields(contact: DesiredContact, targets: MergeTargets) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if contact.first_name:
        fields[targets.first_name] = contact.first_name
    if contact.last_name:
        fields[targets.last_name] = contact.last_name
    if targets.club and contact.club:
        fields[targets.club] = contact.club
    if targets.membership_year and contact.membership_years:
        fields[targets.membership_year] = targets.coerce(
            targets.membership_year, max(contact.membership_years)
        )
    if targets.eventor_id and contact.person_ids:
        fields[targets.eventor_id] = targets.coerce(targets.eventor_id, min(contact.person_ids))
    if targets.mobile and contact.mobile:
        fields[targets.mobile] = contact.mobile
    return fields


def _digits(value: Any) -> str:
    return "".join(ch for ch in _clean(value) if ch.isdigit())


def compute_plan(
    desired: list[DesiredContact],
    existing: list[AudienceMember],
    *,
    managed_tags: frozenset[str],
    targets: MergeTargets,
    update_names: bool = True,
) -> Plan:
    existing_by_email = {m.email: m for m in existing}
    managed_cf = {t.casefold() for t in managed_tags}
    name_fields = {targets.first_name, targets.last_name}
    personal_fields = name_fields | ({targets.mobile} if targets.mobile else set())
    changes: list[ContactChange] = []
    desired_emails: set[str] = set()

    for contact in sorted(desired, key=lambda c: c.email):
        desired_emails.add(contact.email)
        wanted = _desired_merge_fields(contact, targets)
        current = existing_by_email.get(contact.email)
        if current is None:
            changes.append(
                ContactChange(
                    email=contact.email,
                    action="create",
                    name=contact.name,
                    merge_changes={tag: (None, value) for tag, value in wanted.items()},
                    tags_add=contact.tags,
                    desired=contact,
                )
            )
            continue

        existing_cf = {t.casefold(): t for t in current.tags}
        desired_cf = {t.casefold() for t in contact.tags}
        tags_add = frozenset(t for t in contact.tags if t.casefold() not in existing_cf)
        if current.is_protected:
            changes.append(
                ContactChange(
                    email=contact.email,
                    action="skip",
                    name=contact.name,
                    reason=current.status,
                    would_add_tags=tags_add,
                    desired=contact,
                    existing=current,
                )
            )
            continue

        merge_changes: dict[str, tuple[Any, Any]] = {}
        for tag, new in wanted.items():
            old = current.merge_fields.get(tag)
            if tag in name_fields and not update_names:
                continue
            # Names and mobiles belong to one person; on a shared address only fill gaps.
            if tag in personal_fields and contact.shared and _clean(old):
                continue
            if tag == targets.mobile:
                if _digits(old) != _digits(new):
                    merge_changes[tag] = (old, new)
                continue
            if _clean(old) != _clean(new):
                merge_changes[tag] = (old, new)
        tags_remove = frozenset(
            original
            for cf, original in existing_cf.items()
            if cf in managed_cf and cf not in desired_cf
        )
        action: Action = "update" if (merge_changes or tags_add or tags_remove) else "unchanged"
        changes.append(
            ContactChange(
                email=contact.email,
                action=action,
                name=contact.name,
                merge_changes=merge_changes,
                tags_add=tags_add,
                tags_remove=tags_remove,
                desired=contact,
                existing=current,
            )
        )

    # Contacts no longer in any source lose only the managed tags (lapse = tag removal).
    for member in sorted(existing, key=lambda m: m.email):
        if member.email in desired_emails:
            continue
        stale = frozenset(t for t in member.tags if t.casefold() in managed_cf)
        if not stale:
            continue
        first = _clean(member.merge_fields.get("FNAME"))
        last = _clean(member.merge_fields.get("LNAME"))
        name = f"{first} {last}".strip()
        if member.is_protected:
            changes.append(
                ContactChange(
                    email=member.email,
                    action="skip",
                    name=name,
                    reason=member.status,
                    existing=member,
                )
            )
        else:
            changes.append(
                ContactChange(
                    email=member.email,
                    action="update",
                    name=name,
                    tags_remove=stale,
                    existing=member,
                )
            )

    plan = Plan(changes=changes, managed_tags=managed_tags, audience_size=len(existing))
    _detect_changed_emails(plan, existing, targets)
    return plan


def _detect_changed_emails(
    plan: Plan, existing: list[AudienceMember], targets: MergeTargets
) -> None:
    creates = plan.by_action("create")
    if not creates:
        return
    matched: set[str] = set()
    if targets.eventor_id:
        by_person: dict[str, AudienceMember] = {}
        for member in existing:
            value = _clean(member.merge_fields.get(targets.eventor_id))
            if value:
                by_person.setdefault(value, member)
        for change in creates:
            assert change.desired is not None
            for pid in sorted(change.desired.person_ids):
                old = by_person.get(str(pid))
                if old is not None and old.email != change.email:
                    plan.changed_emails.append(
                        {
                            "person_id": pid,
                            "name": change.name,
                            "old_email": old.email,
                            "old_status": old.status,
                            "new_email": change.email,
                        }
                    )
                    matched.add(change.email)
                    break

    losing = [c for c in plan.changes if c.action in ("update", "skip") and c.desired is None]
    by_name: dict[str, list[ContactChange]] = {}
    for change in losing:
        if change.name:
            by_name.setdefault(change.name.casefold(), []).append(change)
    for change in creates:
        if change.email in matched or not change.name:
            continue
        for old in by_name.get(change.name.casefold(), []):
            plan.possible_changed_emails.append(
                {
                    "name": change.name,
                    "old_email": old.email,
                    "old_status": old.existing.status if old.existing else None,
                    "new_email": change.email,
                }
            )
