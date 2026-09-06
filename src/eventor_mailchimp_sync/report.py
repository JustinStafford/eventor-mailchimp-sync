"""Human-readable diff and machine-readable JSON report for a run."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from eventor_mailchimp_sync.config import SyncConfig
from eventor_mailchimp_sync.diff import ContactChange, Plan
from eventor_mailchimp_sync.sources import PullResult

REPORT_VERSION = 1


def redact_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def redact_name(name: str) -> str:
    return " ".join(part[:1] + "." for part in name.split())


def _email(email: str, redact: bool) -> str:
    return redact_email(email) if redact else email


def _name(name: str, redact: bool) -> str:
    return redact_name(name) if redact else name


def _tags(tags: frozenset[str]) -> str:
    return ", ".join(sorted(tags))


def render_diff(
    plan: Plan,
    pull: PullResult,
    config: SyncConfig,
    *,
    apply: bool,
    redact: bool = False,
    warnings: list[str] | None = None,
) -> str:
    """Render the plan as readable text for the terminal or a CI log."""
    lines: list[str] = []
    mode = "APPLY" if apply else "DRY RUN (no changes written; use --apply)"
    org = pull.organisation
    lines.append(f"Eventor -> Mailchimp sync: {mode}")
    lines.append(
        f"Organisation: {org.name or org.short_name} (#{org.id}) via {config.eventor_base_url}"
    )
    lines.append(
        f"Members: {pull.stats.get('memberships', 0)} from /{pull.member_source} for "
        f"{', '.join(str(y) for y in pull.membership_years)}"
        + (
            f" ({pull.stats['memberships_unpaid_skipped']} unpaid skipped)"
            if pull.stats.get("memberships_unpaid_skipped")
            else ""
        )
    )
    lines.append(
        f"Entrants: {pull.stats.get('entries', 0)} entries to {pull.stats.get('events', 0)} events "
        f"between {pull.window_start:%Y-%m-%d} and {pull.window_end:%Y-%m-%d}"
        + (
            f" ({pull.stats['entries_team_skipped']} team entries skipped)"
            if pull.stats.get("entries_team_skipped")
            else ""
        )
    )
    if pull.stats.get("starts"):
        lines.append(f"Starters: {pull.stats['starts']} from published start lists")
    lines.append(
        f"Desired contacts: {len(pull.contacts)}   Audience contacts: {plan.audience_size}   "
        f"Managed tags: {_tags(plan.managed_tags)}"
    )
    for warning in warnings or []:
        lines.append(f"WARNING: {warning}")
    lines.append("")

    def section(title: str, items: list[ContactChange], fmt) -> None:
        lines.append(f"{title} ({len(items)})")
        if not items:
            lines.append("  none")
        for item in items:
            lines.append("  " + fmt(item))
        lines.append("")

    section(
        "New contacts",
        plan.by_action("create"),
        lambda c: (
            f"+ {_email(c.email, redact):<40} {_name(c.name, redact):<28} [{_tags(c.tags_add)}]"
        ),
    )

    updates = plan.by_action("update")
    tag_updates = [c for c in updates if c.tags_add or c.tags_remove]
    merge_updates = [c for c in updates if c.merge_changes]

    def fmt_tags(c: ContactChange) -> str:
        parts = [f"+{t}" for t in sorted(c.tags_add)] + [f"-{t}" for t in sorted(c.tags_remove)]
        return f"~ {_email(c.email, redact):<40} {_name(c.name, redact):<28} {' '.join(parts)}"

    section("Tag changes", tag_updates, fmt_tags)

    def fmt_merge(c: ContactChange) -> str:
        parts = [f"{tag}: {old!r} -> {new!r}" for tag, (old, new) in c.merge_changes.items()]
        return f"~ {_email(c.email, redact):<40} {'; '.join(parts)}"

    section("Merge-field changes", merge_updates, fmt_merge)

    def fmt_skip(c: ContactChange) -> str:
        extra = ""
        if c.would_add_tags:
            extra = f" (would add: {_tags(c.would_add_tags)})"
        elif c.existing and c.desired is None:
            stale = sorted(t for t in c.existing.tags if t in plan.managed_tags)
            extra = f" (stale managed tags left in place: {', '.join(stale)})"
        return f"- {_email(c.email, redact):<40} {c.reason}{extra}"

    section("Skipped (never written to)", plan.by_action("skip"), fmt_skip)

    members_without = [p for p in pull.no_email if p.is_member]
    others_without = [p for p in pull.no_email if not p.is_member]
    lines.append(f"No email address ({len(pull.no_email)})")
    if not pull.no_email:
        lines.append("  none")
    for person in members_without:
        pid = f"person #{person.person_id}" if person.person_id is not None else "no person ID"
        lines.append(
            f"  ? {_name(person.name, redact):<28} member   {pid} via {', '.join(person.sources)}"
        )
    if others_without:
        lines.append(
            f"  ? {len(others_without)} non-member entrants: Eventor exposes no contact details "
            f"for them on /entries (full list in the JSON report)"
        )
    lines.append("")

    shared = [c for c in pull.contacts if c.shared]
    if shared:
        lines.append(f"Shared email addresses ({len(shared)}) - name taken from the primary person")
        for c in shared:
            others = len(c.person_ids) - 1
            lines.append(
                f"  = {_email(c.email, redact):<40} {_name(c.name, redact)} (+{others} more)"
            )
        lines.append("")

    if plan.changed_emails:
        lines.append(
            f"Changed email addresses ({len(plan.changed_emails)}) - matched by Eventor ID"
        )
        for item in plan.changed_emails:
            lines.append(
                f"  > {_name(item['name'], redact)}: {_email(item['old_email'], redact)} "
                f"({item['old_status']}) -> {_email(item['new_email'], redact)}"
            )
        lines.append("")
    if plan.possible_changed_emails:
        lines.append(
            f"Possible changed email addresses ({len(plan.possible_changed_emails)}) - same name"
        )
        for item in plan.possible_changed_emails:
            lines.append(
                f"  ? {_name(item['name'], redact)}: {_email(item['old_email'], redact)} "
                f"({item['old_status']}) -> {_email(item['new_email'], redact)}"
            )
        lines.append("")

    rejected = [c for c in plan.changes if c.rejected]
    if rejected:
        lines.append(f"Rejected by Mailchimp ({len(rejected)}) - fix the address in Eventor")
        for c in rejected:
            lines.append(f"  x {_email(c.email, redact):<40} {_name(c.name, redact):<28} {c.error}")
        lines.append("")

    errors = [c for c in plan.changes if c.error and not c.rejected]
    if errors:
        lines.append(f"API errors ({len(errors)})")
        for c in errors:
            lines.append(f"  ! {_email(c.email, redact):<40} {c.error}")
        lines.append("")

    s = plan.summary()
    lines.append(
        f"Summary: {s['new_contacts']} new, {s['updated_contacts']} updated "
        f"({s['tag_additions']} tag additions, {s['tag_removals']} tag removals, "
        f"{s['merge_field_changes']} merge-field changes), {s['unchanged_contacts']} unchanged, "
        f"{s['skipped_contacts']} skipped, {len(pull.no_email)} without email"
        + (f", {len(rejected)} rejected by Mailchimp" if rejected else "")
        + (f", {len(errors)} API errors" if errors else "")
    )
    return "\n".join(lines)


def _change_to_json(change: ContactChange, redact: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "email": _email(change.email, redact),
        "name": _name(change.name, redact),
        "action": change.action,
    }
    if change.reason:
        data["reason"] = change.reason
    if change.merge_changes:
        data["merge_fields"] = {
            tag: {"old": old, "new": new} for tag, (old, new) in change.merge_changes.items()
        }
    if change.tags_add:
        data["tags_add"] = sorted(change.tags_add)
    if change.tags_remove:
        data["tags_remove"] = sorted(change.tags_remove)
    if change.would_add_tags:
        data["would_add_tags"] = sorted(change.would_add_tags)
    if change.existing is not None:
        data["existing_status"] = change.existing.status
    if change.desired is not None:
        data["sources"] = list(change.desired.sources)
        data["person_ids"] = sorted(change.desired.person_ids)
        if change.desired.shared:
            data["shared_email"] = True
    if change.action in ("create", "update"):
        data["applied"] = change.applied
    if change.rejected:
        data["rejected"] = True
    if change.error:
        data["error"] = change.error
    return data


def build_report(
    plan: Plan,
    pull: PullResult,
    config: SyncConfig,
    *,
    apply: bool,
    started_at: datetime,
    finished_at: datetime,
    ok: bool,
    audience_name: str | None = None,
    merge_fields_present: dict[str, str | None] | None = None,
    warnings: list[str] | None = None,
    redact: bool = False,
) -> dict[str, Any]:
    """Assemble the JSON report. Everything a later step needs is in ``exceptions``."""
    org = pull.organisation
    errors = [c for c in plan.changes if c.error and not c.rejected]
    rejected = [c for c in plan.changes if c.rejected]
    exceptions: dict[str, Any] = {
        "no_email": [
            {
                "person_id": p.person_id,
                "name": _name(p.name, redact),
                "is_member": p.is_member,
                "sources": list(p.sources),
            }
            for p in pull.no_email
        ],
        "unsubscribed": [],
        "cleaned": [],
        "archived": [],
        "shared_email": [
            {
                "email": _email(c.email, redact),
                "name": _name(c.name, redact),
                "person_ids": sorted(c.person_ids),
            }
            for c in pull.contacts
            if c.shared
        ],
        "changed_email": [
            {
                **item,
                "name": _name(item["name"], redact),
                "old_email": _email(item["old_email"], redact),
                "new_email": _email(item["new_email"], redact),
            }
            for item in plan.changed_emails
        ],
        "possible_changed_email": [
            {
                **item,
                "name": _name(item["name"], redact),
                "old_email": _email(item["old_email"], redact),
                "new_email": _email(item["new_email"], redact),
            }
            for item in plan.possible_changed_emails
        ],
        "rejected": [
            {
                "email": _email(c.email, redact),
                "name": _name(c.name, redact),
                "action": c.action,
                "person_ids": sorted(c.desired.person_ids) if c.desired else [],
                "sources": list(c.desired.sources) if c.desired else [],
                "error": c.error,
            }
            for c in rejected
        ],
        "api_errors": [
            {"email": _email(c.email, redact), "action": c.action, "error": c.error} for c in errors
        ],
    }
    for change in plan.by_action("skip"):
        bucket = exceptions.get(change.reason or "")
        if isinstance(bucket, list):
            bucket.append(_change_to_json(change, redact))

    return {
        "report_version": REPORT_VERSION,
        "ok": ok,
        "mode": "apply" if apply else "dry-run",
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "redacted": redact,
        "eventor": {
            "base_url": config.eventor_base_url,
            "organisation_id": org.id,
            "organisation_name": org.name or org.short_name,
            "member_source": pull.member_source,
            "membership_years": list(pull.membership_years),
            "window_start": pull.window_start.isoformat(timespec="seconds"),
            "window_end": pull.window_end.isoformat(timespec="seconds"),
            "events": [
                {
                    "id": e.id,
                    "name": e.name,
                    "start_date": e.start_date.isoformat(timespec="seconds")
                    if e.start_date
                    else None,
                    "series_tags": sorted(r.tag for r in config.series if r.matches(e)),
                }
                for e in pull.events
            ],
            "stats": pull.stats,
        },
        "mailchimp": {
            "list_id": config.mailchimp_list_id,
            "audience_name": audience_name,
            "audience_contacts": plan.audience_size,
            "merge_fields": merge_fields_present or {},
            "managed_tags": sorted(plan.managed_tags),
            "warnings": list(warnings or []),
        },
        "summary": plan.summary(),
        "changes": [_change_to_json(c, redact) for c in plan.changes if c.action != "unchanged"],
        "exceptions": exceptions,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
