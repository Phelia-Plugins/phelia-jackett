from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter

from .torznab import TorznabClient


class JackettSearchProvider:
    """
    Adapter implementing your core's SearchProvider protocol:
      async def search(self, query: dict) -> list[dict]
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self._client = TorznabClient(base_url=base_url, api_key=api_key)

    async def search(self, query: Dict[str, Any]):
        return await self._client.search(query)


class Plugin:
    """
    Entry point class required by your plugin loader.
    Expected lifecycle hooks (optional): post_install(ctx), on_enable(ctx), on_disable(ctx)
    The 'ctx' dict is provided by Phelia core and should expose:
      - register_route(path: str, router: APIRouter)
      - register_search_provider(provider)
      - register_settings_panel(plugin_id: str, schema: dict)
      - settings_store (with get/set methods or callables)
      - notify(message: str)
    """

    # Static identifiers — make sure they match your registry manifest
    PLUGIN_ID = "phelia.jackett"
    SETTINGS_NS = "phelia.jackett"

    # Default Torznab endpoint used by Jackett "All" indexers
    DEFAULT_TORZNAB_BASE = "http://jackett:9117/api/v2.0/indexers/all/results/torznab"

    # Settings schema rendered by core Settings UI
    SETTINGS_SCHEMA = {
        "title": "Jackett",
        "description": "Configure Jackett Torznab endpoint.",
        "type": "object",
        "properties": {
            "base_url": {
                "type": "string",
                "title": "Torznab base URL",
                "description": "e.g. http://jackett:9117/api/v2.0/indexers/all/results/torznab",
                "default": DEFAULT_TORZNAB_BASE,
            },
            "api_key": {
                "type": "string",
                "title": "API key",
                "format": "password",
                "description": "Jackett API key from ServerConfig.json",
            },
            "use_all_indexers": {
                "type": "boolean",
                "title": "Use 'All' indexers feed",
                "default": True,
            },
        },
        "required": ["base_url", "api_key"],
    }

    def __init__(self) -> None:
        self._ctx: Dict[str, Any] | None = None
        self._provider: JackettSearchProvider | None = None
        self._router = APIRouter()

        # Simple debug/health endpoint mounted under /plugins/{id}
        @self._router.get("/health")
        async def health():
            return {"ok": True, "plugin": self.PLUGIN_ID}

    # ------------- lifecycle hooks -------------

    def post_install(self, ctx: Dict[str, Any]) -> None:
        """
        Attempt automatic discovery:
        - Detect base_url if not stored yet (try default hostnames).
        - Import API key from Jackett's ServerConfig.json if the config dir is mounted.
        - Persist settings to the namespaced plugin settings store.
        """
        self._ctx = ctx
        settings = self._get_settings(ctx)

        # Try to prefill base_url if missing
        if not settings.get("base_url"):
            # Default guess for Docker bridge DNS
            settings["base_url"] = self.DEFAULT_TORZNAB_BASE

        # Try to auto-import API key from mounted config
        if not settings.get("api_key"):
            key = self._try_read_api_key(ctx)
            if key:
                settings["api_key"] = key

        # Persist possibly updated settings
        self._save_settings(ctx, settings)

        # Register settings panel on first install as well
        self._register_settings_panel(ctx)

        # Optionally notify UI
        self._notify(ctx, "Jackett plugin installed. Configure API key in Settings if not auto-detected.")

        # Mount plugin router
        self._register_routes(ctx)

    async def on_enable(self, ctx: Dict[str, Any]) -> None:
        """
        Validate configuration and register the SearchProvider with the core.
        """
        self._ctx = ctx
        settings = self._get_settings(ctx)
        base = (settings.get("base_url") or "").strip()
        api_key = (settings.get("api_key") or "").strip()

        if not base or not api_key:
            self._notify(ctx, "Jackett plugin not configured. Please set base URL and API key in Settings.")
            return

        client = TorznabClient(base_url=base, api_key=api_key)
        ok = await client.caps_ok()
        if not ok:
            self._notify(ctx, "Jackett Torznab 'caps' check failed. Verify base URL and API key.")
            return

        self._provider = JackettSearchProvider(base_url=base, api_key=api_key)
        ctx["register_search_provider"](self._provider)
        self._notify(ctx, "Jackett search provider enabled.")

        # Mount routes if not yet mounted (idempotent)
        self._register_routes(ctx)
        self._register_settings_panel(ctx)

    def on_disable(self, ctx: Dict[str, Any]) -> None:
        """
        No persistent hooks needed here; core should drop providers/routes for disabled plugins.
        """
        self._notify(ctx, "Jackett plugin disabled.")

    # ------------- helper methods -------------

    def _get_settings(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """
        Read namespaced settings from the core-provided settings store.
        The store may be an object with get/set, or callables in ctx.
        """
        store = ctx.get("settings_store")
        if hasattr(store, "get"):
            return dict(store.get(self.SETTINGS_NS) or {})
        get_fn = ctx.get("settings_get")
        if callable(get_fn):
            return dict(get_fn(self.SETTINGS_NS) or {})
        return {}

    def _save_settings(self, ctx: Dict[str, Any], values: Dict[str, Any]) -> None:
        """
        Persist namespaced settings back to the store.
        """
        store = ctx.get("settings_store")
        if hasattr(store, "set"):
            store.set(self.SETTINGS_NS, values)
            return
        set_fn = ctx.get("settings_set")
        if callable(set_fn):
            set_fn(self.SETTINGS_NS, values)

    def _notify(self, ctx: Dict[str, Any], message: str) -> None:
        """
        Send a transient UI notification if available.
        """
        notify = ctx.get("notify")
        if callable(notify):
            try:
                notify(message)
            except Exception:
                pass

    def _register_routes(self, ctx: Dict[str, Any]) -> None:
        """
        Mount this plugin's router under /plugins/{plugin_id}.
        The core is responsible for prefixing with the plugin id.
        """
        register = ctx.get("register_route")
        if callable(register):
            try:
                register("/health", self._router)
            except Exception:
                pass

    def _register_settings_panel(self, ctx: Dict[str, Any]) -> None:
        """
        Tell the core to render our panel on the main Settings page.
        """
        reg = ctx.get("register_settings_panel")
        if callable(reg):
            try:
                reg(self.PLUGIN_ID, self.SETTINGS_SCHEMA)
            except Exception:
                pass

    def _try_read_api_key(self, ctx: Dict[str, Any]) -> Optional[str]:
        """
        Try to read Jackett API key from ServerConfig.json if a config dir is mounted.
        Priority:
          1) ctx["mounts"]["jackett_config"]
          2) env JACKETT_CONFIG_DIR
          3) /config (common container default)
        """
        mounts = ctx.get("mounts") or {}
        candidates = [
            mounts.get("jackett_config"),
            os.environ.get("JACKETT_CONFIG_DIR"),
            "/config",
        ]
        for root in [p for p in candidates if p]:
            cfg = Path(root) / "Jackett" / "ServerConfig.json"
            if cfg.exists():
                try:
                    data = json.loads(cfg.read_text(encoding="utf-8"))
                    key = data.get("APIKey") or data.get("ApiKey") or data.get("api_key")
                    if key and isinstance(key, str) and len(key) >= 8:
                        return key
                except Exception:
                    continue
        return None

