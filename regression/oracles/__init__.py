"""Oracle plugins — the customer-owned pass/fail predicate under fault.

Each oracle is an ``ExperimentPlugin`` that implements the *double baseline*:
``capture_baseline`` measures steady state before the fault, ``verify`` measures
again under the fault and reports the *newly-failing* delta.
"""

from __future__ import annotations
