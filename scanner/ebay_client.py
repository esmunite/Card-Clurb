"""eBay Browse API client with strict quota discipline and mock mode.

Secrets come from (precedence order):
    1. environment variables: EBAY_CLIENT_ID (App ID), EBAY_CLIENT_SECRET (Cert ID)
    2. locked secrets file ~/.config/pokedeal/secrets.env (must be 0600),
       with KEY=VALUE lines. Read at runtime only, by this process only.
       Never commit it, never log its values.

When neither source has both keys, the client runs in MOCK MODE.

When either is absent the client runs in MOCK MODE: no network calls,
deterministic fake listings from mock_data.py. This lets the whole
pipeline (scan -> score -> publish) be developed and smoke-tested
with $0 spend and zero credentials.

Quota model (verified against eBay's free developer tier):
    - ~5,000 Browse API calls/day per application, shared across
      item_summary/search and get_item.
    - Every response carries X-RateLimit-Remaining; we read it and
      treat the provider's number as authoritative.
    - A local QuotaTracker enforces a conservative internal ceiling so
      one runaway job can never burn the daily budget.

Token flow: OAuth 2.0 client credentials grant.
    POST https://api.ebay.com/identity/v1/oauth2/token
    Authorization: Basic base64(CLIENT_ID:CLIENT_SECRET)
    grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope
Tokens are cached until expiry (minus a 60s safety margin).
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

try:
    from .mock_data import mock_search_results
except ImportError:  # run as a top-level script (run_scan.py), not as a package
    from mock_data import mock_search_results

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
BROWSE_ITEM_URL = "https://api.ebay.com/buy/browse/v1/item"

# eBay free-tier daily allowance for the Browse API (calls, not dollars).
DAILY_CALL_BUDGET = 5000
# Internal safety ceiling: never let one process use more than this per day.
# The daily scan is budgeted far below this (see RUNBOOK.md).
INTERNAL_DAILY_CAP = 1500

EBAY_CLIENT_ID = "EBAY_CLIENT_ID"
EBAY_CLIENT_SECRET = "EBAY_CLIENT_SECRET"

SECRETS_FILE = os.path.expanduser("~/.config/pokedeal/secrets.env")


def _load_local_secrets() -> None:
    """Seed missing secret env vars from the locked local secrets file.

    Why a file: the scheduler has no secret store, and the Secure Vault
    only serves approval-gated browser logins (its values are never
    readable by code), so unattended scan runs cannot use either. This
    file — owner-read-only, read at runtime only — is the one path left.

    Rules: file must be 0600 (or 0400); anything looser is ignored with
    a warning. Environment variables always win over the file. Values
    are never logged.
    """
    try:
        st = os.stat(SECRETS_FILE)
    except OSError:
        return  # no file: env-only or mock mode, behavior unchanged
    if stat.S_IMODE(st.st_mode) not in (0o400, 0o600):
        print(
            f"warning: ignoring {SECRETS_FILE}: permissions must be 600 "
            "(owner read-only). Fix with: chmod 600 " + SECRETS_FILE,
            flush=True,
        )
        return
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        print(f"warning: could not read {SECRETS_FILE}: {exc}", flush=True)
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


class EbayAPIError(RuntimeError):
    """Non-recoverable eBay API error (auth failure, bad request, ...)."""


class QuotaExhaustedError(RuntimeError):
    """Raised when the daily call budget is exhausted."""


class RateLimitedError(RuntimeError):
    """Raised on HTTP 429 when retries are exhausted."""


@dataclass
class QuotaTracker:
    """Tracks Browse API usage against the daily budget.

    `provider_remaining` is updated from the X-RateLimit-Remaining header
    whenever eBay sends one; the provider's number wins over our counter.
    """

    daily_budget: int = DAILY_CALL_BUDGET
    internal_cap: int = INTERNAL_DAILY_CAP
    used: int = 0
    provider_remaining: int | None = None

    def _effective_remaining(self) -> int:
        remaining = self.daily_budget - self.used
        if self.provider_remaining is not None:
            remaining = min(remaining, self.provider_remaining)
        remaining = min(remaining, self.internal_cap - self.used)
        return max(remaining, 0)

    def check(self, calls: int = 1) -> None:
        if self._effective_remaining() < calls:
            raise QuotaExhaustedError(
                f"Daily eBay API budget exhausted: used={self.used}, "
                f"provider_remaining={self.provider_remaining}, "
                f"budget={self.daily_budget}"
            )

    def record(self, calls: int = 1, response_headers: dict | None = None) -> None:
        self.used += calls
        if response_headers:
            raw = response_headers.get("X-RateLimit-Remaining")
            if raw is not None:
                try:
                    self.provider_remaining = int(raw)
                except (TypeError, ValueError):
                    pass

    def status(self) -> dict:
        return {
            "used": self.used,
            "provider_remaining": self.provider_remaining,
            "effective_remaining": self._effective_remaining(),
            "daily_budget": self.daily_budget,
        }


@dataclass
class EbayClient:
    """Thin wrapper around the eBay Browse API.

    Mock mode is automatic: if EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not
    both present in the environment, every call returns mock data and no
    HTTP request is ever made.
    """

    marketplace: str = "EBAY_US"
    quota: QuotaTracker = field(default_factory=QuotaTracker)
    timeout: int = 20

    def __post_init__(self) -> None:
        _load_local_secrets()  # locked file seeds env only for unset vars
        self.client_id = os.environ.get(EBAY_CLIENT_ID, "")
        self.client_secret = os.environ.get(EBAY_CLIENT_SECRET, "")
        self.mock_mode = not (self.client_id and self.client_secret)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------------ #
    # auth
    # ------------------------------------------------------------------ #
    def _fetch_token(self) -> None:
        """OAuth client-credentials grant. Mock mode: no-op."""
        if self.mock_mode:
            self._token = "mock-token"
            self._token_expires_at = time.time() + 3600
            return
        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            }
        ).encode()
        req = urllib.request.Request(
            TOKEN_URL,
            data=body,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001 - surface as domain error
            raise EbayAPIError(f"eBay OAuth token request failed: {exc}") from exc
        self._token = payload["access_token"]
        # expires_in is seconds; keep a 60s safety margin.
        self._token_expires_at = time.time() + int(payload.get("expires_in", 7200)) - 60

    def _auth_headers(self) -> dict:
        if not self._token or time.time() >= self._token_expires_at:
            self._fetch_token()
        return {
            "Authorization": f"Bearer {self._token}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    # low-level GET with 429 backoff
    # ------------------------------------------------------------------ #
    def _get(self, url: str, params: dict | None = None, retries: int = 3) -> tuple[dict, dict]:
        """GET with exponential backoff on 429. Returns (payload, headers)."""
        query = urllib.parse.urlencode(params or {})
        full_url = f"{url}?{query}" if query else url
        backoff = 2.0
        for attempt in range(retries + 1):
            self.quota.check()
            req = urllib.request.Request(full_url, headers=self._auth_headers())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    headers = dict(resp.headers.items())
                    payload = json.loads(resp.read().decode())
                self.quota.record(response_headers=headers)
                return payload, headers
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < retries:
                    # Daily-quota 429s never recover in seconds, but per-second
                    # throttles do; back off and retry a bounded number of times.
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                if exc.code == 429:
                    raise RateLimitedError("eBay rate limit retries exhausted") from exc
                raise EbayAPIError(f"eBay API HTTP {exc.code}: {exc.read()[:300]}") from exc
        raise RateLimitedError("eBay rate limit retries exhausted")  # pragma: no cover

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def search(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
        filters: dict | None = None,
    ) -> dict:
        """Single page of item_summary/search. Returns the raw payload.

        In mock mode returns fabricated listings shaped like the real API.
        """
        if self.mock_mode:
            self.quota.record()
            return mock_search_results(query, limit=limit, offset=offset)
        params = {"q": query, "limit": min(limit, 200), "offset": offset}
        if filters:
            # eBay filter syntax, e.g. {"buyingOptions": ["FIXED_PRICE"], ...}
            params["filter"] = ",".join(f"{k}:{{{','.join(v)}}}" for k, v in filters.items())
        payload, _ = self._get(BROWSE_SEARCH_URL, params)
        return payload

    def search_all(self, query: str, max_items: int = 200, page_size: int = 50) -> list[dict]:
        """Paginated search; stops at max_items or end of results."""
        items: list[dict] = []
        offset = 0
        while len(items) < max_items:
            page = self.search(query, limit=page_size, offset=offset)
            summaries = page.get("itemSummaries", [])
            if not summaries:
                break
            items.extend(summaries)
            total = page.get("total", 0)
            offset += len(summaries)
            if offset >= total or len(summaries) < page_size:
                break
        return items[:max_items]

    def get_item(self, item_id: str) -> dict:
        """Full item detail (1 call). Used sparingly for disappearance checks."""
        if self.mock_mode:
            self.quota.record()
            return {"itemId": item_id, "mock": True}
        payload, _ = self._get(f"{BROWSE_ITEM_URL}/{urllib.parse.quote(item_id, safe='')}")
        return payload

    def is_active(self, item_id: str) -> bool:
        """True if the listing is still buyable.

        Ended/sold listings either 404 or come back without buying options.
        A failed check for any other reason raises (caller decides).
        Mock mode: always True (no network).
        """
        if self.mock_mode:
            self.quota.record()
            return True
        try:
            item = self.get_item(item_id)
        except EbayAPIError as exc:
            if "404" in str(exc):
                return False
            raise
        return bool(item.get("buyingOptions"))

    @staticmethod
    def affiliate_url(item_url: str) -> str:
        """Append EPN tracking. Campaign id comes from env, never hardcoded.

        Set EPN_CAMPAIGN_ID once the eBay Partner Network account is approved.
        Without it, links are plain (no commission) but still work.
        """
        campaign = os.environ.get("EPN_CAMPAIGN_ID", "").strip()
        if not campaign or not item_url:
            return item_url
        sep = "&" if "?" in item_url else "?"
        return f"{item_url}{sep}mkcid=1&mkrid=711-53200-19255-0&campid={campaign}"
