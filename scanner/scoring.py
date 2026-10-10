"""Deal-scoring engine: turns raw eBay listings into ranked deals.

Pipeline per listing:
    1. estimate market price  (sold-comps reference; mock table until the
       Marketplace Insights API / self-built comps corpus is live)
    2. component scores 0-100: discount, auction urgency, seller trust,
       listing quality
    3. composite deal score = weighted sum (weights configurable)
    4. hard flags (counterfeit keywords etc.) can veto a deal outright

All functions are pure and deterministic -> easy to unit test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# --------------------------------------------------------------------- #
# configurable weights (must sum to 1.0)
# --------------------------------------------------------------------- #
DEFAULT_WEIGHTS = {
    "discount_vs_market": 0.45,
    "auction_urgency": 0.25,
    "seller_trust": 0.15,
    "listing_quality": 0.15,
}

# Listings containing any of these are treated as counterfeit/reprint risk
# and can never score as a deal, no matter how cheap.
COUNTERFEIT_KEYWORDS = (
    "reprint", "proxy", "proxie", "custom card", "fan art", "not real",
    "replica", "fake",
)

# Phrases that suggest a stock photo rather than the actual item.
STOCK_PHOTO_KEYWORDS = ("stock photo", "stock image", "picture for reference", "not actual item")

# Wear-and-tear red flags: the card exists, but nobody should pay market for it.
# Any of these in the title vetoes the listing outright.
CONDITION_RED_FLAGS = (
    "heavily played", "heavy play", "hp condition",
    "damaged", "water damage", "creased", "crease",
    "for parts", "poor condition", "bad condition",
    "played condition",
)

# Grading companies recognized when matching graded searches.
GRADING_COMPANIES = ("psa", "bgs", "cgc", "sgc", "ace grading", "tag", "hga")

import re as _re
_GRADE_RE = _re.compile(r"\b(10|9\.5|9|8\.5|8)\b")


def looks_graded(title_lower: str) -> bool:
    """Title shows a grading company plus a numeric grade (e.g. 'PSA 10')."""
    return any(c in title_lower for c in GRADING_COMPANIES) and bool(_GRADE_RE.search(title_lower))

# Minimum composite score (0-100) for a listing to be published as a deal.
DEAL_THRESHOLD = 70.0

# BIN listings priced at least this far below market count as "mispriced".
MISPRICED_BIN_DISCOUNT = 0.15  # 15%+ below market


# --------------------------------------------------------------------- #
# market reference
# --------------------------------------------------------------------- #
@dataclass
class MarketReference:
    """Estimated market prices per tracked search.

    MVP: seeded from a static table (mock mode) / manual research (live).
    Phase 2: accumulate sold prices from our own daily disappearance checks
    until the corpus is mature, or use eBay Marketplace Insights API once
    EPN partner approval lands.
    """

    prices: dict[str, float] = field(default_factory=dict)

    def lookup(self, search_name: str) -> float | None:
        return self.prices.get(search_name)

    def record_sale(self, search_name: str, price: float) -> None:
        """Fold one observed sold price into a running median-ish estimate."""
        old = self.prices.get(search_name)
        self.prices[search_name] = price if old is None else round((old * 3 + price) / 4, 2)


# Approximate market anchors for the starter searches (USD). These are
# rough, human-set starting points — the live system refines them from
# observed sales over time. Never presented to users as exact values.
MOCK_MARKET_PRICES: dict[str, float] = {
    "charizard_151_psa10": 185.0,
    "charizard_151_sir": 28.0,
    "151_booster_box_sealed": 165.0,
    "151_etb_sealed": 62.0,
    "prismatic_evo_etb_sealed": 78.0,
    "prismatic_evo_booster_box": 175.0,
    "umbreon_prismatic_sir": 420.0,
    "moonbreon_psa10": 1150.0,
    "charizard_151_misspell": 24.0,
    "base_set_charizard_proxy_watch": 0.0,  # informational only
}


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #
def _price_of(listing: dict) -> float | None:
    try:
        return float(listing["price"]["value"])
    except (KeyError, TypeError, ValueError):
        return None


def _title_lower(listing: dict) -> str:
    return str(listing.get("title", "")).lower()


def _is_auction(listing: dict) -> bool:
    return "AUCTION" in (listing.get("buyingOptions") or [])


def _hours_to_end(listing: dict, now: datetime) -> float | None:
    raw = listing.get("auctionEndDate")
    if not raw:
        return None
    try:
        end = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (end - now).total_seconds() / 3600.0
    except ValueError:
        return None


# --------------------------------------------------------------------- #
# component scores (each 0-100, higher = better deal signal)
# --------------------------------------------------------------------- #
def discount_score(price: float | None, market: float | None) -> float:
    """How far below estimated market the listing is priced."""
    if price is None or not market or market <= 0:
        return 50.0  # neutral when we have no market anchor
    discount = (market - price) / market
    if discount >= 0.40:
        return 100.0
    if discount >= 0.25:
        return 85.0
    if discount >= MISPRICED_BIN_DISCOUNT:
        return 70.0
    if discount >= 0.05:
        return 45.0
    if discount >= -0.05:
        return 25.0
    return 5.0  # overpriced


def auction_urgency_score(listing: dict, now: datetime | None = None) -> float:
    """Ending-soon auctions with few/no bids are the classic snipe setup."""
    now = now or datetime.now(timezone.utc)
    if not _is_auction(listing):
        return 50.0  # neutral for BIN
    hours = _hours_to_end(listing, now)
    bids = int(listing.get("bidCount") or 0)
    if hours is None:
        return 40.0
    if bids == 0 and hours <= 6:
        return 100.0
    if bids == 0 and hours <= 24:
        return 80.0
    if bids <= 2 and hours <= 6:
        return 70.0
    if hours <= 24:
        return 45.0
    return 20.0


def seller_trust_score(listing: dict) -> float:
    """Feedback-based trust. New/low-feedback sellers get penalized, not banned."""
    seller = listing.get("seller") or {}
    try:
        feedback = int(seller.get("feedbackScore") or 0)
        pct = float(seller.get("feedbackPercentage") or 100.0)
    except (TypeError, ValueError):
        return 40.0
    score = 60.0
    if feedback >= 1000:
        score += 25
    elif feedback >= 100:
        score += 15
    elif feedback < 10:
        score -= 25
    if pct >= 99.5:
        score += 15
    elif pct >= 98.0:
        score += 5
    elif pct < 95.0:
        score -= 30
    return max(0.0, min(100.0, score))


def listing_quality_score(listing: dict) -> float:
    """Penalize stock-photo / vague listings; reward descriptive ones."""
    title = _title_lower(listing)
    score = 70.0
    if any(k in title for k in STOCK_PHOTO_KEYWORDS):
        score -= 30
    if len(title) < 20:
        score -= 15
    if listing.get("image", {}).get("imageUrl"):
        score += 5
    return max(0.0, min(100.0, score))


def hard_flags(listing: dict, search_category: str = "") -> list[str]:
    """Deal-breakers. Any flag here vetoes the listing as a deal."""
    title = _title_lower(listing)
    flags = [f"counterfeit_risk:{kw}" for kw in COUNTERFEIT_KEYWORDS if kw in title]
    flags += [f"condition_risk:{kw}" for kw in CONDITION_RED_FLAGS if kw in title]
    # A graded search ("... PSA 10") that returns an ungraded card is not a
    # mispriced graded card — it's a different product. Veto the mismatch.
    if search_category == "graded_singles" and not looks_graded(title):
        flags.append("condition_mismatch:ungraded_in_graded_search")
    return flags


# --------------------------------------------------------------------- #
# composite
# --------------------------------------------------------------------- #


# --------------------------------------------------------------------- #
# composite
# --------------------------------------------------------------------- #
def score_deal(
    listing: dict,
    market: MarketReference,
    search_name: str,
    weights: dict[str, float] | None = None,
    now: datetime | None = None,
    search_category: str = "",
) -> dict:
    """Score one listing. Returns a scored-deal dict (JSON-serializable)."""
    weights = weights or DEFAULT_WEIGHTS
    now = now or datetime.now(timezone.utc)
    price = _price_of(listing)
    market_price = market.lookup(search_name)

    components = {
        "discount_vs_market": discount_score(price, market_price),
        "auction_urgency": auction_urgency_score(listing, now),
        "seller_trust": seller_trust_score(listing),
        "listing_quality": listing_quality_score(listing),
    }
    composite = round(sum(components[k] * weights[k] for k in components), 1)
    flags = hard_flags(listing, search_category)

    deal_type = "watch"
    if _is_auction(listing) and components["auction_urgency"] >= 70:
        deal_type = "auction_snipe"
    elif (
        price is not None
        and market_price
        and (market_price - price) / market_price >= MISPRICED_BIN_DISCOUNT
    ):
        deal_type = "mispriced_bin"

    return {
        "item_id": listing.get("itemId", ""),
        "title": listing.get("title", ""),
        "price": price,
        "currency": (listing.get("price") or {}).get("currency", "USD"),
        "url": listing.get("itemWebUrl", ""),
        "image": (listing.get("image") or {}).get("imageUrl", ""),
        "buying_options": listing.get("buyingOptions", []),
        "bid_count": listing.get("bidCount"),
        "ends_at": listing.get("auctionEndDate"),
        "seller": (listing.get("seller") or {}).get("username", "unknown"),
        "search_name": search_name,
        "market_estimate": market_price,
        "deal_score": composite,
        "deal_type": deal_type,
        "components": components,
        "flags": flags,
        "is_deal": composite >= DEAL_THRESHOLD and not flags,
    }


def is_deal(scored: dict, threshold: float = DEAL_THRESHOLD) -> bool:
    return bool(scored.get("is_deal")) and scored.get("deal_score", 0) >= threshold
