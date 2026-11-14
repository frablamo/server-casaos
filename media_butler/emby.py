"""Emby library refresh."""

from __future__ import annotations

import logging

import httpx

from .config import EmbyConfig

log = logging.getLogger(__name__)


class EmbyClient:
    def __init__(self, config: EmbyConfig) -> None:
        self.config = config

    async def refresh(self) -> bool:
        if not self.config.enabled:
            return True
        url = f"{self.config.url}/Library/Refresh"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    url,
                    headers={"X-Emby-Token": self.config.api_key},
                )
                response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - refresh must never break a move
            log.warning("could not refresh Emby (%s): %s", url, exc)
            return False
        log.info("Emby refreshed")
        return True
