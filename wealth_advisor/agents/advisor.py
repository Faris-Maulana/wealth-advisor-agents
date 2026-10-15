"""Advisor Agent: turns validated analysis into recommendations and a narrative."""

from __future__ import annotations

from ..models import AnalysisResult, ClientContext, Recommendation, StepStatus, to_jsonable
from ..tools.narrative import TemplateNarrator, build_facts
from .base import Agent, AgentResult, RunContext, Task


class AdvisorAgent(Agent):
    name = "advisor"
    capabilities = frozenset({"build_advice"})
    required_tools = frozenset({"advisory.recommend", "narrative.template"})
    optional_tools = frozenset({"narrative.llm"})

    def execute(self, task: Task, ctx: RunContext) -> AgentResult:
        context: ClientContext = task.payload["context"]
        analysis: AnalysisResult = task.payload["analysis"]
        warnings: list[str] = []
        status = StepStatus.OK

        res = self.tools.call("advisory.recommend", analysis=analysis,
                              profile=context.profile if context.profile.source != "unavailable" else None)
        if res.ok:
            recs = res.data
        else:
            status = StepStatus.DEGRADED
            warnings.append("recommendation engine failed; manual review required")
            self.decide("recommendations_fallback", reason=res.error)
            recs = [Recommendation("R1", "P1", "general", "Recommendation engine failed: review findings manually.",
                                   res.error or "unknown error", requires_approval=True)]

        facts = build_facts({
            "client_name": context.name,
            "risk": to_jsonable(analysis.risk),
            "anomalies": to_jsonable(analysis.anomalies),
            "recommendations": to_jsonable(recs),
            "degraded_sources": context.degraded_sources,
        })
        narrative, source = self._narrate(facts, warnings)
        output = {"recommendations": recs, "narrative": narrative, "narrative_source": source}
        ctx.session.put(ctx.run_id, "advice", to_jsonable(output))
        return AgentResult(status, output=output, warnings=warnings)

    def _narrate(self, facts: dict, warnings: list[str]) -> tuple[str, str]:
        if "narrative.llm" in self.tools.names:
            res = self.tools.call("narrative.llm", facts=facts)
            if res.ok:
                return res.data["text"], res.data["source"]
            self.decide("narrative_fallback_template", reason=res.error)
            warnings.append("LLM narrative unavailable or rejected by guardrail; used template")
        res = self.tools.call("narrative.template", facts=facts)
        if res.ok:
            return res.data["text"], res.data["source"]
        # Last resort: the template should never fail, but never block the report on prose.
        return TemplateNarrator().narrate({}), "template"
