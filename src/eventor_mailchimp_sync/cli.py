"""Command-line interface.

Exit codes: 0 success, 1 configuration problem, 2 Eventor API failure,
3 Mailchimp API failure (including any failed write during ``--apply``).
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from eventor_client import EventorError, ResponseCache
from eventor_mailchimp_sync import __version__
from eventor_mailchimp_sync.config import ConfigError, load_config
from eventor_mailchimp_sync.mailchimp import MailchimpError
from eventor_mailchimp_sync.report import write_report
from eventor_mailchimp_sync.sync import (
    make_eventor_client,
    make_mailchimp_client,
    run,
    with_overrides,
)

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_EVENTOR = 2
EXIT_MAILCHIMP = 3

app = typer.Typer(
    help="Keep a Mailchimp audience in sync with a club's Eventor members and entrants.",
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _echo(message: str = "") -> None:
    typer.echo(message)


def _err(message: str) -> None:
    typer.echo(message, err=True)


@app.callback()
def _common(
    env_file: Annotated[
        Path,
        typer.Option("--env-file", help="Load environment variables from this file if it exists."),
    ] = Path(".env"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
    no_progress: Annotated[
        bool, typer.Option("--no-progress", help="Suppress progress messages on stderr.")
    ] = False,
    version: Annotated[bool, typer.Option("--version", help="Print the version and exit.")] = False,
) -> None:
    if version:
        _echo(f"eventor-mailchimp-sync {__version__}")
        raise typer.Exit()
    if env_file.is_file():
        load_dotenv(env_file, override=False)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not verbose and not no_progress:
        # Progress lines from this package only; a dry run can take a few minutes on
        # Eventor's side and would otherwise sit silent the whole time.
        progress = logging.StreamHandler(sys.stderr)
        progress.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        progress.setLevel(logging.INFO)
        for name in ("eventor_mailchimp_sync.sources", "eventor_mailchimp_sync.sync"):
            logger = logging.getLogger(name)
            logger.setLevel(logging.INFO)
            logger.addHandler(progress)
            logger.propagate = False


def _load(config_path: Path | None, *, require_mailchimp: bool = True):
    try:
        return load_config(config_path=config_path, require_mailchimp=require_mailchimp)
    except ConfigError as exc:
        _err(f"configuration error: {exc}")
        raise typer.Exit(EXIT_CONFIG) from exc


@app.command()
def sync(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write changes to Mailchimp. Without it, nothing is written."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Explicitly request a dry run (the default).")
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option(
            "--report",
            help="Path for the JSON run report (default SYNC_REPORT_PATH or report.json).",
        ),
    ] = None,
    config: Annotated[
        Path | None, typer.Option("--config", help="TOML file with event-series tag mappings.")
    ] = None,
    window_months: Annotated[
        int | None, typer.Option("--window-months", min=1, help="Override SYNC_WINDOW_MONTHS.")
    ] = None,
    redact: Annotated[
        bool, typer.Option("--redact", help="Mask emails and names in the diff and report.")
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Do not print the diff.")] = False,
    as_of: Annotated[
        datetime | None,
        typer.Option("--as-of", hidden=True, formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]),
    ] = None,
) -> None:
    """Pull members and entrants from Eventor and reconcile the Mailchimp audience."""
    if apply and dry_run:
        _err("--apply and --dry-run are mutually exclusive")
        raise typer.Exit(EXIT_CONFIG)
    cfg = with_overrides(_load(config), window_months=window_months, report_path=report)
    try:
        result = run(cfg, apply=apply, now=as_of, redact=redact)
    except EventorError as exc:
        _err(f"Eventor API failure: {exc}")
        raise typer.Exit(EXIT_EVENTOR) from exc
    except MailchimpError as exc:
        _err(f"Mailchimp API failure: {exc}")
        raise typer.Exit(EXIT_MAILCHIMP) from exc

    if not quiet:
        _echo(result.diff_text)
    write_report(cfg.report_path, result.report)
    _echo(f"Report written to {cfg.report_path}")
    if not result.ok:
        _err(f"{len(result.api_errors)} change(s) failed to apply; see the report")
        raise typer.Exit(EXIT_MAILCHIMP)


@app.command()
def whoami() -> None:
    """Show which organisation the Eventor API key belongs to."""
    cfg = _load(None, require_mailchimp=False)
    try:
        with make_eventor_client(cfg) as client:
            org = client.organisation_for_api_key()
    except EventorError as exc:
        _err(f"Eventor API failure: {exc}")
        raise typer.Exit(EXIT_EVENTOR) from exc
    _echo(f"{org.name or org.short_name} (organisation #{org.id}) on {cfg.eventor_base_url}")
    _echo(f"Member source: /{cfg.effective_member_source}")


@app.command()
def audience() -> None:
    """Show the Mailchimp audience and which optional merge fields exist."""
    cfg = _load(None)
    try:
        with make_mailchimp_client(cfg) as client:
            info = client.list_info()
            fields = client.merge_fields()
    except MailchimpError as exc:
        _err(f"Mailchimp API failure: {exc}")
        raise typer.Exit(EXIT_MAILCHIMP) from exc
    stats = info.get("stats", {})
    _echo(
        f"{info.get('name')} ({info.get('id')}): {stats.get('member_count', '?')} subscribed, "
        f"{stats.get('unsubscribe_count', '?')} unsubscribed, "
        f"{stats.get('cleaned_count', '?')} cleaned"
    )
    present = {f.tag.upper() for f in fields}
    _echo("Merge fields: " + ", ".join(f"{f.tag} ({f.type})" for f in fields))
    rows = [
        ("club", cfg.merge_club),
        ("membership year", cfg.merge_membership_year),
        ("Eventor person ID", cfg.merge_eventor_id),
    ]
    if cfg.merge_mobile:
        rows.append(("mobile", cfg.merge_mobile))
    for label, tag in rows:
        state = "will be synced" if tag in present else "not in audience; skipped"
        if tag == cfg.merge_mobile and tag in present:
            info = next(f for f in fields if f.tag.upper() == tag)
            if info.type == "phone" and (info.phone_format or "").upper() == "US":
                state = "US phone format; change it to international in Mailchimp"
            elif cfg.phone_country_code:
                state = f"will be synced as +{cfg.phone_country_code}..."
        _echo(f"  {label:<18} {tag:<12} {state}")


@app.command("cache-clear")
def cache_clear() -> None:
    """Delete cached Eventor responses (EVENTOR_CACHE_DIR)."""
    cfg = _load(None, require_mailchimp=False)
    if cfg.eventor_cache_dir is None:
        _echo("EVENTOR_CACHE_DIR is not set; nothing to clear")
        return
    removed = ResponseCache(cfg.eventor_cache_dir, cfg.eventor_cache_ttl_seconds).clear()
    _echo(f"Removed {removed} cached response(s) from {cfg.eventor_cache_dir}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
