"""Invo (app.invoapp.com) API client.

Invo has no public API docs. Endpoints and field meanings here are the ones
observed in app.invoapp.com's own network traffic and used by two existing
open-source projects:
  - bhevey/invo-mirror-bot   (Python, Invo -> Binance spot)
  - plungarini/invo-sentinel (TypeScript, Invo -> Hyperliquid perps)
If Invo changes its API, this is the one file that needs to change.

Auth model (same as invo-sentinel):
  - INVO_REFRESH_TOKEN: long-lived JWT. Exchanged for short-lived (~10 min)
    access tokens via GET /v1_0/auth/refresh_token.
  - INVO_ACCESS_TOKEN (optional): a short-lived access JWT copied from the
    browser. Used until it expires; after that we need the refresh token.
Both JWTs carry an `expires` (epoch seconds) claim that we decode locally
(no signature check -- we're only reading our own token's expiry).

Every failure to authenticate raises InvoAuthError with re-auth steps; it is
never swallowed into an empty result, since "no positions" and "couldn't
log in" must never look the same to the bot.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any

import requests

from ratelimit import SlidingWindowLimiter

log = logging.getLogger("invo.client")

BASE_URL = os.environ.get("INVO_API_BASE", "https://api.invoapp.com/v1_0")
APP_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://app.invoapp.com",
    "Referer": "https://app.invoapp.com/",
    "x-app-version": "0.0.75",
    "x-platform": "web",
}
TIMEOUT_S = 15
MAX_429_RETRIES = 3
MAX_429_SLEEP_S = 10.0
PAGE_SIZE = 50

REAUTH_INSTRUCTIONS = """
Invo authentication failed. To re-authenticate:
  1. Log in at https://app.invoapp.com in a desktop browser.
  2. Open DevTools (F12) -> Network tab, filter by "api.invoapp.com".
  3. Click around (open any portfolio) so requests appear.
  4. Find a request to  .../v1_0/auth/refresh_token  (or log out and back in
     to force one). Its request header  Authorization: Bearer <JWT>  is your
     REFRESH token. Alternatively: Application tab -> Local Storage ->
     https://app.invoapp.com -> look for a key containing "refresh".
  5. export INVO_REFRESH_TOKEN='<that JWT>'   (no "Bearer " prefix needed)
     Optionally also export INVO_ACCESS_TOKEN from any other request's
     Authorization header -- it only lives ~10 minutes.
Never commit these tokens. They grant access to your Invo account.
""".strip()


class InvoError(RuntimeError):
    pass


class InvoAuthError(InvoError):
    def __init__(self, reason: str):
        super().__init__(f"{reason}\n\n{REAUTH_INSTRUCTIONS}")
        self.reason = reason


class InvoRateLimitError(InvoError):
    pass


def _strip_bearer(tok: str | None) -> str | None:
    if not tok:
        return None
    tok = tok.strip()
    return tok[7:].strip() if tok.lower().startswith("bearer ") else tok


def jwt_expiry(token: str) -> float | None:
    """Return the `expires` (or standard `exp`) claim of a JWT, or None if undecodable."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("expires", payload.get("exp"))
        return float(exp) if exp is not None else None
    except Exception:
        return None


def jwt_user_id(token: str) -> str | None:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64)).get("user_id")
    except Exception:
        return None


def _decode_body(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Some Invo responses are base64-encoded JSON (observed by invo-sentinel).
    try:
        return json.loads(base64.b64decode(text))
    except Exception:
        return text


class InvoClient:
    def __init__(
        self,
        refresh_token: str | None = None,
        access_token: str | None = None,
        limiter: SlidingWindowLimiter | None = None,
        session: requests.Session | None = None,
    ):
        self.refresh_token = _strip_bearer(refresh_token or os.environ.get("INVO_REFRESH_TOKEN"))
        self.access_token = _strip_bearer(access_token or os.environ.get("INVO_ACCESS_TOKEN"))
        if not self.refresh_token and not self.access_token:
            raise InvoAuthError("No Invo credentials: neither INVO_REFRESH_TOKEN nor INVO_ACCESS_TOKEN is set.")
        budget = int(os.environ.get("INVO_RATE_LIMIT", "220"))
        self.limiter = limiter or SlidingWindowLimiter(budget, 300.0)
        self.http = session or requests.Session()
        self.calls_made = 0

    # ---- auth -------------------------------------------------------------

    def refresh_token_days_remaining(self) -> float | None:
        if not self.refresh_token:
            return None
        exp = jwt_expiry(self.refresh_token)
        return None if exp is None else (exp - time.time()) / 86400

    def _access_valid(self, margin_s: float = 30) -> bool:
        if not self.access_token:
            return False
        exp = jwt_expiry(self.access_token)
        # Undecodable token: let the server decide (a 401 triggers refresh).
        return exp is None or exp - time.time() > margin_s

    def refresh(self) -> None:
        if not self.refresh_token:
            raise InvoAuthError("Access token expired/rejected and INVO_REFRESH_TOKEN is not set.")
        exp = jwt_expiry(self.refresh_token)
        if exp is not None and exp < time.time():
            raise InvoAuthError(
                f"INVO_REFRESH_TOKEN expired on {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(exp))}."
            )
        self.limiter.acquire()
        self.calls_made += 1
        try:
            resp = self.http.get(
                f"{BASE_URL}/auth/refresh_token",
                headers={**APP_HEADERS, "Authorization": f"Bearer {self.refresh_token}"},
                timeout=TIMEOUT_S,
            )
        except requests.RequestException as e:
            raise InvoError(f"Network error refreshing Invo token: {e}") from e
        data = _decode_body(resp.text)
        if resp.status_code != 200 or not isinstance(data, dict) or not data.get("accessToken"):
            raise InvoAuthError(f"Token refresh rejected (HTTP {resp.status_code}): {str(data)[:200]}")
        self.access_token = _strip_bearer(data["accessToken"])
        if data.get("refreshToken"):  # rotate if the server issues a new one
            self.refresh_token = _strip_bearer(data["refreshToken"])
        log.info("Invo access token refreshed")

    def _ensure_token(self) -> None:
        if not self._access_valid():
            self.refresh()

    # ---- transport --------------------------------------------------------

    def post(self, path: str, body: dict) -> dict:
        self._ensure_token()
        retried_auth = False
        attempt_429 = 0
        while True:
            self.limiter.acquire()
            self.calls_made += 1
            try:
                resp = self.http.post(
                    f"{BASE_URL}/{path.lstrip('/')}",
                    headers={**APP_HEADERS, "Authorization": f"Bearer {self.access_token}"},
                    json=body,
                    timeout=TIMEOUT_S,
                )
            except requests.RequestException as e:
                raise InvoError(f"Network error on {path}: {e}") from e

            if resp.status_code == 401:
                if retried_auth:
                    raise InvoAuthError(f"{path} still 401 after a successful token refresh.")
                self.refresh()
                retried_auth = True
                continue
            if resp.status_code == 429:
                if attempt_429 >= MAX_429_RETRIES:
                    raise InvoRateLimitError(f"{path} still rate-limited after {MAX_429_RETRIES} retries")
                try:
                    delay = float(resp.headers.get("retry-after", ""))
                except ValueError:
                    delay = 2.0 ** attempt_429
                delay = min(delay, MAX_429_SLEEP_S)
                log.warning("Invo 429 on %s; sleeping %.1fs", path, delay)
                time.sleep(delay)
                attempt_429 += 1
                continue

            data = _decode_body(resp.text)
            if resp.status_code >= 400:
                raise InvoError(f"{path} HTTP {resp.status_code}: {str(data)[:300]}")
            if not isinstance(data, dict):
                raise InvoError(f"{path}: unexpected non-JSON response: {str(data)[:200]}")
            # Invo can return 200 with success:false -- check explicitly.
            if data.get("success") is False or data.get("error"):
                raise InvoError(f"{path} returned an error: {str(data.get('error'))[:300]}")
            return data

    # ---- endpoints --------------------------------------------------------

    def get_portfolio(self, portfolio_id: str) -> dict:
        data = self.post("portfolios/get_portfolio_by_id", {"portfolioId": portfolio_id})
        p = data.get("portfolio")
        if not isinstance(p, dict):
            raise InvoError(f"get_portfolio_by_id({portfolio_id}): response has no 'portfolio' object")
        return p

    def get_open_investments(self, portfolio_id: str) -> list[dict]:
        data = self.post(
            "investments/get_investments",
            {"portfolioId": portfolio_id, "isOpen": True, "params": {"page": 1, "size": PAGE_SIZE}},
        )
        return list(data.get("investmentsTicker") or [])

    def get_closed_investments_page(self, portfolio_id: str, page: int, size: int = PAGE_SIZE) -> list[dict]:
        data = self.post(
            "investments/get_investments",
            {"portfolioId": portfolio_id, "isOpen": False, "params": {"page": page, "size": size}},
        )
        return list(data.get("investmentsTicker") or [])

    def get_all_closed_investments(self, portfolio_id: str, max_pages: int = 40) -> tuple[list[dict], bool]:
        """Page through closed trades. Returns (trades, truncated).

        `truncated` is True if max_pages was hit before a short page -- the
        caller must surface that, since metrics then cover only the most
        recent part of the history.
        """
        out: list[dict] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            batch = self.get_closed_investments_page(portfolio_id, page)
            new = 0
            for inv in batch:
                key = str(inv.get("baseId") or inv.get("id") or id(inv))
                if key not in seen:
                    seen.add(key)
                    out.append(inv)
                    new += 1
            if len(batch) < PAGE_SIZE or new == 0:
                return out, False
        return out, True
