"""Detectors for Kubernetes manifest fragilities.

YAML scanning, line-based. Real-world manifests are well-defined enough that
regex is fine for v1; jump to a YAML AST walker if false positives appear.
"""

from __future__ import annotations

import re

from agents.diagnostician.tools.code_reader import TargetCodeReader
from agents.tester.detectors._base import Issue

_DEPLOYMENT_KIND = re.compile(r"^kind:\s*Deployment\s*$", re.MULTILINE)
_REPLICAS_ONE = re.compile(r"^\s*replicas:\s*1\s*(?:#.*)?$")
_REQUIRED_AFFINITY = re.compile(
    r"^\s*requiredDuringSchedulingIgnoredDuringExecution\s*:?\s*$"
)


def _yaml_files(code: TargetCodeReader) -> list[str]:
    return sorted(set(code.list_files("**/*.yaml") + code.list_files("**/*.yml")))


# --------------------------------------------------------------------------- #
# replicas: 1 on a Deployment                                                 #
# --------------------------------------------------------------------------- #


class SingleReplicaDetector:
    """Flag k8s Deployments configured with replicas: 1.

    Limitation: we check ``kind: Deployment`` and ``replicas: 1`` exist in the
    same file; we don't tie them to the same document within a multi-doc YAML.
    For the common case (one Deployment per file) that's accurate enough.
    """

    name = "single-replica"

    def find(self, code: TargetCodeReader) -> list[Issue]:
        out: list[Issue] = []
        for path in _yaml_files(code):
            try:
                text = code.read_file(path)
            except Exception:
                continue
            if not _DEPLOYMENT_KIND.search(text):
                continue
            for line_num, line in enumerate(text.splitlines(), start=1):
                if _REPLICAS_ONE.match(line):
                    out.append(
                        Issue(
                            file=path,
                            line=line_num,
                            snippet=line.strip(),
                            detail="Deployment with replicas: 1",
                        )
                    )
                    break  # one finding per file
        return out


# --------------------------------------------------------------------------- #
# Hard pod-affinity                                                           #
# --------------------------------------------------------------------------- #


class HardPodAffinityDetector:
    """Flag ``requiredDuringSchedulingIgnoredDuringExecution`` — a hard
    affinity that can pin a pod to one node.
    """

    name = "hard-pod-affinity"

    def find(self, code: TargetCodeReader) -> list[Issue]:
        out: list[Issue] = []
        for path in _yaml_files(code):
            try:
                text = code.read_file(path)
            except Exception:
                continue
            for line_num, line in enumerate(text.splitlines(), start=1):
                if _REQUIRED_AFFINITY.match(line):
                    out.append(
                        Issue(
                            file=path,
                            line=line_num,
                            snippet=line.strip(),
                            detail="hard affinity / anti-affinity rule",
                        )
                    )
                    break
        return out
