from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


class ReflectorRow(BaseModel):
    """
    Maps directly to a row from reflection_events BigQuery table.
    This is the Curator input — fetched by request_id.
    """
    request_id:           str
    event_id:             Optional[str]  = None
    tenant_id:            Optional[str]  = None
    site_id:              Optional[str]  = None
    decision:             Optional[str]  = None
    expected_route:       Optional[str]  = None
    actual_route:         Optional[str]  = None
    expected_scope_key:   Optional[str]  = None
    actual_scope_key:     Optional[str]  = None
    impression_text:      Optional[str]  = None
    generated_impression: Optional[str]  = None
    final_impression:     Optional[str]  = None
    human_impression:     Optional[str]  = None   # radiologist finalized — confirmed preferred_output
    input_payload_json:   Optional[Any]  = None   # parsed dict after fetch
    issues_json:          Optional[Any]  = None   # parsed list after fetch
    has_embedding:        Optional[bool] = None

    @field_validator("input_payload_json", "issues_json", mode="before")
    @classmethod
    def parse_json_columns(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                return json.loads(stripped)
            except Exception:
                return None
        return value


class TrainingExample(BaseModel):
    """
    DPO training example.

    prompt_input     -> clinical context plus prompt_text when supplied by upstream.

    preferred_output → human_impression (radiologist finalized — confirmed by manager)
    dispreferred     → generated_impression (DPO rejected side)
    """
    prompt_input:        Dict[str, Any]
    preferred_output:    str
    dispreferred_output: Optional[str] = None
    metadata:            Dict[str, Any] = Field(default_factory=dict)


class ImpressionSet(BaseModel):
    """All 3 impression sources preserved as-is for traceability."""
    generated_impression: Optional[str] = None
    final_impression:     Optional[str] = None
    human_impression:     Optional[str] = None


class ReadinessFlags(BaseModel):
    """Reports what data is available and what is still missing."""
    has_prompt:           bool      = False
    has_generated_output: bool      = False
    has_human_output:     bool      = False
    has_dpo_pair:         bool      = False
    is_dpo_ready:         bool      = False
    missing_fields:       List[str] = Field(default_factory=list)


class CuratorScores(BaseModel):
    """Simple quality/readiness scores for reporting."""
    data_completeness_score:   float = 0.0
    prompt_completeness_score: float = 0.0
    dpo_readiness_score:       float = 0.0


class CuratorFeedbackResponse(BaseModel):
    request_id:       str
    tenant_id:        Optional[str]   = None
    decision:         Optional[str]   = None

    # All 3 impressions preserved
    impressions:      ImpressionSet

    # Upstream reward from Reflector — not recomputed
    reward_score:     Optional[float] = None
    reward_label:     Optional[str]   = None
    reward_reason:    Optional[str]   = None
    issue_codes:      List[str]       = Field(default_factory=list)

    prompt_text:      Optional[str]   = None
    scores:           CuratorScores   = Field(default_factory=CuratorScores)

    # DPO training example
    # Built only when prompt_text + human_impression + generated_impression exist.
    training_example: Optional[TrainingExample] = None

    # Readiness — tells downstream what is still missing
    readiness:        ReadinessFlags

    provenance:       Dict[str, Any]  = Field(default_factory=dict)
    trace:            Dict[str, Any]  = Field(default_factory=dict)


class CuratorReviewRequest(BaseModel):
    """
    What Reflector POSTs to curator_callback_url.
    Curator fetches full payload from BigQuery using this request_id.
    """
    request_id: str
