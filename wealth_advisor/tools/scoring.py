"""Aggregate findings into a single explainable risk score.

score = 1 - prod(1 - w_severity * strength_i)

A noisy-OR: each finding independently "contributes" risk, the result is
bounded to [0, 1], saturates smoothly (ten medium findings do not exceed one
critical by a silly margin), and is order-independent. Suppressed/dismissed
findings are excluded.
"""

from __future__ import annotations

from typing import Optional

from ..models import Anomaly, RiskScore, Severity
from .base import FunctionTool, ParamSpec, ToolSpec

SEVERITY_WEIGHT = {Severity.LOW: 0.05, Severity.MEDIUM: 0.12, Severity.HIGH: 0.25, Severity.CRITICAL: 0.45}
BANDS = [(0.20, "low"), (0.45, "moderate"), (0.70, "elevated"), (1.01, "high")]
BAND_ORDER = ["low", "moderate", "elevated", "high"]


def band_for(value: float) -> str:
    return next(name for limit, name in BANDS if value < limit)


def compute_risk_score(anomalies: list, previous: Optional[dict] = None) -> RiskScore:
    active: list[Anomaly] = [a for a in anomalies if a.counts_toward_risk]
    remaining = 1.0
    contributions = []
    for a in active:
        c = SEVERITY_WEIGHT[a.severity] * max(0.5, min(1.0, a.score))
        remaining *= 1 - c
        contributions.append((c, a))
    value = round(1 - remaining, 4)
    contributions.sort(key=lambda x: x[0], reverse=True)
    drivers = [f"{a.title} ({a.severity.value})" for _, a in contributions[:3]]
    prev_val = previous.get("value") if previous else None
    return RiskScore(
        value=value,
        band=band_for(value),
        drivers=drivers,
        previous_value=prev_val,
        previous_band=previous.get("band") if previous else None,
        delta=round(value - prev_val, 4) if prev_val is not None else None,
    )


RISK_SCORE_TOOL = FunctionTool(
    ToolSpec("risk.score", "Aggregate findings into a 0-1 risk score with band and top drivers.",
             {"anomalies": ParamSpec(list), "previous": ParamSpec(dict, required=False)}),
    compute_risk_score,
)
