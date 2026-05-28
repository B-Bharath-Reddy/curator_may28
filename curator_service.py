from __future__ import annotations

import asyncio
import time
import logging
from typing import Any, Dict, List, Optional

from opentelemetry.trace import Status, StatusCode

from app.curator_models import (
    CuratorFeedbackResponse,
    CuratorScores,
    ImpressionSet,
    ReadinessFlags,
    ReflectorRow,
    TrainingExample,
)
from app.curator_metrics import calculate_curator_scores
from app.observability import get_tracer
from app.curator_output_store import curator_output_store

logger = logging.getLogger(__name__)


# Helpers

def _derive_reward_label(score: Optional[float]) -> str:
    if score is None:
        return "unknown"
    if score >= 0.6:
        return "good"
    if score >= 0.0:
        return "acceptable"
    return "bad"


def _extract_issue_codes(issues: Any) -> List[str]:
    if not issues or not isinstance(issues, list):
        return []
    return [i.get("code", "") for i in issues if isinstance(i, dict) and i.get("code")]


def _as_dict(value: Any) -> Dict:
    return value if isinstance(value, dict) else {}


def _extract_generator_text_with_source(row: ReflectorRow) -> tuple[Optional[str], Optional[str], bool]:
    """
    Generator text with fallback chain:
    1. generated_impression   — BQ flat column
    2. input_payload_json.generator_response.output.impression_text
    3. input_payload_json.impression_text
    4. impression_text        — BQ flat column (fallback)
    """
    # 1. BQ flat column
    if row.generated_impression:
        return row.generated_impression, "generated_impression", False

    payload = _as_dict(row.input_payload_json)
    if isinstance(payload, dict):
        # 2. generator_response.output.impression_text
        gen_resp = _as_dict(payload.get("generator_response"))
        output   = _as_dict(gen_resp.get("output"))
        text     = output.get("impression_text")
        if text:
            return text, "input_payload_json.generator_response.output.impression_text", True

        # 3. top-level impression_text in payload
        text = payload.get("impression_text")
        if text:
            return text, "input_payload_json.impression_text", True

    # 4. BQ flat impression_text fallback
    if row.impression_text:
        return row.impression_text, "impression_text", True

    return None, None, False


def _extract_generator_text(row: ReflectorRow) -> Optional[str]:
    text, _, _ = _extract_generator_text_with_source(row)
    return text


def _extract_prompt_text(payload: Any) -> Optional[str]:
    """
    Prompt text fallback chain for the full-payload phase:
    1. input_payload_json.prompt_text
    2. input_payload_json.generator_request.prompt_text
    3. input_payload_json.prompt
    4. input_payload_json.generator_request.prompt
    """
    payload = _as_dict(payload)
    for key in ("prompt_text", "prompt"):
        text = payload.get(key)
        if isinstance(text, str) and text.strip():
            return text

    gen_req = _as_dict(payload.get("generator_request"))
    for key in ("prompt_text", "prompt"):
        text = gen_req.get(key)
        if isinstance(text, str) and text.strip():
            return text

    return None


def _extract_case(payload: Any) -> Dict:
    """
    Extract case/exam with fallback:
    1. input_payload_json.case
    2. input_payload_json.generator_request.case
    """
    if not isinstance(payload, dict):
        return {}
    case = payload.get("case")
    if isinstance(case, dict) and case:
        return case
    gen_req = _as_dict(payload.get("generator_request"))
    return gen_req.get("case") or {}


def _extract_inputs(payload: Any) -> Dict:
    """
    Extract inputs object with fallback:
    1. input_payload_json.inputs
    2. input_payload_json.generator_request.inputs

    Contains structured_findings and findings_text when stored by Reflector.
    Not always present — only available in newer Reflector rows.
    """
    if not isinstance(payload, dict):
        return {}
    inputs = payload.get("inputs")
    if isinstance(inputs, dict) and inputs:
        return inputs
    gen_req = _as_dict(payload.get("generator_request"))
    return gen_req.get("inputs") or {}


# Impression normalization

def normalize_impressions(row: ReflectorRow) -> ImpressionSet:
    """Collect all 3 impression sources as-is for traceability."""
    return ImpressionSet(
        generated_impression=_extract_generator_text(row),
        final_impression=row.final_impression,
        human_impression=row.human_impression,
    )


#  Readiness assessment

def assess_readiness(
    impressions: ImpressionSet,
    prompt_text: Optional[str],
) -> ReadinessFlags:
    """
    Report what is available and what is still missing.
    For the full-payload phase, DPO-ready means:
    prompt_text + human_impression + generated_impression.
    """
    missing = []

    has_generated_output = bool(impressions.generated_impression)
    if not has_generated_output:
        missing.append("generated_impression")

    has_human_output = bool(impressions.human_impression)
    if not has_human_output:
        missing.append("human_impression")

    has_prompt = bool(prompt_text)
    if not has_prompt:
        missing.append("prompt_text")

    has_dpo_pair = has_generated_output and has_human_output
    is_dpo_ready = has_dpo_pair and has_prompt

    return ReadinessFlags(
        has_prompt=has_prompt,
        has_generated_output=has_generated_output,
        has_human_output=has_human_output,
        has_dpo_pair=has_dpo_pair,
        is_dpo_ready=is_dpo_ready,
        missing_fields=missing,
    )


# Training example

def build_training_example(
    row: ReflectorRow,
    impressions: ImpressionSet,
    reward_score: Optional[float],
    reward_label: str,
    issue_codes: List[str],
    payload: Dict,
    prompt_text: Optional[str],
    findings_text: Optional[str],
    structured_findings: Optional[List],
) -> TrainingExample:
    """
    Builds DPO training example.

    Confirmed by manager:
      preferred_output  = human_impression  (radiologist finalized)
      dispreferred      = generated_impression (model output)

    prompt_input captures the exact prompt_text when supplied by upstream.
    findings_text and structured_findings remain useful audit context, but DPO
    readiness is gated by prompt_text + chosen/rejected outputs.
    """
    case = _extract_case(payload)
    exam = case.get("exam") or {} if isinstance(case, dict) else {}

    preferred_output    = impressions.human_impression or ""
    dispreferred_output = impressions.generated_impression or ""

    return TrainingExample(
        prompt_input={
            # Available now
            "request_id":         row.request_id,
            "modality":           exam.get("modality"),
            "body_part":          exam.get("body_part"),
            "procedure_code":     exam.get("procedure_code"),
            "scope_key":          row.actual_scope_key,
            "clinical_indication": case.get("clinical_indication"),
            "history":            case.get("history"),

            # Populated when input_payload_json.inputs exists (newer Reflector rows)
            "findings_text":      findings_text,
            "structured_findings": structured_findings,

            "prompt_text":        prompt_text,
        },
        preferred_output=preferred_output,
        dispreferred_output=dispreferred_output or None,
        metadata={
            "tenant_id":    row.tenant_id,
            "site_id":      row.site_id,
            "decision":     row.decision,
            "reward_score": reward_score,
            "reward_label": reward_label,
            "issue_codes":  issue_codes,
            "actual_route": row.actual_route,
            "scope_key":    row.actual_scope_key,
        },
    )


# Main service

async def curate_feedback(
    row: ReflectorRow,
    store_output: bool = False,
) -> CuratorFeedbackResponse:
    """
    Curator pipeline:
    1. Check send_to_curator flag — skip if False
    2. Extract prompt_text and inputs if available
    3. Normalize all 3 impressions and generator fallback source
    4. Preserve upstream reward + issues from Reflector
    5. Assess readiness
    6. Build TrainingExample only when is_dpo_ready is True
       - preferred_output  = human_impression  (confirmed by manager)
       - dispreferred      = generated_impression
       - DPO-ready requires prompt_text + preferred + dispreferred
    7. Optionally store response in BigQuery for request-id production flow
    8. Return response
    """
    tracer = get_tracer()
    with tracer.start_as_current_span("curator.prepare_dpo_dataset") as span:
        started = time.perf_counter()
        span.set_attribute("curator.request_id",       row.request_id)
        span.set_attribute("curator.tenant_id",        row.tenant_id or "")
        span.set_attribute("curator.site_id",          row.site_id or "")
        span.set_attribute("curator.decision",         row.decision or "")
        span.set_attribute("curator.actual_route",     row.actual_route or "")
        span.set_attribute("curator.expected_route",   row.expected_route or "")
        span.set_attribute("curator.actual_scope_key", row.actual_scope_key or "")
        span.set_attribute("curator.expected_scope_key", row.expected_scope_key or "")

        try:
            payload    = row.input_payload_json or {}
            issues_raw = row.issues_json or []
            reward     = payload.get("reward_signal") or {} if isinstance(payload, dict) else {}

            # Step 1: Check send_to_curator flag
            # Reflector sets send_to_curator=True only for rows worth training on.
            # Skip rows that Reflector did not flag.
            if not reward.get("send_to_curator"):
                logger.info(
                    "Skipping — send_to_curator=False. request_id=%s",
                    row.request_id,
                )
                span.set_attribute("curator.skipped", True)
                span.set_attribute("curator.skip_reason", "send_to_curator=False")
                span.set_status(Status(StatusCode.OK))
                return CuratorFeedbackResponse(
                    request_id=row.request_id,
                    tenant_id=row.tenant_id,
                    decision=row.decision,
                    impressions=ImpressionSet(),
                    readiness=ReadinessFlags(
                        missing_fields=["send_to_curator=False — row skipped"]
                    ),
                    training_example=None,
                    provenance={},
                    trace={
                        "skipped":     True,
                        "skip_reason": "send_to_curator=False",
                    },
                )

            reward_score  = reward.get("score")
            reward_label  = _derive_reward_label(reward_score)
            reward_reason = reward.get("reason")
            issue_codes   = _extract_issue_codes(issues_raw)

            span.set_attribute("curator.send_to_curator", True)

            # Step 2: Extract prompt and inputs if available
            inputs       = _extract_inputs(payload)
            source_text  = inputs.get("source_text") or {} if isinstance(inputs, dict) else {}
            obs_payload  = inputs.get("observation_payload") or {} if isinstance(inputs, dict) else {}
            findings_text        = source_text.get("findings_text")
            structured_findings  = obs_payload.get("structured_findings")
            prompt_text          = _extract_prompt_text(payload)

            span.set_attribute("curator.has_inputs",              bool(inputs))
            span.set_attribute("curator.has_findings_text",       bool(findings_text))
            span.set_attribute("curator.has_structured_findings", bool(structured_findings))
            span.set_attribute("curator.has_prompt",              bool(prompt_text))

            with tracer.start_as_current_span("curator.extract_prompt") as prompt_span:
                prompt_span.set_attribute("curator.prompt_present", bool(prompt_text))
                prompt_span.set_attribute(
                    "curator.prompt_length",
                    len(prompt_text) if prompt_text else 0,
                )
                prompt_span.set_status(Status(StatusCode.OK))

            #   Step 3: Normalize impressions
            with tracer.start_as_current_span("curator.normalize_feedback") as normalize_span:
                generated_text, generator_output_source, fallback_used = (
                    _extract_generator_text_with_source(row)
                )
                impressions = ImpressionSet(
                    generated_impression=generated_text,
                    final_impression=row.final_impression,
                    human_impression=row.human_impression,
                )
                normalize_span.set_attribute(
                    "curator.generated_impression_present",
                    bool(impressions.generated_impression),
                )
                normalize_span.set_attribute(
                    "curator.generator_output_source",
                    generator_output_source or "",
                )
                normalize_span.set_attribute("curator.fallback_used", fallback_used)
                normalize_span.set_attribute(
                    "curator.final_impression_present",
                    bool(impressions.final_impression),
                )
                normalize_span.set_attribute(
                    "curator.human_impression_present",
                    bool(impressions.human_impression),
                )
                normalize_span.set_status(Status(StatusCode.OK))

            # Step 4: Assess readiness
            with tracer.start_as_current_span("curator.assess_readiness") as readiness_span:
                readiness = assess_readiness(impressions, prompt_text)
                readiness_span.set_attribute(
                    "curator.has_generated_output", readiness.has_generated_output
                )
                readiness_span.set_attribute(
                    "curator.has_human_output",
                    readiness.has_human_output,
                )
                readiness_span.set_attribute("curator.has_prompt", readiness.has_prompt)
                readiness_span.set_attribute("curator.has_dpo_pair",       readiness.has_dpo_pair)
                readiness_span.set_attribute("curator.is_dpo_ready", readiness.is_dpo_ready)
                readiness_span.set_attribute(
                    "curator.missing_fields_count", len(readiness.missing_fields)
                )
                readiness_span.set_status(Status(StatusCode.OK))

            # Step 5: Build training example
            training_example = None
            with tracer.start_as_current_span("curator.build_training_example") as training_span:
                if readiness.is_dpo_ready:
                    training_example = build_training_example(
                        row=row,
                        impressions=impressions,
                        reward_score=reward_score,
                        reward_label=reward_label,
                        issue_codes=issue_codes,
                        payload=payload,
                        prompt_text=prompt_text,
                        findings_text=findings_text,
                        structured_findings=structured_findings,
                    )
                training_span.set_attribute(
                    "curator.has_training_example", training_example is not None
                )
                training_span.set_status(Status(StatusCode.OK))

            with tracer.start_as_current_span("curator.score_record") as score_span:
                scores = calculate_curator_scores(
                    request_id=row.request_id,
                    send_to_curator=reward.get("send_to_curator"),
                    prompt_text=prompt_text,
                    preferred_output=impressions.human_impression,
                    dispreferred_output=impressions.generated_impression,
                )
                score_span.set_attribute(
                    "curator.data_completeness_score",
                    scores.data_completeness_score,
                )
                score_span.set_attribute(
                    "curator.prompt_completeness_score",
                    scores.prompt_completeness_score,
                )
                score_span.set_attribute(
                    "curator.dpo_readiness_score",
                    scores.dpo_readiness_score,
                )
                score_span.set_status(Status(StatusCode.OK))

            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

            if readiness.missing_fields:
                logger.info(
                    "Curator readiness incomplete. request_id=%s missing=%s",
                    row.request_id, readiness.missing_fields,
                )

            span.set_attribute("curator.issue_count",     len(issue_codes))
            span.set_attribute("curator.reward_label",    reward_label)
            span.set_attribute("curator.has_input_payload", bool(payload))
            span.set_attribute("curator.has_generated_output", readiness.has_generated_output)
            span.set_attribute("curator.has_human_output", readiness.has_human_output)
            span.set_attribute("curator.has_dpo_pair",        readiness.has_dpo_pair)
            span.set_attribute("curator.is_dpo_ready",        readiness.is_dpo_ready)
            span.set_attribute("curator.has_training_example", training_example is not None)
            span.set_attribute("curator.generator_output_source", generator_output_source or "")
            span.set_attribute("curator.fallback_used", fallback_used)
            span.set_attribute("curator.data_completeness_score", scores.data_completeness_score)
            span.set_attribute("curator.prompt_completeness_score", scores.prompt_completeness_score)
            span.set_attribute("curator.dpo_readiness_score", scores.dpo_readiness_score)
            span.set_attribute("curator.latency_ms",          elapsed_ms)
            if reward_score is not None:
                span.set_attribute("curator.reward_score", reward_score)
            span.set_status(Status(StatusCode.OK))

            response = CuratorFeedbackResponse(
                request_id=row.request_id,
                tenant_id=row.tenant_id,
                decision=row.decision,
                impressions=impressions,
                reward_score=reward_score,
                reward_label=reward_label,
                reward_reason=reward_reason,
                issue_codes=issue_codes,
                prompt_text=prompt_text,
                scores=CuratorScores(
                    data_completeness_score=scores.data_completeness_score,
                    prompt_completeness_score=scores.prompt_completeness_score,
                    dpo_readiness_score=scores.dpo_readiness_score,
                ),
                training_example=training_example,
                readiness=readiness,
                provenance={
                    "actual_route":       row.actual_route,
                    "expected_route":     row.expected_route,
                    "actual_scope_key":   row.actual_scope_key,
                    "expected_scope_key": row.expected_scope_key,
                    "reward_reason":      reward_reason,
                    "send_to_curator":    reward.get("send_to_curator"),
                },
                trace={
                    "latency_ms":               elapsed_ms,
                    "status":                   "ready" if readiness.is_dpo_ready else "incomplete",
                    "has_training_example":     bool(training_example),
                    "generator_output_source":  generator_output_source,
                    "fallback_used":            fallback_used,
                },
            )

            if store_output:
                with tracer.start_as_current_span("curator.store_response") as store_span:
                    stored = await asyncio.to_thread(curator_output_store.store, response, row)
                    store_span.set_attribute("curator.output_stored", stored)
                    if not stored:
                        store_span.set_status(
                            Status(StatusCode.ERROR, "Curator output insert failed")
                        )
                        raise RuntimeError("Failed to store curator output in BigQuery")
                    store_span.set_status(Status(StatusCode.OK))

            return response

        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            raise
