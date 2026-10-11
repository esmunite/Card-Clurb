"""Market prices from eBay-observed sold listings.

The single source of truth is the sales ledger (sales_ledger.py): confirmed
eBay sales the scanner itself watched happen. Each search's market price is
the median of its recent confirmed sales.

Until a search has enough history (MIN_SALES), the hand-set `market_usd`
anchor is used and labeled "static-anchor" — an honest placeholder, never
presented as observed data. No third-party price sites, ever.

Mock mode: no ledger I/O; anchors only; everything counts as liquid.
"""

from __future__ import annotations

try:
    from .sales_ledger import SalesLedger
except ImportError:  # run as a top-level script (run_scan.py), not as a package
    from sales_ledger import SalesLedger

# Re-exported for callers that tune the reference.
EBAY_OBSERVED = "ebay-observed"
STATIC_ANCHOR = "static-anchor"


class MarketPricer:
    """Resolves a market price per tracked search."""

    def __init__(self, ledger: SalesLedger, mock_mode: bool = False) -> None:
        self.ledger = ledger
        self.mock_mode = mock_mode

    def price_for(self, search: dict) -> tuple[float | None, str]:
        """(price, source). eBay-observed median wins; anchor falls back."""
        if not self.mock_mode:
            median, _n = self.ledger.median_for(search["name"])
            if median is not None:
                return median, EBAY_OBSERVED
        anchor = search.get("market_usd")
        if anchor:
            return float(anchor), STATIC_ANCHOR
        return None, "none"

    def is_liquid(self, search: dict) -> bool:
        """False only when the ledger positively shows demand is dead."""
        if self.mock_mode:
            return True
        return self.ledger.is_liquid(search["name"])
