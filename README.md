# eventor-mailchimp-sync

Keep a Mailchimp audience in sync with an orienteering club's **Eventor** members and event
entrants. Built for clubs on the Australian Eventor instance, but works with the Swedish,
Norwegian and IOF instances too.

Every run pulls the club's current members and recent entrants from Eventor, compares them with
the Mailchimp audience, and (only when you say `--apply`) adds missing contacts, updates names
and tags, and removes tags from people who have lapsed. It never unsubscribes, deletes, archives
or resubscribes anyone.

```
$ eventor-mailchimp-sync sync
Eventor -> Mailchimp sync: DRY RUN (no changes written; use --apply)
Organisation: Example Orienteers (#123) via https://eventor.orienteering.asn.au/api
Members: 214 from /memberships for 2026
Entrants: 1,380 entries to 31 events between 2025-09-06 and 2026-09-06
Desired contacts: 611   Audience contacts: 742   Managed tags: entrant, member, member-2026, series-sprint

New contacts (4)
  + new.person@example.com                  New Person                   [entrant, series-sprint]
  ...
Tag changes (12)
  ~ sam.sample@example.com                  Sam Sample                   +member-2026 -entrant
  ...
Skipped (never written to) (3)
  - bounced@example.com                     cleaned (stale managed tags left in place: member)
  ...
Summary: 4 new, 12 updated (15 tag additions, 9 tag removals, 3 merge-field changes), 590 unchanged, 3 skipped, 6 without email
Report written to report.json
```

## Contents

- [How it works](#how-it-works)
- [What gets synced](#what-gets-synced)
- [The never-resubscribe guarantee](#the-never-resubscribe-guarantee)
- [Setting it up for your club](#setting-it-up-for-your-club)
- [Configuration reference](#configuration-reference)
- [Running it on a schedule](#running-it-on-a-schedule)
- [The run report](#the-run-report)
- [Using the Eventor client on its own](#using-the-eventor-client-on-its-own)
- [Development](#development)
- [Licence](#licence)

## How it works

**Eventor has no webhooks**, so polling is the only option. Rather than tracking what changed
since last time, the tool does a **full pull every run** and computes an idempotent diff. Club data
is small (hundreds of members, low thousands of entries a year), so a full pull is cheap and far
more robust than incremental state: there is no cursor, no database and nothing to get out of
step. Run it twice and the second run does nothing.

Each run:

1. Asks Eventor which organisation the API key belongs to (`GET /organisation/apiKey`), so the
   club ID is never hard-coded.
2. Fetches memberships for the current year (and last year's until the end of March, because
   renewals trickle in) from `GET /memberships` on the Australian instance, or everyone attached
   to the organisation from `GET /persons/organisations/{id}` elsewhere.
3. Fetches the club's events in a trailing window (default 12 months) from `GET /events`, then
   every online entry to those events from `GET /entries`. Optionally, published start lists from
   `GET /starts/event` too, which include walk-up entries.
4. Deduplicates on lower-cased, stripped email and works out the tags and merge fields every
   contact should have.
5. Reads the whole Mailchimp audience, including archived contacts, and computes the diff.
6. Prints the diff, writes a JSON report and, with `--apply`, writes the changes.

The Eventor client is a polite API citizen: there is no published rate limit, so it spaces
requests out, backs off exponentially on 5xx and 429 responses, and can cache responses on disk
(opt-in, TTL configurable) so repeated dry runs during setup do not hammer the server.

## What gets synced

| Source | Who | Tags | Merge fields |
| --- | --- | --- | --- |
| `/memberships` (AU) or `/persons/organisations` | Paid members for each synced year | `member-YYYY` per year, plus `member` | `FNAME`, `LNAME`, `PHONE`, `CLUB`, `MEMBERYEAR` |
| `/entries` for the club's events in the window | Everyone with an online entry whose email the club can see (see below) | `entrant`, plus one tag per configured event series | `FNAME`, `LNAME`, `PHONE`, `CLUB` |
| `/starts/event` (optional) | Everyone on a published start list | as for entries | as for entries |

Tags the tool owns are called **managed tags**: `member-YYYY` for the years being synced, the
aggregate `member` tag, `entrant`, and any series tags from `config.toml`. When someone no longer
qualifies, the managed tag is removed. **Membership lapse is a tag removal, never a delete or an
archive.** Tags you add by hand in Mailchimp (`Volunteer`, `Committee`, ...) are never touched, and
neither are `member-YYYY` tags for years that are not being synced, so history is preserved.

Merge fields:

- `FNAME` and `LNAME` are updated when Eventor's value differs (turn off with
  `SYNC_UPDATE_NAMES=false`).
- `PHONE` receives the person's mobile number, tidied to E.164 (`0412 345 678` becomes
  `+61412345678`; the country code follows the Eventor instance, override with
  `SYNC_PHONE_COUNTRY_CODE`). Two numbers that differ only in formatting are treated as equal,
  and an existing number is never blanked when Eventor has none. Mailchimp's `PHONE` field must
  be in international format, which is the default for non-US accounts; if it is set to US
  format the sync warns and leaves mobiles alone. Set `MAILCHIMP_MERGE_MOBILE=none` to skip
  mobiles entirely.
- `CLUB`, `MEMBERYEAR` (the latest synced year the person was a member) and `EVENTORID` (the
  person's Eventor ID) are written **only if those merge fields exist in your audience**. Create
  them in Mailchimp under *Audience > Settings > Audience fields and \*|MERGE|\* tags* if you want
  them. Mailchimp merge tags are limited to 10 characters, which is why it is `MEMBERYEAR` rather
  than `MEMBERSHIP_YEAR`; the names are configurable.
- Several people often share one address (a family membership). The contact gets the union of
  everyone's tags. The name comes from the primary person (a member before a non-member, then the
  oldest), and for shared addresses existing non-empty names in Mailchimp are never overwritten.
  Shared addresses are listed in the report so you can review them.

### Contact details for entrants

**`/entries` returns no contact details for anyone.** This was verified against the Australian
instance in September 2026 with `includePersonElement=true`: every entry carried a `Person` with
name, birth date, sex and `OrganisationId`, and not one carried a `Tele` element, not even for the
key owner's own members. The same holds with the query scoped to the club's own
`organisationIds`, with every documented and undocumented `include*` flag, and on
`/starts/event`, `/results/event` and their IOF XML variants: not one `@` anywhere in the
responses. The AU documentation offers `includeContactDetails` only on `/memberships` and
`/persons/organisations`. The endpoints that do return emails
(`/memberships` and `/persons/organisations/{id}`) are scoped to the key owner's own
organisation.

So the effective scope is **members, plus which members entered**, not everyone who entered:

- Every entry is matched by Eventor person ID to the membership pull, so members who entered are
  always tagged `entrant` (and with any series tags).
- With `SYNC_PERSONS_CONTACT_INDEX=true`, entrants are also looked up among everyone still
  attached to the club in Eventor via `/persons/organisations`, which includes lapsed members.
  They get `entrant` and series tags only, never `member`. Off by default; turn it on if you want
  lapsed members who still race with you back on the list.
- Entrants from other clubs and casual entrants have no email available through the API. They
  are counted in the diff and listed by name and person ID under `exceptions.no_email` in the
  JSON report, but they cannot be synced. If you want them on the list, the entry form or the
  event day is the place to collect consent and an address.

`scripts/probe_entries.py` re-checks this for your instance and key, printing counts and
element structure only (no personal data). See [Development](#development).

## The never-resubscribe guarantee

Someone who unsubscribed, bounced or was archived must stay that way, whatever Eventor says.
This is enforced in code, not policy:

- Contacts are written with `PUT /lists/{list_id}/members/{md5(email)}` and
  `status_if_new: "subscribed"`. **The `status` field is never sent**, so an existing contact's
  subscription status cannot change. New contacts are created as subscribed without double
  opt-in; if your club needs double opt-in, do not use this tool to add contacts.
- Contacts whose status is `unsubscribed`, `cleaned` or `archived` are never written to at all:
  no merge-field updates, no tag additions, no tag removals. They appear in the diff and the
  report as *skipped* with the reason.
- The Mailchimp client has no delete, archive or unsubscribe method.
- Mailchimp's batch subscribe endpoint (`POST /lists/{list_id}`) is deliberately **not** used: it
  requires a `status` per member, which with `update_existing: true` would risk flipping an
  unsubscribed contact back. Per-contact `PUT` plus the tags endpoint is the safe path and, at
  club scale, plenty fast.

Tests cover each of these rules (`tests/test_diff.py`, `tests/test_mailchimp.py`,
`tests/test_sync_cli.py`).

## Setting it up for your club

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

1. **Get an Eventor API key.** Club API keys are issued by your federation (in Australia, ask
   Orienteering Australia). The key identifies your club; the tool discovers the club ID itself.
2. **Get a Mailchimp API key** (*Account > Extras > API keys*) and the **audience ID** (*Audience
   > Settings > Audience name and defaults*). The key's `-usNN` suffix is the server prefix.
3. **Optionally add merge fields** `CLUB`, `MEMBERYEAR` (number) and `EVENTORID` (text) to the
   audience. `EVENTORID` enables reliable changed-email detection.
4. Clone and install:

   ```bash
   git clone https://github.com/JustinStafford/eventor-mailchimp-sync.git
   cd eventor-mailchimp-sync
   uv sync
   cp .env.example .env   # then fill in the keys; .env is git-ignored
   ```

5. Check both connections:

   ```bash
   uv run eventor-mailchimp-sync whoami
   ```

   ```bash
   uv run eventor-mailchimp-sync audience
   ```

6. Dry run (the default; nothing is written):

   ```bash
   uv run eventor-mailchimp-sync sync
   ```

   Read the diff and `report.json`. Check the *Skipped* and *No email address* sections in
   particular. When it looks right:

   ```bash
   uv run eventor-mailchimp-sync sync --apply
   ```

7. Optionally create `config.toml` (git-ignored; see `config.example.toml`) to tag entrants by
   event series, for example `series-sprint` for everyone who entered a Sprint Series round.

`eventor-mailchimp-sync sync --help` lists the options: `--apply`, `--report PATH`,
`--config PATH`, `--window-months N`, `--redact` (mask emails and names in the diff and
report) and `--quiet`.

Exit codes: `0` success, `1` configuration problem, `2` Eventor API failure, `3` Mailchimp API
failure, including any write that failed during `--apply` (the other writes still go through and
the failures are listed in the report).

## Configuration reference

Everything is an environment variable; `.env` is loaded automatically for local development.
Only the first four are required. An empty value means "use the default" (GitHub Actions
passes unset variables as empty strings); where a feature can be switched off, the value is
`none`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `EVENTOR_API_KEY` | | Club API key (`ApiKey` header) |
| `MAILCHIMP_API_KEY` | | Mailchimp Marketing API key |
| `MAILCHIMP_LIST_ID` | | Audience ID |
| `EVENTOR_BASE_URL` | `https://eventor.orienteering.asn.au/api` | Eventor instance, with `/api` |
| `MAILCHIMP_SERVER_PREFIX` | suffix of the API key | Data centre, e.g. `us21` |
| `EVENTOR_CACHE_DIR` | unset (no cache) | Directory for cached Eventor responses |
| `EVENTOR_CACHE_TTL_SECONDS` | `3600` | Cache lifetime |
| `SYNC_WINDOW_MONTHS` | `12` | Trailing window for events and entrants |
| `SYNC_PREVIOUS_YEAR_UNTIL_MONTH` | `3` | Up to and including this month, last year's members are still synced (`0` to disable) |
| `SYNC_MEMBER_SOURCE` | `auto` | `memberships` (AU), `persons`, or `auto` by instance |
| `SYNC_REQUIRE_PAID` | `true` | Ignore unpaid memberships |
| `SYNC_MEMBER_TAG` | `member` | Aggregate tag for current members; `none` to disable |
| `SYNC_ENTRANT_TAG` | `entrant` | Tag for entrants |
| `SYNC_INCLUDE_STARTS` | `false` | Also read published start lists (walk-ups); one request per event |
| `SYNC_PERSONS_CONTACT_INDEX` | `false` | Also match entrants against everyone attached to the club (lapsed members) for an email |
| `SYNC_UPDATE_NAMES` | `true` | Update `FNAME`/`LNAME` from Eventor |
| `MAILCHIMP_MERGE_CLUB` | `CLUB` | Merge tag for the club name |
| `MAILCHIMP_MERGE_MEMBERSHIP_YEAR` | `MEMBERYEAR` | Merge tag for the latest membership year |
| `MAILCHIMP_MERGE_EVENTOR_ID` | `EVENTORID` | Merge tag for the Eventor person ID |
| `MAILCHIMP_MERGE_MOBILE` | `PHONE` | Merge tag for the mobile number; `none` to skip mobiles |
| `SYNC_PHONE_COUNTRY_CODE` | by instance (`61`, `46`, `47`) | Country code used to turn local numbers into E.164; `none` to leave numbers as typed |
| `SYNC_CONFIG` | `config.toml` if present | TOML file with series mappings |
| `SYNC_REPORT_PATH` | `report.json` | Where the JSON report is written |

`config.toml`:

```toml
[series.sprint]
tag = "series-sprint"
event_ids = [12345, 12346]              # match by Eventor event ID (parents of multi-day events too)
name_patterns = ["sprint series"]       # and/or case-insensitive regular expressions on the event name
```

## Running it on a schedule

### GitHub Actions

Run the sync from a **private** repository, not from a public one. GitHub keeps secrets safe
either way, but on a public repository every workflow log and artifact is readable by anyone,
and the sync prints names and email addresses in its diff and uploads them in the report.

The recommended setup is a private repository containing only a workflow and, optionally, a
`config.toml`; the workflow installs this tool from GitHub with `uvx` on every run.
[`examples/private-runner/`](examples/private-runner/) has the workflow and step-by-step
instructions. In short: create a private repository, copy the workflow in, add the secrets
`EVENTOR_API_KEY`, `MAILCHIMP_API_KEY` and `MAILCHIMP_LIST_ID`, pin `TOOL_REF` to a tag or
commit, and run it once by hand with *apply* unticked.

The workflow runs daily at 16:07 UTC (02:07 Sydney time in winter, 03:07 in summer; GitHub cron
is UTC only, so edit the `cron` line to change it) and on demand (`workflow_dispatch`, with an
*apply* tick box that defaults to on), then uploads `report.json` as an artifact for 14 days.
GitHub may start scheduled runs up to half an hour late, and it switches schedules off in
repositories with no commits for 60 days, so expect an email asking you to re-enable it if the
runner repository sits untouched.

This repository's own `.github/workflows/sync.yml` is the same workflow gated on a repository
variable `SYNC_ENABLED`, so it stays dormant here. `.github/workflows/ci.yml` runs ruff and
pytest on every push and pull request.

### launchd (macOS)

`examples/launchd/au.org.example.eventor-mailchimp-sync.plist` runs the sync every morning at
05:30 from a checkout with a `.env`. Edit the paths, copy it to `~/Library/LaunchAgents/` and
load it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/au.org.example.eventor-mailchimp-sync.plist
```

### cron (Linux)

```
30 5 * * * cd /home/club/eventor-mailchimp-sync && /usr/local/bin/uv run --frozen eventor-mailchimp-sync sync --apply --quiet >> sync.log 2>&1
```

## The run report

Alongside the readable diff, every run writes a JSON report so a person, a script or an agent can
act on the exceptions without re-running anything.

```jsonc
{
  "report_version": 1,
  "ok": true,                      // false if any write failed
  "mode": "dry-run",               // or "apply"
  "eventor": { "organisation_id": 123, "membership_years": [2026], "events": [...], "stats": {...} },
  "mailchimp": { "audience_name": "Club news", "merge_fields": {...}, "managed_tags": [...] },
  "summary": { "new_contacts": 4, "updated_contacts": 12, "skipped_contacts": 3, ... },
  "changes": [ { "email": "...", "action": "create|update|skip", "tags_add": [...], "tags_remove": [...],
                 "merge_fields": { "FNAME": { "old": "", "new": "Sam" } }, "applied": true } ],
  "exceptions": {
    "no_email": [ { "person_id": 1005, "name": "Kim Noemail", "is_member": true, "sources": ["membership:2026"] } ],
    "unsubscribed": [...], "cleaned": [...], "archived": [...],   // skipped contacts, by reason
    "shared_email": [...],                                        // one address, several people
    "changed_email": [...],            // same EVENTORID, different address (needs the merge field)
    "possible_changed_email": [...],   // same name, different address (heuristic)
    "api_errors": [...]
  }
}
```

Typical follow-ups: chase members in `no_email` for an address, decide whether `cleaned`
(bounced) members need a phone call, and merge the two contacts listed in `changed_email`.

## Using the Eventor client on its own

`eventor_client` has no Mailchimp knowledge and is meant to be reused:

```python
from datetime import datetime, timedelta

from eventor_client import AU_BASE_URL, EventorClient, ResponseCache

cache = ResponseCache(".eventor-cache", ttl_seconds=3600)
with EventorClient(AU_BASE_URL, api_key, cache=cache) as client:
    org = client.organisation_for_api_key()
    # Australian instance only; everywhere else use persons_in_organisation()
    members = client.memberships(org.id, 2026)
    people = client.persons_in_organisation(org.id)
    since = datetime.now() - timedelta(days=90)
    events = client.events(organisation_ids=[org.id], from_date=since)
    # organisation_ids on entries() filters by the entrant's club, so pass event IDs
    entries = client.entries(event_ids=[e.id for e in events])
    # Every starter, including walk-ups, once the start list is published
    starts = client.event_starts(events[0].id)
    # Any other endpoint: the parsed root element
    results = client.get_xml("/results/event", {"eventId": str(events[0].id)})
```

Results are frozen dataclasses (`Organisation`, `Person`, `Membership`, `Event`, `Entry`,
`StartList`). `get_xml` returns the parsed root element for endpoints without a dedicated
method. Eventor dates are `yyyy-mm-dd hh:mm:ss`; pass `date`/`datetime` objects and the client
formats them. `entries()` exposes `from_modify_date`/`to_modify_date` for callers who want
incremental pulls, even though the sync does not use them.

The element names follow the community
[Eventor OpenAPI spec](https://github.com/orienteering-oss/eventor-api-openapi-spec) and its
`schema.xsd`; the test fixtures in `tests/fixtures/` are sanitised documents built from that XSD.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

No test touches the network: HTTP is mocked with `respx` and a guard in `tests/conftest.py`
refuses real socket connections.

`scripts/probe_entries.py` answers the "do non-member entrants come back with an email?"
question empirically for your instance and key, printing counts and element structure only:

```bash
uv run python scripts/probe_entries.py --months 6 --max-events 3 --starts
```

## Licence

[MIT](LICENSE). Not affiliated with Eventor, Orienteering Australia or Mailchimp.
