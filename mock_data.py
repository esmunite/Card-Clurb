"""Deterministic mock listings so the pipeline runs with $0 and no keys.

Shapes mirror eBay Browse API item summaries:
    itemId, title, price{value,currency}, buyingOptions,
    bidCount, auctionEndDate, seller{feedbackScore, feedbackPercentage},
    itemWebUrl, image{imageUrl}, condition, shippingOptions...
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone

MOCK_SELLERS = [
    {"username": "cardvault_tcg", "feedbackScore": 12480, "feedbackPercentage": "99.8"},
    {"username": "pokepullz", "feedbackScore": 3421, "feedbackPercentage": "100.0"},
    {"username": "new_seller_2026", "feedbackScore": 7, "feedbackPercentage": "85.7"},
    {"username": "slab_kings", "feedbackScore": 882, "feedbackPercentage": "99.1"},
    {"username": "reprint_city", "feedbackScore": 312, "feedbackPercentage": "97.4"},
]

# (template, typical price USD, buying option)
MOCK_LISTING_TEMPLATES = [
    ("{q} - NEAR MINT!! Fast Ship", 0.82, "FIXED_PRICE"),   # underpriced BIN
    ("{q} PSA 10 GEM MINT Slab", 1.35, "FIXED_PRICE"),       # overpriced BIN
    ("{q} - auction, no reserve", 0.45, "AUCTION"),          # auction, low start
    ("{q} lot bundle deal", 0.95, "FIXED_PRICE"),           # fairly priced
    ("{q} REPRINT proxy custom card", 0.12, "FIXED_PRICE"), # fake red-flag
]


def _rng_for(query: str, offset: int) -> random.Random:
    seed = int(hashlib.md5(f"{query}:{offset}".encode()).hexdigest()[:8], 16)
    return random.Random(seed)


def mock_search_results(query: str, limit: int = 50, offset: int = 0) -> dict:
    rng = _rng_for(query, offset)
    now = datetime.now(timezone.utc)
    summaries = []
    for i in range(min(limit, 12)):  # 12 mock results per page max
        tpl, price_mult, buying = MOCK_LISTING_TEMPLATES[i % len(MOCK_LISTING_TEMPLATES)]
        seller = MOCK_SELLERS[(i + offset) % len(MOCK_SELLERS)]
        base = 20.0 + (int(hashlib.md5(query.encode()).hexdigest()[:4], 16) % 180)
        # Small per-index jitter so mock listings from the same template don't
        # look like exact duplicates (real eBay results never repeat a price).
        jitter = 1 + (((i * 13 + offset) % 7) - 3) * 0.02
        price = round(base * price_mult * jitter, 2)
        item_id = f"mock-{hashlib.md5(f'{query}{i}{offset}'.encode()).hexdigest()[:10]}"
        summary: dict = {
            "itemId": item_id,
            "title": tpl.format(q=query.title()),
            "price": {"value": f"{price:.2f}", "currency": "USD"},
            "buyingOptions": [buying],
            "condition": "New",
            "seller": seller,
            "itemWebUrl": f"https://www.ebay.com/itm/{item_id}",
            "image": {"imageUrl": "https://example.com/mock-card.jpg"},
            "itemLocation": {"country": "US"},
        }
        if buying == "AUCTION":
            summary["bidCount"] = rng.choice([0, 0, 1, 3])
            summary["auctionEndDate"] = (
                now + timedelta(hours=rng.choice([2, 5, 11, 30, 70]))
            ).isoformat()
            summary["currentBidPrice"] = {"value": f"{price:.2f}", "currency": "USD"}
        summaries.append(summary)
    return {
        "total": 240,
        "limit": limit,
        "offset": offset,
        "itemSummaries": summaries[offset : offset + limit] if offset else summaries,
    }
