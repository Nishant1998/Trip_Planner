"""Run-local public source registration; never fetch source URLs directly."""
from hashlib import sha256
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict


def public_url(value: str) -> str:
    if len(value) > 2048 or any(ord(char) < 33 for char in value) or "\\" in value:
        raise ValueError("Invalid public URL")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("Public HTTP(S) sources only, without credentials")
    host = parts.hostname.lower().rstrip(".")
    if parts.port not in {None, 80, 443}:
        raise ValueError("Nonstandard source port")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ("." not in host or "%" in host or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".test", ".invalid"))
                or re.fullmatch(r"[0-9.]+", host) or any(label.startswith("0x") for label in host.split("."))):
            raise ValueError("Local or obfuscated source address")
        ascii_host = host.encode("idna").decode("ascii")
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) for label in ascii_host.split(".")):
            raise ValueError("Malformed source host")
    else:
        if not address.is_global:
            raise ValueError("Private or reserved source address")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    run_id: str
    entity_id: str
    field: str
    url: str
    title: str
    basis: str


class SourceRegistry:
    def __init__(self, run_id: str, known_entities: set[str]):
        self.run_id = run_id
        self.known_entities = frozenset(known_entities)
        self.sources: dict[str, Source] = {}

    def register(self, *, entity_id: str, field: str, url: str, title: str,
                 basis: str = "search_lead") -> Source:
        if entity_id not in self.known_entities:
            raise ValueError("Unregistered entity")
        url = public_url(url)
        digest = sha256(f"{self.run_id}\0{entity_id}\0{field}\0{url}".encode()).hexdigest()[:24]
        source = Source(id=f"source_{digest}", run_id=self.run_id, entity_id=entity_id,
                        field=field, url=url, title=title[:300], basis=basis)
        self.sources.setdefault(source.id, source)
        return self.sources[source.id]

    def read_target(self, source_id: str, *, entity_id: str, field: str) -> Source:
        source = self.sources.get(source_id)
        if source is None or (source.run_id, source.entity_id, source.field) != (self.run_id, entity_id, field):
            raise ValueError("Source does not belong to this run and gap")
        public_url(source.url)
        return source
