from __future__ import annotations

from typing import Any

from .sqlite_store import RuntimeStorageMixin

LIST_ANI_CACHE_KEY = "ani_rss:list_ani:v1"


class PluginCacheMixin(RuntimeStorageMixin):
    async def cached_list_ani(self) -> list[dict[str, Any]]:
        ttl = self.subscription_cache_ttl_seconds()
        if ttl > 0:
            cached = self.runtime_store().get_cache(LIST_ANI_CACHE_KEY)
            if isinstance(cached, list):
                return [item for item in cached if isinstance(item, dict)]

        data = await self.client(require_api_key=True).list_ani()
        if ttl > 0:
            self.runtime_store().set_cache(LIST_ANI_CACHE_KEY, data, ttl)
        return data

    def invalidate_subscription_cache(self) -> None:
        self.runtime_store().delete_cache(LIST_ANI_CACHE_KEY)
