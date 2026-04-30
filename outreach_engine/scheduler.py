import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Settings
from .follow_up import FollowUpRunner
from .reply_monitor import ReplyMonitor
from .slack_bot_listener import SlackBotListener

logger = logging.getLogger(__name__)


async def run_outreach_periodically(settings: Settings):
    """Background task to run full outreach pipeline on a schedule."""
    from .state_store import StateStore
    from .workflow import OutreachWorkflow

    interval = settings.outreach_run_interval_hours * 3600
    state = StateStore(settings.state_db_path)

    while True:
        try:
            # Check if a run is already in progress (overlap protection)
            existing = state.get_json("auto_run_active")
            if existing and existing.get("active"):
                logger.info("Skipping scheduled outreach run — previous run still active")
            else:
                run_id = f"auto-{uuid.uuid4()}"
                state.set_json("auto_run_active", {"active": True, "run_id": run_id})
                logger.info("Starting scheduled outreach run: %s", run_id)
                try:
                    workflow = OutreachWorkflow(settings)
                    await asyncio.to_thread(workflow.run, run_id)
                    logger.info("Scheduled outreach run completed: %s", run_id)
                finally:
                    state.set_json("auto_run_active", {"active": False, "run_id": run_id})
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in scheduled outreach run: %s", e)
            # Clear active flag on error so next run can proceed
            try:
                state.set_json("auto_run_active", {"active": False})
            except Exception:
                pass

        await asyncio.sleep(interval)


async def run_followups_periodically(settings: Settings):
    """Background task to run followups periodically (e.g., 1x a day)."""
    runner = FollowUpRunner(settings)
    
    # Check every hour. The logic only sends if follow_up_due_at <= now,
    # so running often is safe.
    POLL_INTERVAL = 3600 
    
    while True:
        try:
            logger.info("Executing periodic follow-up runner...")
            await asyncio.to_thread(runner.run)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in background follow-up runner: %s", e)
            
        await asyncio.sleep(POLL_INTERVAL)


async def run_slack_listener_periodically(settings: Settings):
    """Background task to run slack listener polling."""
    listener = SlackBotListener(settings)
    
    POLL_INTERVAL = 15  # Poll every 15 seconds
    
    while True:
        try:
            await asyncio.to_thread(listener.run_once)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in background slack listener: %s", e)
            
        await asyncio.sleep(POLL_INTERVAL)


async def run_reply_monitor_periodically(settings: Settings):
    """Background task to monitor IMAP inboxes for lead replies."""
    monitor = ReplyMonitor(settings)
    interval = settings.reply_check_interval_seconds

    while True:
        try:
            logger.info("Checking inboxes for lead replies...")
            await asyncio.to_thread(monitor.check_all_accounts)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Error in reply monitor: %s", e)

        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager to manage background tasks linked to the FastAPI app lifecycle."""
    settings = Settings.from_env()
    
    logger.info("Starting background tasks...")
    logger.info(
        "  Outreach run: every %dh | Follow-up check: every 1h | "
        "Reply monitor: every %ds | Slack listener: every 15s",
        settings.outreach_run_interval_hours,
        settings.reply_check_interval_seconds,
    )
    task1 = asyncio.create_task(run_followups_periodically(settings))
    task2 = asyncio.create_task(run_slack_listener_periodically(settings))
    task3 = asyncio.create_task(run_outreach_periodically(settings))
    task4 = asyncio.create_task(run_reply_monitor_periodically(settings))
    
    yield
    
    logger.info("Shutting down background tasks...")
    task1.cancel()
    task2.cancel()
    task3.cancel()
    task4.cancel()
    
    # Wait for tasks to finish cancelling
    try:
        await asyncio.gather(task1, task2, task3, task4, return_exceptions=True)
    except Exception:
        logger.error("Error during shutdown of background tasks")
