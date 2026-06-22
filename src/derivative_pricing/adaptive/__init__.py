"""Adaptive parsing: declarative specs + a generic engine + an AI proposer.

See SYSTEMIMPLEMENTATION.md section 8. The engine runs a ParserSpec
deterministically to produce BrokerMark records; the proposer uses an LLM
(at onboarding only) to draft a spec for a new/unknown broker format.
"""

from derivative_pricing.adaptive.spec import ColumnRule, DirectionRule, ParserSpec
from derivative_pricing.adaptive.engine import run_spec

__all__ = ["ColumnRule", "DirectionRule", "ParserSpec", "run_spec"]
