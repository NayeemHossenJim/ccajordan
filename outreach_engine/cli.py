from __future__ import annotations

import argparse
import os
import uuid
from pathlib import Path

import uvicorn

from .config import Settings
from .logging_utils import configure_logging
from .state_store import StateStore
from .workflow import OutreachWorkflow


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Creator outreach workflow runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run workflow now")
    run_parser.add_argument("--run-id", dest="run_id", default=None)

    status_parser = subparsers.add_parser("status", help="Read run status")
    status_parser.add_argument("run_id", help="Run identifier")

    subparsers.add_parser("serve", help="Serve HTTP API for n8n trigger")

    subparsers.add_parser("follow-up", help="Check and send due follow-up emails")

    replied_parser = subparsers.add_parser("mark-replied", help="Mark a lead email as replied (cancels pending follow-up)")
    replied_parser.add_argument("email", help="Lead email address that replied")

    brands_parser = subparsers.add_parser("find-brands", help="Find top brands in a niche")
    brands_parser.add_argument("niche", help="Niche to search (e.g., 'kitchen knife')")
    brands_parser.add_argument("--count", type=int, default=10, help="Number of brands to find (default: 10)")
    brands_parser.add_argument("--slack", action="store_true", help="Post results to Slack channel")

    return parser


def _get_state_store() -> StateStore:
    state_db_path = os.getenv("STATE_DB_PATH", str(Path("workflow_state.db")))
    return StateStore(state_db_path)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "status":
        status = _get_state_store().get_run(args.run_id)
        if not status:
            raise SystemExit("Run not found")
        print(status)
        return

    if args.command == "mark-replied":
        count = _get_state_store().mark_replied(args.email)
        if count:
            print(f"Marked {count} pending follow-up(s) as replied for {args.email}")
        else:
            print(f"No pending follow-ups found for {args.email}")
        return

    settings = Settings.from_env()
    configure_logging(settings.log_level)

    if args.command == "run":
        run_id = args.run_id or f"run-{uuid.uuid4()}"
        workflow = OutreachWorkflow(settings)
        workflow.run(run_id=run_id)
        print(run_id)
        return

    if args.command == "serve":
        uvicorn.run(
            "outreach_engine.api:app",
            host=settings.api_host,
            port=settings.api_port,
            reload=False,
        )
        return

    if args.command == "follow-up":
        from .follow_up import FollowUpRunner
        runner = FollowUpRunner(settings)
        result = runner.run()
        print(f"Follow-up results: {result}")
        return

    if args.command == "find-brands":
        from .brand_finder import BrandFinder
        finder = BrandFinder(api_key=settings.openai_api_key, model=settings.openai_model)
        brands = finder.find_brands(niche=args.niche, count=args.count)

        if not brands:
            print(f"No brands found for '{args.niche}'")
            return

        print(f"\nTop {len(brands)} brands in '{args.niche}':\n")
        for i, brand in enumerate(brands, 1):
            print(f"  {i}. {brand['name']} — {brand['website']}")
            if brand.get("description"):
                print(f"     {brand['description']}")
        print()

        if args.slack:
            from .slack_approval import SlackApprover
            approver = SlackApprover(
                bot_token=settings.slack_bot_token,
                channel_id=settings.slack_approval_channel_id,
                timeout_minutes=settings.slack_approval_timeout_minutes,
                poll_interval_seconds=settings.slack_poll_interval_seconds,
            )
            approver.post_brand_results(niche=args.niche, brands=brands)
            print("Results posted to Slack.")
        return


if __name__ == "__main__":
    main()

