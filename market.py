"""Live market prices + demand filter for the PokéDeal Scanner.

Two upgrades in one place:

1. LIVE PRICES — raw-singles searches mapped to a PokemonTCG.io card id get
   real TCGplayer market prices (free API, no key needed) instead of the
   hand-set `market_usd` anchors, which go stale. Graded and sealed searches
   keep their curated anchors until the paid PriceCharting API is worth it.

2. DEMAND FILTER — a card nobody is buying is not a deal, it's a trap.
   TCGplayer only publishes a market price when a card has recent sales, so
   a live lookup that returns no market price (or price data older than
   LIQUIDITY_MAX_AGE_DAYS) means the card is dead: the search is skipped
   for that run and its API calls go to cards people actually buy.

Caching: live prices refresh at most once every 24h (cache file), so the
6×/day scan cadence costs ~8 PokemonTCG.io calls/day, not 8×25.

Mock mode: no network ever — every search falls back to its static anchor
and every search counts as liquid (deterministic tests stay deterministic).
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TCG_API_CARD_URL = "https://api.pokemontcg.io/v2/cards"
CACHE_TTL_SECONDS = 24 * 3600
# TCGplayer market data older than this => nobody is buying it => dead card.
LIQUIDITY_MAX_AGE_DAYS = 30


def _parse_tcg_date(s: str | None) -> datetime | None:
    # PokemonTCG.io updatedAt looks like "2026/10/05".
    if not s:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class MarketPricer:
    """Resolves a market price per tracked search, live when possible."""

    def __init__(self, cache_path: str | Path | None = None, api_key: str | None = None,
                 mock_mode: bool = False) -> None:
        self.cache_path = Path(cache_path) if cache_path else None
        self.api_key = api_key or os.environ.get("POKEMONTCG_API_KEY", "").strip()
        self.mock_mode = mock_mode
        self._cache: dict = {}
        self._live: dict[str, dict] = {}   # search name -> {price, updated_at, liquid}
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except (OSError, ValueError):
                self._cache = {}

    # ------------------------------------------------------------------ #
    # live lookup
    # ------------------------------------------------------------------ #
    def _fetch_card(self, tcg_id: str) -> dict | None:
        # Full card fetch (no ?select=): the single-card endpoint is flaky
        # with select filters (observed HTTP 500/502 on 2026-10-06), while
        # the plain fetch works. Payload is cached 24h and only 3 cards
        # are tracked, so the extra bytes are negligible.
        url = f"{TCG_API_CARD_URL}/{urllib.parse.quote(tcg_id, safe='')}"
        headers = {"User-Agent": "PokedealScanner/1.0"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        # PokemonTCG.io throws intermittent 500/502s; retry with backoff so
        # one bad minute doesn't wipe out the whole price refresh.
        for attempt in range(3):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    payload = json.loads(resp.read().decode())
                return (payload or {}).get("data")
            except Exception:
                if attempt < 2:
                    time.sleep(2 ** attempt)
        return None

    def _live_price(self, tcg_id: str, tcg_kind: str) -> dict | None:
        """Returns {price, updated_at, liquid} or None on any failure."""
        now = time.time()
        cached = self._cache.get(tcg_id)
        if cached and now - cached.get("fetched_at", 0) < CACHE_TTL_SECONDS:
            return cached["data"]
        card = self._fetch_card(tcg_id)
        if not card:
            return None
        tcg = card.get("tcgplayer") or {}
        prices = tcg.get("prices") or {}
        kind_prices = prices.get(tcg_kind) or {}
        market = kind_prices.get("market")
        updated_at = _parse_tcg_date(tcg.get("updatedAt"))
        liquid = market is not None and updated_at is not None and \
            (datetime.now(timezone.utc) - updated_at).days <= LIQUIDITY_MAX_AGE_DAYS
        result = {"price": market,
                  "updated_at": tcg.get("updatedAt"),
                  "liquid": liquid}
        self._cache[tcg_id] = {"fetched_at": now, "data": result}
        return result

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def refresh(self, searches: list[dict]) -> None:
        """Pull live prices for searches that declare a tcg_id. Mock: no-op."""
        self._live = {}
        if self.mock_mode:
            return
        for s in searches:
            tcg_id = (s.get("tcg_id") or "").strip()
            if not tcg_id:
                continue
            info = self._live_price(tcg_id, (s.get("tcg_kind") or "holofoil").strip())
            if info:
                self._live[s["name"]] = info
        if self.cache_path:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(self._cache))
            except OSError:
                pass

    def price_for(self, search: dict) -> tuple[float | None, str]:
        """(price, source). Live TCGplayer market wins; static anchor falls back."""
        name = search["name"]
        info = self._live.get(name)
        if info and info.get("price"):
            return float(info["price"]), "tcgplayer-live"
        anchor = search.get("market_usd")
        if anchor:
            return float(anchor), "static-anchor"
        return None, "none"

    def is_liquid(self, search: dict) -> bool:
        """False only when we positively know demand is dead.

        A live lookup with no market price (or stale price data) means nobody
        is buying it. Searches without a live source default to liquid —
        they're curated by hand, not proven dead.
        """
        info = self._live.get(search["name"])
        if info is None:
            return True  # no live data => can't prove it's dead
        return bool(info.get("liquid"))
