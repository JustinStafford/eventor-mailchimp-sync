# Running the sync from a private repository

The public repository is the tool. Your club's credentials, event-series
mappings and run reports belong in a **private** repository, because on a
public repository every workflow log and artifact is readable by anyone, and
the sync prints names and email addresses in its diff and report.

1. Create a new private repository on GitHub (any name, for example
   `club-mailchimp-sync`). Do not fork this one: forks of public repositories
   cannot be made private.
2. Add [`sync.yml`](sync.yml) from this directory as
   `.github/workflows/sync.yml`, and optionally a `config.toml` with series
   tags (see `config.example.toml` in the tool's repository).
3. Under *Settings > Secrets and variables > Actions* add the secrets
   `EVENTOR_API_KEY`, `MAILCHIMP_API_KEY` and `MAILCHIMP_LIST_ID`, plus any
   variables you want (`SYNC_PERSONS_CONTACT_INDEX`, `SYNC_REDACT`, ...).
4. Set the variable `TOOL_REF` to a tag or commit of the tool so a future change
   here cannot alter your sync without you choosing to update. `main` is used
   when it is unset.
5. Open *Actions*, run *Sync Mailchimp audience* by hand with *apply* unticked,
   and read the report artifact. Then leave the schedule to it: 16:07 UTC daily
   (02:07 Sydney time in winter), editable in the `cron` line.

The workflow installs the tool with `uvx` directly from GitHub on every run, so
there is nothing to update in your private repository when the tool changes:
move `TOOL_REF` when you want the new version.
