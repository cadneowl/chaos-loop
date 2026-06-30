"""First-party example plugins, imported on discovery.

Keep these dependency-free so ``chaos plugins list`` and the test-suite work
offline. They double as the executable reference for the hook contract.
"""

from __future__ import annotations

from plugins.examples.keyvalue_scenario import KeyValueScenario
from plugins.examples.web_service_scenario import WebServiceScenario

__all__ = ["KeyValueScenario", "WebServiceScenario"]
