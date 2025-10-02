from __future__ import annotations

import httpx
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree


class TorznabClient:
    """
    Minimal Torznab client for Jackett-backed endpoints.
    The base_url must point to the Torznab endpoint root, e.g.:
      http://jackett:9117/api/v2.0/indexers/all/results/torznab
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._timeout = timeout

    async def caps_ok(self) -> bool:
        url = f"{self.base_url}/api?t=caps&apikey={self.api_key}"
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            r = await http.get(url)
            if r.status_code != 200:
                return False
            # Basic XML parse sanity check
            try:
                ElementTree.fromstring(r.text.encode("utf-8"))
                return True
            except Exception:
                return False

    async def search(self, query: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        query: {"title": str, "year": int|None, "media_type": "movie|tv|album|track", ...}
        Returns a list of normalized items compatible with Phelia's SearchProvider contract.
        """
        q = query.get("title") or ""
        if not q:
            return []

        url = f"{self.base_url}/api?t=search&apikey={self.api_key}&q={q}"
        results: List[Dict[str, Any]] = []

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            r = await http.get(url)
            r.raise_for_status()
            root = ElementTree.fromstring(r.text.encode("utf-8"))

            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                enclosure = item.find("enclosure")

                size = None
                if enclosure is not None:
                    # Torznab sets length in bytes (optional)
                    length = enclosure.attrib.get("length")
                    if length and length.isdigit():
                        size = int(length)
                    # Prefer enclosure URL for magnet/torrent
                    link = enclosure.attrib.get("url", link) or link

                seeders = None
                # Torznab custom attrs (seeders, peers, etc.)
                for attr in item.findall("{*}attr"):
                    if attr.attrib.get("name") == "seeders":
                        try:
                            seeders = int(attr.attrib.get("value", ""))
                        except Exception:
                            pass

                pubdate = item.findtext("pubDate")

                results.append(
                    {
                        "title": title,
                        "link": link,                    # magnet or torrent URL
                        "size_bytes": size,
                        "seeders": seeders,
                        "pubdate": pubdate,
                        "provider": "jackett",
                    }
                )
        return results

