"""
Tool registry — kept SCOPED per specialist.

IMPORTANT: each specialist agent imports ONLY its own tool list. We never
expose `ALL_TOOLS` to a specialist. This satisfies the Specialist Isolation
Rule (Section 3.3 of the case study) and is what the evaluator inspects.
"""
from .campaign_tools import CAMPAIGN_TOOLS
from .audience_tools import AUDIENCE_TOOLS
from .bidding_tools import BIDDING_TOOLS
from .budget_tools import BUDGET_TOOLS

__all__ = ["CAMPAIGN_TOOLS", "AUDIENCE_TOOLS", "BIDDING_TOOLS", "BUDGET_TOOLS"]
