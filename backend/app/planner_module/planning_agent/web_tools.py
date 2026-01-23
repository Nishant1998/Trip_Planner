"""Bounded Tavily retrieval; observations never become verified claims here."""
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import json
from time import monotonic

import httpx

from .resources import ResourceGuard, ResourceLimit
from .web_sources import Source, SourceRegistry, public_url
from .contracts import fingerprint


class PlannerWebTools:
    def __init__(self, *, api_key: str, registry: SourceRegistry,
                 guard: ResourceGuard, gaps: dict[str, dict], transport=None, checkpoint=None):
        self.api_key = api_key
        self.registry = registry
        self.guard = guard
        # Scope and query text are supplied by preparation, not model arguments.
        self.gaps = deepcopy(gaps)
        self.transport = transport
        self.cache: dict[str, dict] = {}
        self.retained_characters = 0
        self.lock = asyncio.Lock()
        self.checkpoint = checkpoint

    def snapshot(self):
        return {"run_id": self.registry.run_id, "scope_hash": fingerprint(self.gaps),
                "sources": [source.model_dump(mode="json") for source in self.registry.sources.values()],
                "cache": deepcopy(self.cache), "retained_characters": self.retained_characters}

    def restore(self, saved):
        if saved["run_id"] != self.registry.run_id or saved["scope_hash"] != fingerprint(self.gaps):
            raise ValueError("Saved web state belongs to another run or scope")
        sources = {}
        for raw in saved["sources"]:
            source = Source.model_validate(raw)
            if source.run_id != self.registry.run_id or source.entity_id not in self.registry.known_entities:
                raise ValueError("Saved source is outside run scope")
            public_url(source.url)
            sources[source.id] = source
        retained = saved["retained_characters"]
        if not isinstance(retained, int) or not 0 <= retained <= 18_000:
            raise ValueError("Invalid retained web context counter")
        self.registry.sources = sources
        self.cache = deepcopy(saved["cache"])
        self.retained_characters = retained

    async def _request(self, kind, body):
        if not self.api_key:
            return {"status": "unavailable", "reason": "missing_tavily_key"}
        try:
            timeout = await self.guard.reserve(kind)
        except ResourceLimit as exc:
            return {"status": "limited", "reason": str(exc)}
        if self.checkpoint:
            # Persistence failure is not a provider error and must stop dispatch.
            await self.checkpoint()
        started = monotonic()
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                                            follow_redirects=False, timeout=timeout) as client:
                    async with client.stream(
                        "POST", "https://api.tavily.com/" + ("search" if kind == "search" else "extract"),
                        headers={"Authorization": f"Bearer {self.api_key}"}, json=body,
                    ) as response:
                        if not 200 <= response.status_code < 300:
                            return {"status": "provider_error", "http_status": response.status_code}
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 512_000:
                                return {"status": "unavailable", "reason": "response_size_limit"}
                        payload = json.loads(content)
                        if not isinstance(payload, dict):
                            raise ValueError("Invalid provider envelope")
                        return {"status": "ok", "payload": payload}
        except (TimeoutError, httpx.TimeoutException):
            return {"status": "timeout"}
        except (httpx.HTTPError, ValueError):
            return {"status": "provider_error", "reason": "invalid_or_unavailable_response"}
        finally:
            await self.guard.reconcile_web(timeout, monotonic() - started)

    async def search(self, gap_id: str):
        return await self._retrieve(gap_id, None)

    async def read(self, gap_id: str, source_id: str):
        return await self._retrieve(gap_id, source_id)

    async def _retrieve(self, gap_id, source_id):
        async with self.lock:
            gap = self.gaps.get(gap_id)
            if gap is None or gap.get("entity_id") not in self.registry.known_entities:
                return {"status": "rejected", "reason": "unknown_gap"}
            if gap.get("resolved"):
                return {"status": "already_covered", "gap_id": gap_id}
            source = None
            if source_id is not None:
                try:
                    source = self.registry.read_target(source_id, entity_id=gap["entity_id"], field=gap["field"])
                except ValueError:
                    return {"status": "rejected", "reason": "unregistered_source"}
            key = f"{gap_id}\0{source_id}"
            if key in self.cache:
                return {**deepcopy(self.cache[key]), "cache_hit": True}
            if self.retained_characters >= 18_000:
                return {"status": "limited", "reason": "web_context_limit"}
            query = gap["query"]
            body = ({"query": query, "search_depth": "basic", "max_results": 3,
                     "auto_parameters": False, "include_answer": False,
                     "include_raw_content": False, "include_images": False}
                    if source is None else
                    {"urls": [source.url], "extract_depth": "basic", "format": "text"})
            self.cache[key] = {"status": "unknown", "reason": "request_in_flight_or_interrupted", "gap_id": gap_id}
            result = await self._request("search" if source is None else "extract", body)
            output = {"gap_id": gap_id, "retrieved_at": datetime.now(UTC).isoformat(),
                      "cache_hit": False, "untrusted": True, **result}
            payload = output.pop("payload", {})
            if result["status"] == "ok":
                output["items"] = []
                records = payload.get("results", [])
                if not isinstance(records, list):
                    output.update(status="provider_error", reason="invalid_results")
                    records = []
                for record in records[:3 if source is None else 1]:
                    if not isinstance(record, dict):
                        continue
                    try:
                        url = public_url(record.get("url", ""))
                        if source and url != source.url:
                            # A redirect's identity/applicability has not been established.
                            continue
                        registered = self.registry.register(entity_id=gap["entity_id"], field=gap["field"],
                                                            url=url, title=str(record.get("title", "")))
                    except (ValueError, TypeError):
                        continue
                    text = record.get("content" if source is None else "raw_content")
                    if not isinstance(text, str) or not text.strip():
                        continue
                    retained = text[:min(1200 if source is None else 6000, 18_000 - self.retained_characters)]
                    self.retained_characters += len(retained)
                    digest = sha256(retained.encode()).hexdigest()
                    output["items"].append({"source_id": registered.id, "url": url,
                        "text": retained, "content_hash": digest, "chunk_id": f"chunk_{digest[:24]}",
                        "truncated": len(retained) < len(text),
                        "basis": "search_lead" if source is None else "extracted_excerpt",
                        "limitations": "Extraction may omit qualifiers; content is not verified evidence."})
                if not output["items"] and output["status"] == "ok":
                    output.update(status="unavailable", reason="no_usable_results")
            # Failed identical requests are retained too: no automatic paid retries.
            self.cache[key] = deepcopy(output)
            return output
