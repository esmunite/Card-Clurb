#!/usr/bin/env python3
"""PokéDeal Scanner — daily scan orchestrator.

    scan (eBay Browse API, mock until keys exist)
      -> score (scoring.py)
      -> publish (site/deals.json)

Usage:
    python3 scanner/run_scan.py [--priority 1] [--max-per-search 100] [--top 20]

Reads:
    scanner/tracked_searches.yaml
    env: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET (optional; absent = mock mode)
         EPN_CAMPAIGN_ID (optional; enables affiliate-tagged links)

Writes:
    site/deals.json   — feed consumed by the static site
    logs/scan-<ts>.log (when --log-dir given; stdout otherwise)

Exit codes: 0 ok (even with zero deals), 2 quota exhausted (keeps old feed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_dotenv() -> None:
    """Load project .env (600-perm secret file) into os.environ.

    Real environment variables take precedence; the file only fills gaps.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        text = env_path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

from ebay_client import EbayClient, QuotaExhaustedError  # noqa: E402
from market import MarketPricer  # noqa: E402
from sales_ledger import SalesLedger, record_from_listing  # noqa: E402
from scoring import (  # noqa: E402
    DEAL_THRESHOLD,
    MarketReference,
    is_deal,
    score_deal,
)

ROOT = Path(__file__).resolve().parent.parent
SEARCHES_FILE = ROOT / "scanner" / "tracked_searches.yaml"
DEALS_JSON = ROOT / "site" / "deals.json"


def load_searches(priority: int | None = None) -> list[dict]:
    with open(SEARCHES_FILE) as f:
        data = yaml.safe_load(f)
    searches = data.get("searches", [])
    if priority is not None:
        searches = [s for s in searches if s.get("priority", 2) <= priority]
    return searches


def run_scan(
    priority: int | None = None,
    max_per_search: int = 100,
    top_n: int = 40,
) -> dict:
    client = EbayClient()
    searches = load_searches(priority)

    # eBay-observed market prices: the sales ledger (built from our own
    # disappearance checks across runs) gives each search the median of its
    # recent confirmed eBay sales. Until a search has enough history, the
    # hand-set anchor is used and labeled "static-anchor". No third-party
    # price sites. The ledger updates AFTER this run's scan (one-run lag),
    # so pricing always rests on completed observations.
    ledger = SalesLedger(ROOT / "logs", mock_mode=client.mock_mode)
    ledger.load()
    pricer = MarketPricer(ledger, mock_mode=client.mock_mode)
    market = MarketReference()
    price_source: dict[str, str] = {}
    for s in searches:
        price, source = pricer.price_for(s)
        if price:
            market.prices[s["name"]] = price
            price_source[s["name"]] = source

    scored: list[dict] = []
    seen: dict[str, dict] = {}
    errors: list[str] = []
    skipped: list[str] = []
    scanned = 0
    ledger_records: list[dict] = []

    for s in searches:
        name, query = s["name"], s["query"]
        if not pricer.is_liquid(s):
            skipped.append(name)
            continue
        try:
            listings = client.search_all(query, max_items=max_per_search)
        except QuotaExhaustedError:
            # Stop the run but keep whatever we already scored; the
            # previously published feed stays live (see RUNBOOK.md).
            errors.append(f"quota exhausted during '{name}' — partial run")
            break
        except Exception as exc:  # noqa: BLE001 - per-search isolation
            errors.append(f"search '{name}' failed: {exc}")
            continue
        scanned += len(listings)
        for listing in listings:
            rec = record_from_listing(listing, s)
            if rec:
                ledger_records.append(rec)
            deal = score_deal(listing, market, name,
                             search_category=s.get("category", ""))
            if is_deal(deal):
                deal["url"] = EbayClient.affiliate_url(deal["url"])
                # Dedupe: the same listing can match multiple tracked searches;
                # keep its highest score.
                key = deal.get("item_id") or deal["url"]
                if key not in seen or deal["deal_score"] > seen[key]["deal_score"]:
                    seen[key] = deal

    scored = sorted(seen.values(), key=lambda d: d["deal_score"], reverse=True)

    # Sales ledger: fold this run's listings into the watch list, confirm
    # disappearances (gone auction-with-bids => confirmed eBay sale), and
    # persist for the next run. This is what market prices are built from.
    ledger.observe(ledger_records)
    sales_added = ledger.confirm_gone(client.is_active)
    ledger.save()

    # Sold-listing check: verify each candidate is still buyable, backfilling
    # from the next-best scored deals. A sold listing vanishes from search
    # results eventually, but this catches the ones that sold since the scan.
    # Detail calls are capped to stay inside the quota budget (see RUNBOOK).
    # Variety cap: no single tracked search can take more than 3 of the 40
    # slots, so one hot card can't crowd out everything else.
    MAX_VERIFY_CHECKS = 180
    MAX_PER_SEARCH = 5
    verified: list[dict] = []
    per_search: dict[str, int] = {}
    dropped = 0
    checks = 0
    pool = list(scored)  # highest score first
    while len(verified) < top_n and pool and checks < MAX_VERIFY_CHECKS:
        deal = pool.pop(0)
        sname = deal.get("search_name", "")
        if per_search.get(sname, 0) >= MAX_PER_SEARCH:
            continue  # variety cap — no API call spent on this one
        checks += 1
        item_id = deal.get("item_id") or ""
        try:
            alive = client.is_active(item_id) if item_id else True
        except Exception as exc:  # noqa: BLE001 - a failed check keeps the deal listed
            errors.append(f"verify '{str(deal.get('title', ''))[:40]}' failed: {exc}")
            alive = True
        if alive:
            verified.append(deal)
            per_search[sname] = per_search.get(sname, 0) + 1
        else:
            dropped += 1
    top = verified

    feed = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mock_mode": client.mock_mode,
        "searches_run": len(searches),
        "listings_scanned": scanned,
        "deals_found": len(scored),
        "verified_live": len(top),
        "verified_dropped": dropped,
        "verify_checks": checks,
        "skipped_low_demand": skipped,
        "price_source": price_source,
        "sales_ledger": ledger.summary() | {"new_sales_this_run": sales_added},
        "threshold": DEAL_THRESHOLD,
        "quota": client.quota.status(),
        "errors": errors,
        "deals": top,
    }
    DEALS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(DEALS_JSON, "w") as f:
        json.dump(feed, f, indent=2)
    return feed


def main() -> int:
    parser = argparse.ArgumentParser(description="PokéDeal Scanner daily run")
    parser.add_argument("--priority", type=int, default=None,
                        help="only run searches with priority <= N")
    parser.add_argument("--max-per-search", type=int, default=100)
    parser.add_argument("--top", type=int, default=150,
                        help="how many live deals to publish (default 150)")
    args = parser.parse_args()

    try:
        feed = run_scan(
            priority=args.priority,
            max_per_search=args.max_per_search,
            top_n=args.top,
        )
    except QuotaExhaustedError as exc:
        print(f"QUOTA EXHAUSTED: {exc} — keeping previous feed.", file=sys.stderr)
        return 2

    mode = "MOCK" if feed["mock_mode"] else "LIVE"
    print(
        f"[{mode}] searches={feed['searches_run']} "
        f"scanned={feed['listings_scanned']} deals={feed['deals_found']} "
        f"quota_used={feed['quota']['used']} -> {DEALS_JSON}"
    )
    for d in feed["deals"][:5]:
        print(f"  {d['deal_score']:>5}  {d['deal_type']:<14} ${d['price']}  {d['title'][:60]}")
    if feed["errors"]:
        print("errors:", file=sys.stderr)
        for e in feed["errors"]:
            print(f"  - {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
