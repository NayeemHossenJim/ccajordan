from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from .config import Settings
from .logging_utils import configure_logging
from .state_store import StateStore
from .workflow import OutreachWorkflow
from .scheduler import lifespan


settings = Settings.from_env()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)
app = FastAPI(title="Creator Outreach Engine API", version="1.0.0", lifespan=lifespan)


class RunRequest(BaseModel):
    run_id: Optional[str] = None


class RunResponse(BaseModel):
    run_id: str
    status: str
    message: str


def _execute(run_id: str) -> None:
    workflow = OutreachWorkflow(settings)
    workflow.run(run_id)


@app.post("/runs", response_model=RunResponse)
def start_run(payload: RunRequest, background_tasks: BackgroundTasks) -> RunResponse:
    run_id = payload.run_id or f"run-{uuid.uuid4()}"
    background_tasks.add_task(_execute, run_id)
    return RunResponse(run_id=run_id, status="queued", message="Workflow run queued")


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = StateStore(settings.state_db_path).get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
