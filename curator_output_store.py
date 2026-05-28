from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.observability import get_tracer
from opentelemetry.trace import Status, StatusCode

from google.cloud import bigquery

from app.config import settings
from app.curator_models import CuratorFeedbackResponse, ReflectorRow

logger = logging.getLogger(__name__)


class CuratorOutputStore:
    """
    Writes lean Curator output rows to BigQuery.

    The table stores a few filterable columns, the clean dpo_record when ready,
    and the full curator_response JSON for audit/debug/reporting.
    """

    def __init__(self):
        self.client = None
        self.table = None
        self._initialize()

    def _initialize(self) -> None:
        if not settings.bigquery_table_id:
            logger.warning(
                "CuratorOutputStore: BIGQUERY_TABLE_ID not set. Output writes are disabled."
            )
            return

        try:
            self.client = bigquery.Client(project=settings.google_cloud_project)
            self.table = self.client.get_table(settings.bigquery_table_id)
            logger.info(
                "CuratorOutputStore initialized. table=%s",
                settings.bigquery_table_id,
            )
        except Exception:
            logger.exception("CuratorOutputStore initialization failed")
            self.client = None
            self.table = None

    def is_available(self) -> bool:
        return self.client is not None and self.table is not None

    def store(self, response: CuratorFeedbackResponse, source_row: ReflectorRow) -> bool:
        if not self.is_available():
            logger.error(
                "CuratorOutputStore is not available. Check BIGQUERY_TABLE_ID and credentials."
            )
            return False
        tracer = get_tracer()
        with tracer.start_as_current_span("curator.store_response") as span:
            span.set_attribute("curator.request_id", response.request_id)
            span.set_attribute("curator.is_dpo_ready", response.readiness.is_dpo_ready)
            span.set_attribute("curator.has_dpo_pair", response.readiness.has_dpo_pair)

        try:
            row = self._build_row(response, source_row)
            errors = self.client.insert_rows_json(settings.bigquery_table_id, [row])
            if errors:
                logger.error("BQ insert errors [%s]: %s", response.request_id, errors)
                span.set_status(Status(StatusCode.ERROR, str(errors)))
                return False
            span.set_attribute("curator.stored", True)
            span.set_status(Status(StatusCode.OK))
            logger.info("Stored curator response [%s]", response.request_id)
            return True

        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.exception("Failed to store [%s]", response.request_id)
            return False



    def _build_row(
            self,
            response: CuratorFeedbackResponse,
            source_row: ReflectorRow,
    ) -> dict:
        training = response.training_example
        readiness = response.readiness
        trace = response.trace or {}

        dpo_record = None
        if readiness.is_dpo_ready and training:
            dpo_record = {
                "prompt": training.prompt_input.get("prompt_text"),
                "chosen": training.preferred_output,
                "rejected": training.dispreferred_output
            }

        return {
            "request_id": response.request_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "event_id": source_row.event_id,
            "tenant_id": response.tenant_id,
            "site_id": source_row.site_id,
            "decision": response.decision,

            "has_dpo_pair": readiness.has_dpo_pair,
            "is_dpo_ready": readiness.is_dpo_ready,
            "missing_fields": json.dumps(readiness.missing_fields),

            "reward_score": response.reward_score,
            "reward_label": response.reward_label,

            "latency_ms": trace.get("latency_ms"),
            "dpo_record": json.dumps(dpo_record) if dpo_record else None,
            "curator_response": json.dumps(response.model_dump(mode="json")),
        }


curator_output_store = CuratorOutputStore()
