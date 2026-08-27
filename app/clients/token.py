"""Shared Schwab token provider.

Implements the same shared-token mechanism MM uses (its env vars):

    SCHWAB_AUTH_MODE        "shared" -> fetch the access token over HTTP.
    SCHWAB_TOKEN_URL        token endpoint (defaults to MM's /auth/token).
    SCHWAB_TOKEN_SHARE_KEY  bearer key presented to that endpoint.

    GET <token_url>   Authorization: Bearer <share_key>
    -> { "access_token": "...", "expires_in": 1800 }   (token/accessToken also accepted)

The resulting access token is what the Schwab client uses for market data and
order routing, so this 0DTE app shares MM's single Schwab authorization.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger("schwab.token")


class SharedTokenProvider:
    def __init__(self, timeout: float = 8.0):
        self.mode = settings.schwab_auth_mode
        self.url = settings.token_url
        self.key = settings.token_share_key
        self.client = httpx.Client(timeout=timeout)
        self._token: Optional[str] = None
        self._exp: float = 0.0

    def invalidate(self) -> None:
        """Drop the cached token so the next get_token() re-fetches.

        Called when Schwab returns 401 (the shared token rotated under us)."""
        self._token = None
        self._exp = 0.0

    def get_token(self) -> Optional[str]:
        if self.mode != "shared":
            log.error("SCHWAB_AUTH_MODE=%s unsupported (only 'shared' implemented)", self.mode)
            return None
        if self._token and time.time() < self._exp:
            return self._token
        if not self.key:
            log.error("SCHWAB_TOKEN_SHARE_KEY (or MM_API_KEY) not set — cannot fetch shared token")
            return None
        try:
            r = self.client.get(self.url, headers={"Authorization": f"Bearer {self.key}"})
            if r.status_code != 200:
                log.error("shared token %s -> HTTP %s", self.url, r.status_code)
                return None
            j = r.json()
            tok = j.get("access_token") or j.get("token") or j.get("accessToken")
            if not tok:
                log.error("shared token response missing access_token: keys=%s", list(j.keys()))
                return None
            self._token = tok
            ttl = float(j.get("expires_in", 600))
            self._exp = time.time() + max(30.0, ttl - 30.0)
            log.info("shared Schwab token acquired (ttl=%ss)", int(ttl))
            return tok
        except Exception as exc:  # pragma: no cover - network
            log.error("shared token fetch failed: %s", exc)
            return None
