"""Diagnostician agent. Correlates failed reports + chaos timeline + code to produce ranked hypotheses."""

from __future__ import annotations

from pathlib import Path

from agents.diagnostician.diagnoser import Diagnoser
from agents.diagnostician.tools.code_reader import TargetCodeReader
from agents.diagnostician.tools.loki import LokiBackend
from agents.tester.tools.prometheus import PromBackend
from shared.contracts import DiagnosisReport, DiagnosisRequest

_PROMPT_DIR = Path(__file__).parent / "prompts"


class ClaudeDiagnosticianAgent:
    """Implements `orchestrator.loop.DiagnosticianAgent`.

    The agent owns wiring (DiagnosisRequest -> tools -> diagnoser -> DiagnosisReport).
    The cognitive step lives behind the Diagnoser Protocol so this class can be
    tested deterministically with FixtureDiagnoser and later upgraded to a real
    LLM-backed Diagnoser without touching the orchestrator integration.
    """

    def __init__(
        self,
        *,
        diagnoser: Diagnoser,
        loki: LokiBackend | None = None,
        prom: PromBackend | None = None,
        code: TargetCodeReader | None = None,
        model: str = "claude-opus-4-7",
    ) -> None:
        self._diagnoser = diagnoser
        self._loki = loki
        self._prom = prom
        self._code = code
        self.model = model

    async def diagnose(self, req: DiagnosisRequest) -> DiagnosisReport:
        # The DiagnosisRequest schema already validates that at least one failed
        # report was supplied. The Diagnoser does the cognitive work; we own the
        # report assembly + ranking invariants.
        hypotheses = await self._diagnoser.diagnose(
            request=req, loki=self._loki, prom=self._prom, code=self._code
        )

        if not hypotheses:
            # The DiagnosisReport schema mandates >=1 hypothesis. If the diagnoser
            # had nothing to say, that's still information: produce a low-confidence
            # "unknown" hypothesis so the contract holds and the fixer can choose
            # action=NONE downstream.
            from shared.contracts import RootCauseHypothesis

            hypotheses = [
                RootCauseHypothesis(
                    summary="diagnoser produced no hypotheses",
                    confidence=0.0,
                    evidence=[],
                    suggested_fix_class="working-as-intended",
                    affected_paths=[],
                )
            ]

        # Rank by confidence descending so the fixer always reads the top one first.
        ranked = sorted(hypotheses, key=lambda h: h.confidence, reverse=True)

        return DiagnosisReport(
            experiment_id=req.experiment_id,
            hypotheses=ranked,
            notes=_notes_for(req),
        )


def _notes_for(req: DiagnosisRequest) -> str:
    """Short summary of inputs the diagnoser had access to, for the audit trail."""
    bits = []
    if req.failed_tester_report:
        bits.append(
            f"tester: {len(req.failed_tester_report.failed_probes)} failed probe(s), "
            f"{len(req.failed_tester_report.anomalies)} anomaly(s)"
        )
    if req.failed_security_report:
        bits.append(
            f"security: {len(req.failed_security_report.findings)} finding(s)"
        )
    bits.append(f"chaos: {len(req.chaos_timeline.events)} timeline event(s)")
    return "; ".join(bits)
