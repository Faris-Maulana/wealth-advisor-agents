"""Advisor-facing narrative summary.

Two implementations behind one tool contract:

* `TemplateNarrator` (default): deterministic, always available, zero cost.
* `LLMNarrator`: optional; rewrites the same facts into prose via any
  OpenAI-compatible chat-completions endpoint (Ollama, vLLM, hosted APIs).
  Its output passes a guardrail before it is accepted:
  every number in the narrative must appear in the facts it was given (no
  hallucinated figures) and it must not contain promissory language. If the
  call fails or the guardrail rejects the text, the agent falls back to the
  template. The LLM never decides anything; it only phrases.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Optional

from ..errors import PermanentToolError, SchemaValidationError, ToolTimeoutError, TransientToolError
from .base import ParamSpec, Tool, ToolSpec

BANNED_PHRASES = ("guarantee", "risk-free", "risk free", "cannot lose", "certain to")
NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")


def build_facts(report_core: dict) -> dict:
    """Compact, LLM-safe fact sheet (no raw transactions or PII beyond first name)."""
    risk = report_core.get("risk") or {}
    return {
        "client_first_name": (report_core.get("client_name") or "").split(" ")[0],
        "risk_band": risk.get("band"),
        "risk_score_pct": round((risk.get("value") or 0) * 100),
        "risk_score_scale": 100,
        "previous_risk_score_pct": (round(risk["previous_value"] * 100)
                                    if risk.get("previous_value") is not None else None),
        "top_findings": [{"title": a["title"], "severity": a["severity"]}
                         for a in report_core.get("anomalies", []) if a.get("status") not in ("suppressed", "dismissed")][:5],
        "recommendations": [{"priority": r["priority"], "action": r["action"]}
                            for r in report_core.get("recommendations", [])][:5],
        "data_caveats": report_core.get("degraded_sources", []),
    }


class Narrator(ABC):
    source: str

    @abstractmethod
    def narrate(self, facts: dict) -> str:
        ...


class TemplateNarrator(Narrator):
    source = "template"

    def narrate(self, facts: dict) -> str:
        name = facts.get("client_first_name") or "The client"
        band = facts.get("risk_band") or "unknown"
        score = facts.get("risk_score_pct")
        lines = [f"{name}'s overall risk is {band.upper()} (score {score}/100)."]
        prev = facts.get("previous_risk_score_pct")
        if prev is not None:
            direction = "up" if score > prev else "down" if score < prev else "unchanged"
            lines[-1] = lines[-1][:-1] + f", {direction} from {prev}/100 at the last review."
        findings = facts.get("top_findings") or []
        if findings:
            lines.append("Key findings: " + "; ".join(f"{f['title']} [{f['severity']}]" for f in findings) + ".")
        else:
            lines.append("No material anomalies or risk signals were detected.")
        recs = facts.get("recommendations") or []
        if recs:
            lines.append(f"Most urgent action ({recs[0]['priority']}): {recs[0]['action']}")
        if facts.get("data_caveats"):
            lines.append("Caveat: analysis ran in degraded mode for " + ", ".join(facts["data_caveats"])
                         + "; some checks used cached or partial data.")
        return " ".join(lines)


class LLMNarrator(Narrator):
    source = "llm"
    SYSTEM = ("You are an assistant to a licensed wealth advisor. Rewrite the provided JSON facts into a concise "
              "briefing of at most 120 words for the advisor. Use only the facts given; do not introduce any "
              "numbers, products, or claims that are not in the facts. Never promise returns. Plain prose, no "
              "markdown.")

    def __init__(self, model: str, base_url: str, api_key: Optional[str] = None, timeout_s: float = 20.0):
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key if api_key is not None else os.environ.get("WA_LLM_API_KEY")
        self.timeout_s = timeout_s

    def narrate(self, facts: dict) -> str:
        body = json.dumps({
            "model": self.model,
            "max_tokens": 400,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": self.SYSTEM},
                         {"role": "user", "content": json.dumps(facts)}],
        }).encode()
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise TransientToolError(f"llm: HTTP {exc.code}") from exc
            raise PermanentToolError(f"llm: HTTP {exc.code}") from exc
        except TimeoutError as exc:
            raise ToolTimeoutError("llm: request timed out") from exc
        except urllib.error.URLError as exc:
            raise TransientToolError(f"llm: {exc.reason}") from exc
        try:
            text = (payload["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise SchemaValidationError("llm: unexpected response shape") from exc
        if not text:
            raise SchemaValidationError("llm: empty completion")
        return text


def guardrail_violations(text: str, facts: dict) -> list[str]:
    problems = []
    lowered = text.lower()
    problems += [f"banned phrase '{p}'" for p in BANNED_PHRASES if p in lowered]
    fact_blob = json.dumps(facts)
    allowed = {n.replace(",", "").rstrip(".") for n in NUMBER_RE.findall(fact_blob)}
    for n in NUMBER_RE.findall(text):
        norm = n.replace(",", "").rstrip(".")
        if norm and norm not in allowed:
            problems.append(f"number '{n}' not present in facts")
    if len(text.split()) > 180:
        problems.append("narrative too long")
    return problems


class NarrativeTool(Tool):
    spec = ToolSpec(
        name="narrative.generate",
        description="Produce an advisor-facing summary from a fact sheet. Validated against the facts.",
        params={"facts": ParamSpec(dict)},
    )

    def __init__(self, narrator: Narrator, name: str = "narrative.generate"):
        self.narrator = narrator
        self.spec = replace(NarrativeTool.spec, name=name)

    def run(self, facts: dict) -> dict:
        text = self.narrator.narrate(facts)
        problems = guardrail_violations(text, facts)
        if problems:
            raise SchemaValidationError("narrative rejected by guardrail: " + "; ".join(problems))
        return {"text": text, "source": self.narrator.source}
