from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CuratorScores:
    data_completeness_score: float
    prompt_completeness_score: float
    dpo_readiness_score: float


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def calculate_prompt_completeness_score(prompt_text: Optional[str]) -> float:
    return 1.0 if _present(prompt_text) else 0.0


def calculate_dpo_readiness_score(
    prompt_text: Optional[str],
    preferred_output: Optional[str],
    dispreferred_output: Optional[str],
) -> float:
    if _present(prompt_text) and _present(preferred_output) and _present(dispreferred_output):
        return 1.0
    return 0.0


def calculate_data_completeness_score(
    request_id: Optional[str],
    send_to_curator: Optional[bool],
    prompt_text: Optional[str],
    preferred_output: Optional[str],
    dispreferred_output: Optional[str],
) -> float:
    required = [
        _present(request_id),
        send_to_curator is True,
        _present(prompt_text),
        _present(preferred_output),
        _present(dispreferred_output),
    ]
    return round(sum(required) / len(required), 4)


def calculate_curator_scores(
    request_id: Optional[str],
    send_to_curator: Optional[bool],
    prompt_text: Optional[str],
    preferred_output: Optional[str],
    dispreferred_output: Optional[str],
) -> CuratorScores:
    return CuratorScores(
        data_completeness_score=calculate_data_completeness_score(
            request_id=request_id,
            send_to_curator=send_to_curator,
            prompt_text=prompt_text,
            preferred_output=preferred_output,
            dispreferred_output=dispreferred_output,
        ),
        prompt_completeness_score=calculate_prompt_completeness_score(prompt_text),
        dpo_readiness_score=calculate_dpo_readiness_score(
            prompt_text=prompt_text,
            preferred_output=preferred_output,
            dispreferred_output=dispreferred_output,
        ),
    )
