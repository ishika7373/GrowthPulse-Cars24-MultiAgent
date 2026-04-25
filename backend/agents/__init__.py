from .campaign_agent import run_campaign_agent
from .audience_agent import run_audience_agent
from .bidding_agent import run_bidding_agent
from .budget_agent import run_budget_agent

SPECIALIST_RUNNERS = {
    "CampaignAgent": run_campaign_agent,
    "AudienceAgent": run_audience_agent,
    "BiddingAgent": run_bidding_agent,
    "BudgetAgent": run_budget_agent,
}

__all__ = [
    "run_campaign_agent",
    "run_audience_agent",
    "run_bidding_agent",
    "run_budget_agent",
    "SPECIALIST_RUNNERS",
]
