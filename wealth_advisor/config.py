"""Runtime configuration.

All detection thresholds live here rather than being scattered across detectors,
so a compliance team can tune sensitivity without touching logic. Values can be
overridden through environment variables prefixed with ``WA_``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


@dataclass(frozen=True)
class AnomalyThresholds:
    # Statistical outliers (Iglewicz & Hoaglin modified z-score).
    modified_z: float = 3.5
    modified_z_high: float = 7.0
    min_category_samples: int = 6
    min_outlier_amount: float = 250.0

    # Liquidity-relative large outflow.
    large_outflow_pct_liquid: float = 0.10
    large_outflow_pct_liquid_critical: float = 0.25
    large_outflow_min_amount: float = 5_000.0

    # Structuring: repeated cash deposits just under a reporting threshold.
    structuring_threshold: float = 10_000.0
    structuring_band_pct: float = 0.10
    structuring_min_count: int = 3
    structuring_window_days: int = 7

    # Duplicate charges.
    duplicate_window_minutes: int = 10

    # First-seen counterparty receiving an unusually large amount.
    new_counterparty_lookback_days: int = 30
    new_counterparty_percentile: float = 0.95
    new_counterparty_min_amount: float = 1_000.0

    # Geography.
    unusual_country_min_amount: float = 500.0

    # Velocity burst of outbound transfers.
    velocity_window_hours: int = 48
    velocity_min_count: int = 4
    velocity_channels: tuple[str, ...] = ("wire", "p2p", "ach_out")

    # Cash-flow trends.
    spending_trend_pct: float = 0.25
    spending_trend_min_abs: float = 500.0
    income_drop_pct: float = 0.30
    min_months_for_trend: int = 4

    # Portfolio risk.
    concentration_single: float = 0.20
    concentration_single_high: float = 0.35
    concentration_sector: float = 0.40
    allocation_drift: float = 0.10
    allocation_drift_high: float = 0.20
    min_cash_buffer_months: float = 3.0
    critical_cash_buffer_months: float = 1.0

    # Compliance / profile hygiene.
    kyc_expiry_warning_days: int = 30
    risk_profile_max_age_days: int = 365


# Target strategic allocation and volatility ceiling per stated risk profile.
RISK_PROFILE_TARGETS: dict[str, dict] = {
    "conservative": {"allocation": {"equity": 0.30, "fixed_income": 0.55, "cash": 0.10, "alternative": 0.05},
                     "max_volatility": 0.09},
    "moderate": {"allocation": {"equity": 0.60, "fixed_income": 0.30, "cash": 0.05, "alternative": 0.05},
                 "max_volatility": 0.15},
    "aggressive": {"allocation": {"equity": 0.85, "fixed_income": 0.05, "cash": 0.03, "alternative": 0.07},
                   "max_volatility": 0.25},
}

# Categories that move money between the client's own accounts or into
# investments. Excluded from spend statistics and most anomaly detectors.
NON_SPEND_CATEGORIES: frozenset[str] = frozenset({"investment_transfer", "internal_transfer"})
INCOME_CATEGORIES: frozenset[str] = frozenset({"salary", "pension", "social_security", "freelance",
                                                "dividend", "interest", "rental_income"})


@dataclass(frozen=True)
class Settings:
    data_dir: Path = PROJECT_ROOT / "data"
    state_dir: Path = PROJECT_ROOT / ".state"
    log_dir: Path = PROJECT_ROOT / "logs"
    thresholds: AnomalyThresholds = field(default_factory=AnomalyThresholds)

    # Human-in-the-loop: findings at or above this severity force a review.
    review_min_severity: str = "high"
    # Risk score band that forces a review regardless of individual findings.
    review_min_risk_band: str = "elevated"

    # Resilience.
    tool_max_attempts: int = 3
    tool_base_delay_s: float = 0.2
    tool_max_delay_s: float = 2.0
    breaker_failure_threshold: int = 3
    breaker_reset_s: float = 30.0
    crm_cache_max_age_days: int = 30

    # Optional LLM narrative. Off by default; deterministic template is used otherwise.
    # Any OpenAI-compatible /chat/completions endpoint: Ollama, vLLM, hosted APIs.
    llm_enabled: bool = False
    llm_base_url: str = "http://localhost:11434/v1"
    llm_model: str = "qwen2.5:7b-instruct"
    llm_timeout_s: float = 20.0

    def with_overrides(self, **kwargs) -> "Settings":
        return replace(self, **kwargs)

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        env = os.environ
        base = cls()
        values: dict = {}
        if "WA_DATA_DIR" in env:
            values["data_dir"] = Path(env["WA_DATA_DIR"])
        if "WA_STATE_DIR" in env:
            values["state_dir"] = Path(env["WA_STATE_DIR"])
        if "WA_LOG_DIR" in env:
            values["log_dir"] = Path(env["WA_LOG_DIR"])
        if "WA_REVIEW_MIN_SEVERITY" in env:
            values["review_min_severity"] = env["WA_REVIEW_MIN_SEVERITY"].lower()
        if "WA_LLM_MODEL" in env:
            values["llm_model"] = env["WA_LLM_MODEL"]
        if "WA_LLM_BASE_URL" in env:
            values["llm_base_url"] = env["WA_LLM_BASE_URL"]
        values["llm_enabled"] = env.get("WA_LLM_ENABLED", "").lower() in {"1", "true", "yes"}
        values.update(overrides)
        return replace(base, **values)
