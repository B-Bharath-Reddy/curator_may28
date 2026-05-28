from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import settings
from app.curator_models import (
    CuratorReviewRequest,
    CuratorFeedbackResponse,
    ReflectorRow,
)
from app.curator_service import curate_feedback
from app.curator_input_store import bigquery_input_store
from app.curator_output_store import curator_output_store
from app.observability import setup_phoenix



@asynccontextmanager
async def lifespan(app: FastAPI):
    phoenix_enabled = os.getenv("PHOENIX_ENABLED", "false").lower() == "true"

    if phoenix_enabled:
        setup_phoenix(app)

    yield

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Curator Agent — normalizes Reflector events and builds DPO training examples",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {
        "status":                   "ok",
        "app":                      settings.app_name,
        "version":                  settings.app_version,
        "bigquery_input_available": bigquery_input_store.is_available(),
        "bigquery_output_available": curator_output_store.is_available(),
    }


#  PRODUCTION endpoint
# Reflector POSTs { "request_id": "..." } here via curator_callback_url

@app.post("/v1/ace/curator:review-human-feedback", response_model=CuratorFeedbackResponse)
async def review_human_feedback(req: CuratorReviewRequest):
    try:
        row = await bigquery_input_store.fetch_by_request_id(req.request_id)

        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"No data found for request_id={req.request_id}",
            )

        reflector_row = ReflectorRow(**row)

        return await curate_feedback(reflector_row, store_output=True)

    except HTTPException:
        raise
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


#  MANUAL TESTING endpoint
# POST the full ReflectorRow directly — no BigQuery fetch needed
# Use the real BigQuery row JSON you already have for testing

@app.post("/v1/ace/curator:review-human-feedback-direct", response_model=CuratorFeedbackResponse)
async def review_human_feedback_direct(req: ReflectorRow):
    try:
        # Direct endpoint is for local validation with full payload.
        # It bypasses BigQuery input fetch and still exercises the same curator logic.
        return await curate_feedback(req)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
