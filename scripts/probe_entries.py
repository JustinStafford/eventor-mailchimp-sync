"""Probe whether ``/entries`` exposes email addresses for non-member entrants.

Every Eventor endpoint that promises contact details is scoped to the key
owner's own organisation, so the expectation is that entrants from other clubs
(and casual entrants) come back without a ``Tele`` element even when
``includePersonElement=true``. This script checks that empirically against a
recent event organised by the key owner's club and prints counts only. No
personal data is printed or written.

Usage::

    uv run python scripts/probe_entries.py            # most recent event with entries
    uv run python scripts/probe_entries.py --event-id 12345
    uv run python scripts/probe_entries.py --months 12 --max-events 5 --starts

Reads ``EVENTOR_BASE_URL`` and ``EVENTOR_API_KEY`` from the environment or a
``.env`` file in the current directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eventor_client import AU_BASE_URL, EventorClient, ResponseCache
from eventor_client._xml import children, local_name


def build_client() -> EventorClient:
    load_dotenv()
    api_key = os.environ.get("EVENTOR_API_KEY", "").strip()
    if not api_key:
        sys.exit(
            "EVENTOR_API_KEY is not set. Put it in .env (see .env.example) or export it, "
            "then re-run."
        )
    base_url = os.environ.get("EVENTOR_BASE_URL", AU_BASE_URL).strip() or AU_BASE_URL
    cache = None
    cache_dir = os.environ.get("EVENTOR_CACHE_DIR")
    if cache_dir:
        cache = ResponseCache(cache_dir, float(os.environ.get("EVENTOR_CACHE_TTL_SECONDS", 3600)))
    return EventorClient(base_url, api_key, cache=cache, min_interval=0.5)


def shape(root, path: list[str]) -> dict[str, object]:
    """Describe the child element names and attribute names seen along ``path``.

    Values are never included; this only reports structure so it can be compared
    against the community XSD without touching personal data.
    """
    current = [root]
    for name in path:
        nxt = []
        for el in current:
            nxt.extend(children(el, name))
        current = nxt
    child_tags: Counter[str] = Counter()
    attrs: Counter[str] = Counter()
    for el in current:
        attrs.update(el.attrib.keys())
        child_tags.update(local_name(c.tag) for c in el)
    return {
        "count": len(current),
        "child_elements": dict(sorted(child_tags.items())),
        "attributes": dict(sorted(attrs.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--event-id", type=int, help="Probe this event instead of searching.")
    parser.add_argument("--months", type=int, default=6, help="How far back to look for events.")
    parser.add_argument(
        "--max-events",
        type=int,
        default=3,
        help="Probe up to this many recent events with entries.",
    )
    parser.add_argument("--starts", action="store_true", help="Also probe /starts/event.")
    parser.add_argument("--json", type=Path, help="Write the (personal-data-free) summary here.")
    args = parser.parse_args()

    with build_client() as client:
        org = client.organisation_for_api_key()
        print(f"API key belongs to organisation {org.id}: {org.name} ({org.short_name})")
        if org.id is None:
            sys.exit("Could not determine our organisation ID from /organisation/apiKey")

        if args.event_id:
            candidates = client.events(event_ids=[args.event_id])
        else:
            now = datetime.now()
            candidates = client.events(
                organisation_ids=[org.id],
                from_date=now - timedelta(days=30 * args.months),
                to_date=now,
            )
            candidates = sorted(
                (e for e in candidates if not e.is_cancelled),
                key=lambda e: e.start_date or datetime.min,
                reverse=True,
            )
        print(f"Found {len(candidates)} candidate event(s)")

        summary: dict[str, object] = {
            "base_url": client.base_url,
            "organisation_id": org.id,
            "events": [],
        }
        probed = 0
        for event in candidates:
            if probed >= args.max_events:
                break
            entries = client.entries(event_ids=[event.id], include_person_element=True)
            individual = [e for e in entries if not e.is_team]
            if not individual:
                when = f"{event.start_date:%Y-%m-%d}" if event.start_date else "?"
                print(f"- event {event.id} '{event.name}' ({when}): no entries")
                continue
            probed += 1

            counts: dict[str, Counter[str]] = {"own_org": Counter(), "other_org": Counter()}
            person_element_missing = 0
            for entry in individual:
                person = entry.person
                if person is None:
                    person_element_missing += 1
                    continue
                org_id = person.organisation_id or entry.organisation_id
                bucket = "own_org" if org_id == org.id else "other_org"
                counts[bucket]["entries"] += 1
                counts[bucket]["with_email"] += 1 if person.email else 0
                counts[bucket]["with_mobile"] += 1 if person.mobile else 0
                counts[bucket]["with_phone"] += 1 if person.phone else 0
                counts[bucket]["with_birth_date"] += 1 if person.birth_date else 0

            # Raw structure, so spec mismatches are visible without printing values.
            raw = client.get_xml(
                "/entries", {"eventIds": str(event.id), "includePersonElement": "true"}
            )
            structure = {
                "Entry": shape(raw, ["Entry"]),
                "Entry/Competitor": shape(raw, ["Entry", "Competitor"]),
                "Entry/Competitor/Person": shape(raw, ["Entry", "Competitor", "Person"]),
                "Entry/Competitor/Person/Tele": shape(
                    raw, ["Entry", "Competitor", "Person", "Tele"]
                ),
            }

            when = f"{event.start_date:%Y-%m-%d}" if event.start_date else "?"
            print(f"- event {event.id} '{event.name}' ({when})")
            print(
                f"    entries: {len(entries)} total, {len(individual)} individual, "
                f"{len(entries) - len(individual)} team; "
                f"{person_element_missing} without a Person element"
            )
            for bucket, label in (("own_org", "our organisation"), ("other_org", "other/none")):
                c = counts[bucket]
                print(
                    f"    {label:<18}: {c['entries']:>4} entries, "
                    f"{c['with_email']:>4} with email, {c['with_mobile']:>4} with mobile, "
                    f"{c['with_phone']:>4} with phone, {c['with_birth_date']:>4} with birth date"
                )
            print(f"    Person child elements seen: {structure['Entry/Competitor/Person']}")
            print(f"    Tele attributes seen:       {structure['Entry/Competitor/Person/Tele']}")

            event_summary: dict[str, object] = {
                "event_id": event.id,
                "start_date": event.start_date.isoformat() if event.start_date else None,
                "entries_total": len(entries),
                "entries_individual": len(individual),
                "person_element_missing": person_element_missing,
                "own_org": dict(counts["own_org"]),
                "other_org": dict(counts["other_org"]),
                "structure": structure,
            }

            if args.starts:
                start_list = client.event_starts(event.id)
                with_person = [s for s in start_list.starts if s.person is not None]
                with_email = [s for s in with_person if s.person and s.person.email]
                own = [
                    s
                    for s in with_person
                    if org.id in (s.organisation_id, s.person.organisation_id if s.person else None)
                ]
                print(
                    f"    starts: {len(start_list.starts)} starters, {len(with_person)} with a "
                    f"Person element, {len(own)} from our organisation, "
                    f"{len(with_email)} with email"
                )
                event_summary["starts"] = {
                    "starters": len(start_list.starts),
                    "with_person_element": len(with_person),
                    "own_org": len(own),
                    "with_email": len(with_email),
                }
            summary["events"].append(event_summary)  # type: ignore[union-attr]

        if probed == 0:
            print("No events with entries found; try --months or --event-id.")

    if args.json:
        args.json.write_text(json.dumps(summary, indent=2))
        print(f"Summary written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
