# Creator Outreach Automation (Python-First)

This project implements the workflow defined in ccajordan.txt using Python as the only execution engine.

## What this implementation does

- Loads two Google Sheets tabs:
  - Creators sheet with fields:
    - creator_name
    - creator_niche
    - creator_platform
    - creator_followers
    - instagram_profile
    - tiktok_profile
    - youtube_profile
  - RSS sheet with field:
    - feed_url
- Loads creator-specific outreach templates from a Google Sheet block format:
  - Creator name row
  - `Subject, Copy` header row
  - One or more template rows per creator
  - Uses the best matching template for a lead when keywords match; otherwise falls back to AI drafting
- Runs the exact sequence:
  - For each creator, iterate every RSS feed URL.
  - Fetch RSS XML, extract article links, and pull article content for lead enrichment.
  - Match each lead niche against creator_niche using confidence-based semantic matching.
  - If matched, query Apollo in two steps with throttling:
    - Step 1: find brand-related people via mixed_people/api_search.
    - Step 2: resolve each candidate email via people/match.
  - Enforce strict valid-email-only policy (no guessing).
  - Generate personalized email draft.
  - Send preview to Slack for human review.
  - Apply revision instructions strictly in a loop until APPROVE or REJECT/timeout.
  - Send approved email via SMTP.
  - Continue until all leads, all feeds, and all creators are done.
- Persists run and cursor state in SQLite for resume capability.
- Prevents duplicate sends by creator_name + lead_email.

## Project structure

- outreach_engine/config.py: environment settings loader
- outreach_engine/models.py: typed data models
- outreach_engine/sheets_loader.py: Google Sheets ingestion
- outreach_engine/rss_parser.py: RSS fetch + XML parsing
- outreach_engine/niche_matcher.py: AI + fallback niche matching
- outreach_engine/apollo_client.py: Apollo search + rate limiting + retries
- outreach_engine/email_generator.py: draft and strict revision generation
- outreach_engine/slack_approval.py: Slack approval/revision loop
- outreach_engine/smtp_sender.py: SMTP delivery
- outreach_engine/state_store.py: SQLite run/cursor/sent persistence
- outreach_engine/workflow.py: end-to-end orchestrator
- outreach_engine/cli.py: CLI entrypoint
- outreach_engine/api.py: optional HTTP trigger API

## Quick start

1. Install dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

1. Configure environment

- Copy .env.example to .env
- Fill all required variables.
- Ensure Google service account has access to the target spreadsheet.
- If templates are in a separate spreadsheet, set `EMAIL_FORMATS_SPREADSHEET_ID` and `EMAIL_FORMATS_WORKSHEET_GID`.

1. Run once from CLI

```powershell
python -m outreach_engine run
```

1. Start API server (optional)

```powershell
python -m outreach_engine serve
```

1. Check run status

```powershell
python -m outreach_engine status <run_id>
```

## Slack approval commands

In the approval thread:

- APPROVE: sends the current draft
- REJECT: skips the contact
- Any other message: treated as strict revision instructions, and a new preview is posted

## Python-only operations

The full workflow runs inside Python only. Feed parsing, niche matching, Apollo lookups, Slack approval loops, and SMTP sending are all executed by `outreach_engine/workflow.py`.

The API server exists for programmatic triggers, but it still executes the same Python workflow.

## Notes

- Apollo endpoint payloads can vary by account tier; adjust the request mapping in outreach_engine/apollo_client.py if your tenant uses different fields.
- Default Apollo base URL should be set to <https://api.apollo.io/api/v1> for the two-step flow endpoints.
- Apollo flow is docs-aligned: People API Search discovers candidate IDs, then People Enrichment/Bulk People Enrichment resolves contact emails.
- OpenAI is used for niche matching and email generation/revision. Fallback logic is included for resilience.
- Reply monitoring uses IMAP. For Gmail, enable IMAP in mailbox settings and use an App Password (not your account password).
- `SMTP_ACCOUNTS` supports optional IMAP-specific fields per account: `imap_host`, `imap_port`, `imap_username`, `imap_password`.
