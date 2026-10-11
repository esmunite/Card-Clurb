"""PokéDeal Scanner — scan → score → publish pipeline.

Zero-budget, fully automated. Runs in MOCK MODE until real eBay API
credentials are provided via environment variables (never hardcoded).
"""

from .ebay_client import EbayClient, QuotaExhaustedError, DAILY_CALL_BUDGET
from .scoring import score_deal, is_deal, DEFAULT_WEIGHTS, MarketReference

__all__ = [
    "EbayClient",
    "QuotaExhaustedError",
    "DAILY_CALL_BUDGET",
    "score_deal",
    "is_deal",
    "DEFAULT_WEIGHTS",
    "MarketReference",
]
