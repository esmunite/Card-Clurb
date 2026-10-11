"""eBay-observed sold-price ledger.

The scanner watches eBay listings across runs. When a tracked AUCTION with
bids disappears from eBay, that's a confirmed sale at a known price. Those
observations accumulate here and become the market-price reference:
"what does this card actually sell for on eBay."

Why only auctions with bids: a buy-it-now listing can end unsold, so its
disappearance proves nothing (noisy signal). An auction that ends WITH bids
sold — that's certain. The recorded price is the last-seen bid, i.e. a
slight underestimate of the final price, which biases deal detection
conservative: fewer false "deals", never inflated.

Files (under the scanner logs dir):
  seen_listings.json — every listing observed recently (for disappearance
      detection). Pruned to the last SEEN_PRUNE_DAYS.
  sales_ledger.json  — confirmed sales per search + tracking stats.

Cold start: until a search has MIN_SALES recent sales, pricing falls back
to the hand-set anchor (source "static-anchor"). Each search's source is
labeled in the feed so it's always visible which reference was used.

These files are committed to the repo by the Actions workflow so the
ledger survives across ephemeral runners.

Mock mode: all I/O is skipped (deterministic, no files touched).
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

# A search needs this many sales inside the window before its median
# replaces the hand-set anchor.
MIN_SALES = 5
# Only sales this recent count toward the median.
SALES_WINDOW_DAYS = 30
# Sales older than this are forgotten entirely.
LEDGER_KEEP_DAYS = 45
# Stop tracking listings we haven't seen in this long.
SEEN_PRUNE_DAYS = 10
# A listing must miss this many consecutive runs before we bother
# confirming it's gone (search-result churn causes single misses).
MISSES_BEFORE_CONFIRM = 2
# Max paid confirmation calls (is_active) per run.
CONFIRM_CAP = 20
# "Nobody's buying it" verdict needs both: tracked long enough AND
# plenty of listings watched, with zero sales.
DEAD_MIN_DAYS = 14
DEAD_MIN_OBSERVED = 500


def record_from_listing(listing: dict, search: dict) -> dict | None:
    """Normalize a Browse itemSummary into a ledger observation record."""
    item_id = listing.get("itemId")
    price = listing.get("price") or {}
    try:
        value = float(price.get("value"))
    except (TypeError, ValueError):
        return None
    if not item_id or price.get("currency") != "USD":
        return None
    return {
        "item_id": str(item_id),
        "price": round(value, 2),
        "bid_count": int(listing.get("bidCount") or 0),
        "buying_options": list(listing.get("buyingOptions") or []),
        "search_name": search["name"],
        "record_sales": bool(search.get("record_sales", True)),
    }


class SalesLedger:
    """Tracks listings across runs; confirms sales; serves medians."""

    def __init__(self, logs_dir: str | Path, mock_mode: bool = False) -> None:
        self.dir = Path(logs_dir)
        self.mock_mode = mock_mode
        self.seen_path = self.dir / "seen_listings.json"
        self.sales_path = self.dir / "sales_ledger.json"
        self.seen: dict = {}    # item_id -> {price, bid_count, buying_options,
                                #             search_name, record_sales,
                                #             last_seen, misses}
        self.sales: dict = {}   # search_name -> {"sales": [{ts, price}],
                                #                "first_seen": ts, "observed": n}

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        if self.mock_mode:
            return
        for path, attr in ((self.seen_path, "seen"), (self.sales_path, "sales")):
            try:
                setattr(self, attr, json.loads(path.read_text()))
            except (OSError, ValueError):
                setattr(self, attr, {})

    def save(self) -> None:
        if self.mock_mode:
            return
        now = time.time()
        seen_cutoff = now - SEEN_PRUNE_DAYS * 86400
        self.seen = {k: v for k, v in self.seen.items()
                     if v.get("last_seen", 0) >= seen_cutoff}
        sales_cutoff = now - LEDGER_KEEP_DAYS * 86400
        for entry in self.sales.values():
            entry["sales"] = [s for s in entry.get("sales", [])
                              if s.get("ts", 0) >= sales_cutoff]
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.seen_path.write_text(json.dumps(self.seen))
            self.sales_path.write_text(json.dumps(self.sales))
        except OSError:
            pass  # ledger is best-effort; the scan itself must not fail

    # ------------------------------------------------------------------ #
    # observation
    # ------------------------------------------------------------------ #
    def observe(self, records: list[dict]) -> None:
        """Fold this run's listings into the watch list.

        Listings seen this run reset to misses=0; tracked listings that
        didn't show up get misses+1 (toward the confirmation threshold).
        """
        if self.mock_mode:
            return
        now = time.time()
        current_ids: set[str] = set()
        for rec in records:
            entry = self.sales.setdefault(
                rec["search_name"],
                {"sales": [], "first_seen": now, "observed": 0})
            entry["observed"] = entry.get("observed", 0) + 1
            item_id = rec["item_id"]
            current_ids.add(item_id)
            self.seen[item_id] = {
                "price": rec["price"],
                "bid_count": rec["bid_count"],
                "buying_options": rec["buying_options"],
                "search_name": rec["search_name"],
                "record_sales": rec["record_sales"],
                "last_seen": now,
                "misses": 0,  # seen this run; a reappearance clears churn
            }
        for item_id, info in self.seen.items():
            if item_id not in current_ids:
                info["misses"] = info.get("misses", 0) + 1

    def confirm_gone(self, is_active_fn) -> int:
        """Confirm disappearances; record clean sales. Returns sales added.

        A listing missing for MISSES_BEFORE_CONFIRM consecutive runs gets
        one is_active check (capped per run). Gone + was an auction with
        bids => confirmed sale at the last-seen price.
        """
        if self.mock_mode:
            return 0
        now = time.time()
        candidates = [i for i, s in self.seen.items()
                      if s.get("misses", 0) >= MISSES_BEFORE_CONFIRM]
        added = 0
        for item_id in candidates[:CONFIRM_CAP]:
            info = self.seen.get(item_id)
            if not info:
                continue
            try:
                alive = bool(is_active_fn(item_id))
            except Exception:
                continue  # failed check: leave it, retry next run
            if alive:
                info["misses"] = 0
                info["last_seen"] = now
                continue
            # Gone for real.
            if (info.get("record_sales")
                    and "AUCTION" in (info.get("buying_options") or [])
                    and info.get("bid_count", 0) > 0):
                entry = self.sales.setdefault(
                    info["search_name"],
                    {"sales": [], "first_seen": now, "observed": 0})
                entry["sales"].append({"ts": now, "price": info["price"]})
                added += 1
            del self.seen[item_id]
        return added

    # ------------------------------------------------------------------ #
    # market reference
    # ------------------------------------------------------------------ #
    def _recent_sales(self, search_name: str) -> list[float]:
        cutoff = time.time() - SALES_WINDOW_DAYS * 86400
        entry = self.sales.get(search_name, {})
        return [s["price"] for s in entry.get("sales", [])
                if s.get("ts", 0) >= cutoff]

    def median_for(self, search_name: str) -> tuple[float | None, int]:
        """(median sold price, sale count) — median None until MIN_SALES."""
        prices = self._recent_sales(search_name)
        if len(prices) < MIN_SALES:
            return None, len(prices)
        return round(statistics.median(prices), 2), len(prices)

    def is_liquid(self, search_name: str) -> bool:
        """False only when we positively know demand is dead.

        Dead = tracked for a while, watched plenty of listings, and still
        zero confirmed sales. Anything else counts as liquid — we can't
        prove nobody's buying.
        """
        entry = self.sales.get(search_name)
        if not entry:
            return True
        recent = self._recent_sales(search_name)
        if len(recent) >= MIN_SALES:
            return True
        days = (time.time() - entry.get("first_seen", time.time())) / 86400
        if (days >= DEAD_MIN_DAYS
                and entry.get("observed", 0) >= DEAD_MIN_OBSERVED
                and not recent):
            return False
        return True

    def summary(self) -> dict:
        """Small transparency stats for the published feed."""
        with_comps = sum(1 for name in self.sales
                         if self.median_for(name)[0] is not None)
        total = sum(len(self._recent_sales(name)) for name in self.sales)
        return {"searches_with_comps": with_comps, "recent_sales": total}
