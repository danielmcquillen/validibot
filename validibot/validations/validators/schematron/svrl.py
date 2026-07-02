"""SVRL parsing — re-export of the canonical parser in validibot-shared.

The SVRL → findings/summary parser originally lived here, but production
SVRL parsing happens in the **validator backend container** (which parses
Saxon's output into ``SchematronOutputs`` and cannot import this Django
app), so the canonical implementation moved to
``validibot_shared.schematron.svrl`` (shared >= 0.11.0) — beside the
envelope models, because it defines the same protocol boundary: SVRL in,
the D2 signal contract out. One parser, both repos.

This module re-exports it so Django-side code and tests keep their
stable import path. See the shared module for the full contract docs
(severity chain, active ``successful-report`` handling, truncation).
"""

from validibot_shared.schematron.svrl import DEFAULT_MAX_FINDINGS
from validibot_shared.schematron.svrl import SEVERITY_ERROR
from validibot_shared.schematron.svrl import SEVERITY_INFO
from validibot_shared.schematron.svrl import SEVERITY_WARNING
from validibot_shared.schematron.svrl import SVRL_NS
from validibot_shared.schematron.svrl import SvrlFinding
from validibot_shared.schematron.svrl import SvrlParseError
from validibot_shared.schematron.svrl import SvrlSummary
from validibot_shared.schematron.svrl import parse_svrl

__all__ = [
    "DEFAULT_MAX_FINDINGS",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SVRL_NS",
    "SvrlFinding",
    "SvrlParseError",
    "SvrlSummary",
    "parse_svrl",
]
