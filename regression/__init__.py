"""Resilience regression suites.

The *confirmation* counterpart to the discovery loop: replay a curated corpus of
frozen scenarios and assert everything that used to hold still holds. A scenario
is an ``ExperimentPlan`` + an oracle plugin; a suite is many scenarios plus a
coverage view. See ``REGRESSION_TESTING_PLAN.md``.
"""

from __future__ import annotations
