"""Mock agents for orchestrator dry-runs. Used by `chaos run --dry-run`."""

from __future__ import annotations

from datetime import UTC

from shared.contracts import (
    ChaosTimeline,
    DiagnosisReport,
    DiagnosisRequest,
    ExperimentPlan,
    FixAction,
    FixProposal,
    RootCauseHypothesis,
    SecurityReport,
    SecurityRequest,
    StatisticalSample,
    TesterReport,
    TesterRequest,
    TimelineEvent,
)


class _MockTester:
    async def baseline(self, req: TesterRequest) -> TesterReport:
        return TesterReport(
            request_kind="baseline",
            experiment_id=req.experiment_id,
            steady_state=True,
            samples=[
                StatisticalSample.from_samples("http_p95_ms", [120.0, 118.0, 125.0, 121.0, 119.0])
            ],
            notes="mock baseline; system declared healthy",
        )

    async def verify(self, req: TesterRequest) -> TesterReport:
        return TesterReport(
            request_kind="verify",
            experiment_id=req.experiment_id,
            steady_state=False,
            failed_probes=["cart-checkout-e2e"],
            anomalies=["http_p95_ms drifted 5x above baseline"],
            notes="mock verify; simulated regression",
        )


class _MockSecurity:
    async def baseline(self, req: SecurityRequest) -> SecurityReport:
        return SecurityReport(request_kind="baseline", experiment_id=req.experiment_id)

    async def verify(self, req: SecurityRequest) -> SecurityReport:
        return SecurityReport(request_kind="verify", experiment_id=req.experiment_id)


class _MockChaos:
    async def execute(self, plan: ExperimentPlan) -> ChaosTimeline:
        from datetime import datetime

        now = datetime.now(tz=UTC)
        return ChaosTimeline(
            experiment_id=plan.experiment_id,
            events=[
                TimelineEvent(timestamp=now, fault_name=plan.faults[0].name, event="scheduled"),
                TimelineEvent(timestamp=now, fault_name=plan.faults[0].name, event="started"),
                TimelineEvent(timestamp=now, fault_name=plan.faults[0].name, event="cleaned-up"),
            ],
            success=True,
        )

    async def cleanup(self, plan: ExperimentPlan) -> None:
        return None

    async def get_namespace_annotations(self, namespace: str) -> dict[str, str]:
        # The dry-run path always satisfies the namespace-annotation gate so
        # the loop runs end-to-end without cluster setup.
        return {"chaos.kosta.dev/allowed": "true"}


class _MockDiagnostician:
    async def diagnose(self, req: DiagnosisRequest) -> DiagnosisReport:
        return DiagnosisReport(
            experiment_id=req.experiment_id,
            hypotheses=[
                RootCauseHypothesis(
                    summary="cart service has hard dep on Redis with no retry",
                    confidence=0.8,
                    evidence=["mock: traces show 100% failure rate on cart GET when Redis blocked"],
                    suggested_fix_class="missing-retry",
                    affected_paths=["services/cart/redis_client.py"],
                )
            ],
            notes="mock diagnosis",
        )


class _MockFixer:
    async def propose_fix(self, diagnosis: DiagnosisReport) -> FixProposal:
        return FixProposal(
            experiment_id=diagnosis.experiment_id,
            action=FixAction.CODE_PATCH,
            pr_url="https://example.invalid/mock/pr/123",
            confidence=0.7,
            reasoning="mock: propose adding a 3-retry exponential backoff to redis_client",
            files_touched=["services/cart/redis_client.py", "services/cart/tests/test_redis.py"],
            regression_test_added=True,
        )


def build_mock_agents() -> dict[str, object]:
    return {
        "tester": _MockTester(),
        "security": _MockSecurity(),
        "chaos": _MockChaos(),
        "diagnostician": _MockDiagnostician(),
        "fixer": _MockFixer(),
    }
