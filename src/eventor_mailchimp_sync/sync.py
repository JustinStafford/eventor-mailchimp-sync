"""Orchestrate one run: pull from Eventor, read the audience, plan, optionally apply."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from eventor_client import EventorClient, ResponseCache
from eventor_mailchimp_sync.config import SyncConfig
from eventor_mailchimp_sync.diff import ContactChange, Plan, compute_plan, resolve_merge_targets
from eventor_mailchimp_sync.mailchimp import MailchimpClient, MailchimpError
from eventor_mailchimp_sync.report import build_report, render_diff
from eventor_mailchimp_sync.sources import PullResult, pull

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RunResult:
    ok: bool
    apply: bool
    plan: Plan
    pull: PullResult
    report: dict[str, Any]
    diff_text: str

    @property
    def api_errors(self) -> list[ContactChange]:
        return [c for c in self.plan.changes if c.error and not c.rejected]

    @property
    def rejected(self) -> list[ContactChange]:
        return [c for c in self.plan.changes if c.rejected]


def make_eventor_client(config: SyncConfig) -> EventorClient:
    cache = None
    if config.eventor_cache_dir is not None:
        cache = ResponseCache(config.eventor_cache_dir, config.eventor_cache_ttl_seconds)
    return EventorClient(
        config.eventor_base_url, config.eventor_api_key, cache=cache, min_interval=0.25
    )


def make_mailchimp_client(config: SyncConfig) -> MailchimpClient:
    return MailchimpClient(
        config.mailchimp_api_key, config.mailchimp_server_prefix, config.mailchimp_list_id
    )


def apply_change(client: MailchimpClient, change: ContactChange) -> None:
    """Write one planned change. Never touches subscription status."""
    if change.action == "create":
        client.upsert_member(change.email, merge_fields=change.merge_payload, tags=change.tags_add)
    elif change.action == "update":
        if change.merge_changes:
            client.upsert_member(change.email, merge_fields=change.merge_payload)
        if change.tags_add or change.tags_remove:
            client.update_tags(change.email, add=change.tags_add, remove=change.tags_remove)
    change.applied = True


def run(
    config: SyncConfig,
    *,
    apply: bool = False,
    now: datetime | None = None,
    eventor_client: EventorClient | None = None,
    mailchimp_client: MailchimpClient | None = None,
    redact: bool = False,
) -> RunResult:
    """Execute a sync. Raises ``EventorError``/``MailchimpError`` if a read fails.

    Write failures during ``apply`` are recorded per contact and the run
    continues, so one bad address does not block the rest; ``ok`` is ``False``
    if any write failed.
    """
    started = datetime.now()
    now = now or started
    own_eventor = eventor_client is None
    own_mailchimp = mailchimp_client is None
    eventor_client = eventor_client or make_eventor_client(config)
    mailchimp_client = mailchimp_client or make_mailchimp_client(config)
    try:
        pulled = pull(eventor_client, config, now)
        log.info(
            "Eventor pull done in %.0fs: %d desired contacts",
            (datetime.now() - started).total_seconds(),
            len(pulled.contacts),
        )

        log.info("reading the Mailchimp audience")
        info = mailchimp_client.list_info()
        fields = mailchimp_client.merge_fields()
        members = mailchimp_client.all_members()
        log.info("%d audience contacts fetched; computing the diff", len(members))
        targets = resolve_merge_targets(fields, config)
        plan = compute_plan(
            pulled.contacts,
            members,
            managed_tags=pulled.managed_tags,
            targets=targets,
            update_names=config.update_names,
        )

        if apply:
            pending = [c for c in plan.changes if c.has_writes]
            log.info("applying %d changes to Mailchimp", len(pending))
            for index, change in enumerate(pending, 1):
                if index % 25 == 0:
                    log.info("applied %d of %d", index, len(pending))
                try:
                    apply_change(mailchimp_client, change)
                except MailchimpError as exc:
                    change.error = str(exc)
                    if exc.is_contact_rejection:
                        # Data problem with this one contact: report it, keep going, and
                        # do not fail the run over it.
                        change.rejected = True
                        log.warning("Mailchimp rejected %s: %s", change.email, exc)
                    else:
                        log.error("failed to apply change for %s: %s", change.email, exc)
    finally:
        if own_eventor:
            eventor_client.close()
        if own_mailchimp:
            mailchimp_client.close()

    ok = not any(c.error and not c.rejected for c in plan.changes)
    finished = datetime.now()
    merge_present = {
        "FNAME": targets.first_name,
        "LNAME": targets.last_name,
        config.merge_club: targets.club,
        config.merge_membership_year: targets.membership_year,
        config.merge_eventor_id: targets.eventor_id,
    }
    if config.merge_mobile:
        merge_present[config.merge_mobile] = targets.mobile
    report = build_report(
        plan,
        pulled,
        config,
        apply=apply,
        started_at=started,
        finished_at=finished,
        ok=ok,
        audience_name=info.get("name"),
        merge_fields_present=merge_present,
        warnings=list(targets.warnings),
        redact=redact,
    )
    diff_text = render_diff(
        plan, pulled, config, apply=apply, redact=redact, warnings=list(targets.warnings)
    )
    return RunResult(ok=ok, apply=apply, plan=plan, pull=pulled, report=report, diff_text=diff_text)


def with_overrides(config: SyncConfig, **overrides: Any) -> SyncConfig:
    """Return a copy of ``config`` with non-``None`` overrides applied."""
    return replace(config, **{k: v for k, v in overrides.items() if v is not None})
