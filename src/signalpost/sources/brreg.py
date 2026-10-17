"""Adapter for the Brønnøysund Register Centre (Brønnøysundregistrene) open APIs.

Endpoints (from Brreg's official documentation, licence NLOD 2.0, no API key):
  GET {base}/enheter/{orgnr}             main legal entity
  GET {base}/underenheter/{orgnr}        sub-entity (branch / business unit)
  GET {base}/enheter/{orgnr}/roller      roles (board, CEO, auditor ...)
  GET {base}/enheter?page=&size=&...     search; results under _embedded.enheter,
                                         (page+1)*size must not exceed 10,000
  GET {regnskap}/{orgnr}                 key figures from latest approved annual accounts
                                         (Regnskapsregisteret; documented by Brreg as a
                                         temporary R&D API that may change)
Struck-off entities return 200 with fewer fields; legally removed ones return 410.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..http import BudgetedClient, HttpResult, SourceUnavailable
from .base import LookupResult, SourceRecord

ENHETSREGISTERET = "brreg:enhetsregisteret"
REGNSKAPSREGISTERET = "brreg:regnskapsregisteret"


class MalformedResponse(SourceUnavailable):
    """The source answered, but not with the documented structure."""


class BrregSource:
    name = ENHETSREGISTERET

    def __init__(self, client: BudgetedClient) -> None:
        self.client = client
        self.base = client.settings.enhetsregisteret_base.rstrip("/")
        self.regnskap_base = client.settings.regnskap_base.rstrip("/")

    # -- single entity --------------------------------------------------------
    def lookup(self, orgnr: str) -> LookupResult:
        """Look up a main entity, falling back to the sub-entity register."""
        for kind, path in (("enhet", "enheter"), ("underenhet", "underenheter")):
            url = f"{self.base}/{path}/{orgnr}"
            res = self.client.get_json(url)
            if res.status == 200:
                if not isinstance(res.body, dict):
                    raise MalformedResponse(f"{url}: expected a JSON object")
                return LookupResult("found", self._record(kind, orgnr, res.body, url, res))
            if res.status == 410:
                return LookupResult(
                    "removed", detail=f"{url} returned 410 Gone (entity removed from the register)"
                )
            if res.status not in (404, 400):
                raise SourceUnavailable(f"{url}: unexpected HTTP {res.status}")
        return LookupResult("not_found", detail="no entity or sub-entity with this number")

    def roles(self, orgnr: str) -> SourceRecord | None:
        url = f"{self.base}/enheter/{orgnr}/roller"
        res = self.client.get_json(url)
        if res.status in (404, 410):
            return None
        if res.status != 200 or not isinstance(res.body, dict):
            raise MalformedResponse(f"{url}: HTTP {res.status} / unexpected body")
        return SourceRecord(ENHETSREGISTERET, "roles", orgnr, res.body, url, res.url,
                            res.retrieved_at, _last_modified(res))

    def accounts(self, orgnr: str) -> SourceRecord | None:
        url = f"{self.regnskap_base}/{orgnr}"
        res = self.client.get_json(url)
        if res.status in (404, 410, 204):
            return None
        if res.status != 200 or not isinstance(res.body, list):
            raise MalformedResponse(f"{url}: HTTP {res.status} / expected a JSON array")
        return SourceRecord(REGNSKAPSREGISTERET, "accounts", orgnr, res.body, url, res.url,
                            res.retrieved_at, _last_modified(res))

    # -- search / batch ------------------------------------------------------
    def search_pages(
        self, params: dict[str, Any], page_size: int = 100, start_page: int = 0
    ) -> Iterator[tuple[int, list[SourceRecord], dict[str, Any]]]:
        """Yield (page_number, records, page_info) for an entity search."""
        page = start_page
        while (page + 1) * page_size <= 10_000:
            url = f"{self.base}/enheter"
            res = self.client.get_json(url, {**params, "page": page, "size": page_size})
            if res.status != 200 or not isinstance(res.body, dict):
                raise MalformedResponse(f"{res.url}: HTTP {res.status} / unexpected body")
            items = (res.body.get("_embedded") or {}).get("enheter") or []
            info = res.body.get("page") or {}
            records = []
            for item in items:
                orgnr = str(item.get("organisasjonsnummer", "")) if isinstance(item, dict) else ""
                if not orgnr:
                    continue  # skipped; counted as failure by the caller via len difference
                records.append(
                    self._record("enhet", orgnr, item, f"{self.base}/enheter/{orgnr}", res)
                )
            yield page, records, {**info, "raw_items": len(items)}
            total_pages = info.get("totalPages")
            if not items or (isinstance(total_pages, int) and page + 1 >= total_pages):
                return
            page += 1

    def batch_lookup(self, orgnrs: list[str]) -> list[SourceRecord]:
        """Fetch up to 100 entities in one search request filtered by organisation number.

        Results are NOT trusted to match the request: the caller's identity gate
        drops anything whose number was not asked for.
        """
        if not orgnrs:
            return []
        url = f"{self.base}/enheter"
        res = self.client.get_json(
            url, {"organisasjonsnummer": ",".join(orgnrs), "size": len(orgnrs)}, use_cache=False
        )
        if res.status != 200 or not isinstance(res.body, dict):
            raise MalformedResponse(f"{res.url}: HTTP {res.status} / unexpected body")
        out = []
        for item in (res.body.get("_embedded") or {}).get("enheter") or []:
            if isinstance(item, dict) and item.get("organisasjonsnummer"):
                n = str(item["organisasjonsnummer"])
                out.append(self._record("enhet", n, item, f"{self.base}/enheter/{n}", res))
        return out

    # -- helpers -------------------------------------------------------------
    def _record(self, kind: str, orgnr: str, body: dict, canonical: str, res: HttpResult) -> SourceRecord:
        self_href = ((body.get("_links") or {}).get("self") or {}).get("href")
        if isinstance(self_href, str) and self_href.startswith("https://"):
            canonical = self_href
        return SourceRecord(ENHETSREGISTERET, kind, orgnr, body, canonical, res.url,
                            res.retrieved_at, _last_modified(res))


def _last_modified(res: HttpResult) -> str | None:
    return res.headers.get("last-modified")
