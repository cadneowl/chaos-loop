"""
Static detectors for chaos-engineering hypotheses.

Each detector scans the target's source via TargetCodeReader and emits
``Issue`` objects pointing at code locations that match a fragility pattern.
``StaticHypothesizer`` (in agents/tester/hypothesizer.py) wraps each Issue in a
templated ``Hypothesis`` keyed to a catalogue fault.

The point: pattern detection is a Python job. Asking an LLM to scan a repo for
"places that call redis without retry" wastes both inference budget and
reliability — a regex with two lookarounds does it deterministically. Reserve
the LLM for the irreducible cognitive bits (refining wording, ranking, novel
patterns the rules don't cover).
"""

from agents.tester.detectors._base import Detector, Issue, hypothesis_id, slug
from agents.tester.detectors.async_blocking import SyncCallInAsyncDetector
from agents.tester.detectors.k8s import (
    HardPodAffinityDetector,
    SingleReplicaDetector,
)
from agents.tester.detectors.network import (
    MissingRetryDetector,
    MissingTimeoutDetector,
)
from agents.tester.detectors.resilience import (
    MissingCircuitBreakerDetector,
    NoFallbackForCacheDetector,
)
from agents.tester.detectors.secrets import HardcodedSecretDetector

__all__ = [
    "Detector",
    "HardPodAffinityDetector",
    "HardcodedSecretDetector",
    "Issue",
    "MissingCircuitBreakerDetector",
    "MissingRetryDetector",
    "MissingTimeoutDetector",
    "NoFallbackForCacheDetector",
    "SingleReplicaDetector",
    "SyncCallInAsyncDetector",
    "default_detectors",
    "hypothesis_id",
    "slug",
]


def default_detectors() -> list[Detector]:
    """The detector set StaticHypothesizer uses when no explicit list is passed."""
    return [
        MissingTimeoutDetector(),
        MissingRetryDetector(),
        MissingCircuitBreakerDetector(),
        NoFallbackForCacheDetector(),
        SyncCallInAsyncDetector(),
        SingleReplicaDetector(),
        HardPodAffinityDetector(),
        HardcodedSecretDetector(),
    ]
