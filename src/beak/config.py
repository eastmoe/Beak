from __future__ import annotations

import ipaddress
import json
import socket
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError

from .schemas import RenderRequest


class ConfigError(RuntimeError):
    pass


class UrlSafetyError(ValueError):
    pass


class DomainRule(BaseModel):
    patterns: list[str] = Field(default_factory=list)
    defaults: dict = Field(default_factory=dict)


class UrlSafetyPolicy(BaseModel):
    enabled: bool = False
    allowed_schemes: list[str] = Field(default_factory=lambda: ["http", "https"])
    allowed_domains: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)
    allow_ip_hosts: bool = True
    block_private_hosts: bool = False
    block_loopback_hosts: bool = False
    block_link_local_hosts: bool = False
    block_multicast_hosts: bool = True
    resolve_dns: bool = False
    max_url_length: int = 4096


class BeakConfig(BaseModel):
    domain_rules: list[DomainRule] = Field(default_factory=list)
    safety: UrlSafetyPolicy = Field(default_factory=UrlSafetyPolicy)


class ConfigManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.config = self._load(path)

    @classmethod
    def from_paths(cls, *, explicit_path: str | None, data_dir: Path) -> ConfigManager:
        path = Path(explicit_path).expanduser() if explicit_path else cls.default_path()
        if explicit_path is None:
            cls.ensure_default_file(path)
        return cls(path)

    @staticmethod
    def default_path() -> Path:
        return Path.home() / ".beak" / "config.json"

    @property
    def is_configured(self) -> bool:
        return self.path.exists()

    def read_text(self) -> str:
        if not self.path.exists():
            return self.default_text()
        return self.path.read_text(encoding="utf-8")

    def replace_text(self, content: str) -> None:
        try:
            data = json.loads(content)
            config = BeakConfig.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ConfigError(f"Invalid Beak config: {exc}") from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self._format_config(config), encoding="utf-8")
        self.config = config

    def apply(self, request: RenderRequest) -> RenderRequest:
        merged = request
        explicit_fields = set(request.model_fields_set)
        for rule in self.config.domain_rules:
            if _host_matches_any(_host_from_url(str(merged.url)), rule.patterns):
                merged = self._apply_rule_defaults(merged, explicit_fields, rule.defaults)
        self.validate_url(str(merged.url))
        return merged

    def validate_url(self, url: str) -> None:
        policy = self.config.safety
        if not policy.enabled:
            return

        if len(url) > policy.max_url_length:
            raise UrlSafetyError(f"URL exceeds max_url_length={policy.max_url_length}.")

        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in {item.lower() for item in policy.allowed_schemes}:
            raise UrlSafetyError(f"URL scheme is not allowed: {scheme}")

        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            raise UrlSafetyError("URL host is required.")
        if policy.denied_domains and _host_matches_any(host, policy.denied_domains):
            raise UrlSafetyError(f"URL host is denied by policy: {host}")
        if policy.allowed_domains and not _host_matches_any(host, policy.allowed_domains):
            raise UrlSafetyError(f"URL host is not in allowed_domains: {host}")

        ip = _parse_ip(host)
        if ip is not None:
            self._validate_ip(host, ip, policy)
            return
        if policy.resolve_dns:
            for resolved in _resolve_host(host):
                self._validate_ip(host, resolved, policy)

    def _apply_rule_defaults(
        self,
        request: RenderRequest,
        explicit_fields: set[str],
        defaults: dict,
    ) -> RenderRequest:
        data = request.model_dump(mode="json")
        for field, value in defaults.items():
            if field not in RenderRequest.model_fields or field in explicit_fields:
                continue
            data[field] = value
        try:
            return RenderRequest.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(f"Invalid domain rule defaults for {request.url}: {exc}") from exc

    @staticmethod
    def _validate_ip(host: str, ip: ipaddress.IPv4Address | ipaddress.IPv6Address, policy: UrlSafetyPolicy) -> None:
        if not policy.allow_ip_hosts and _parse_ip(host) is not None:
            raise UrlSafetyError(f"IP literal hosts are not allowed: {host}")
        if policy.block_loopback_hosts and ip.is_loopback:
            raise UrlSafetyError(f"Loopback hosts are blocked: {host}")
        if policy.block_private_hosts and ip.is_private:
            raise UrlSafetyError(f"Private network hosts are blocked: {host}")
        if policy.block_link_local_hosts and ip.is_link_local:
            raise UrlSafetyError(f"Link-local hosts are blocked: {host}")
        if policy.block_multicast_hosts and ip.is_multicast:
            raise UrlSafetyError(f"Multicast hosts are blocked: {host}")

    @staticmethod
    def _load(path: Path) -> BeakConfig:
        if not path.exists():
            return BeakConfig()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return BeakConfig.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise ConfigError(f"Failed to load Beak config from {path}: {exc}") from exc

    @classmethod
    def ensure_default_file(cls, path: Path) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cls.default_text(), encoding="utf-8")

    @classmethod
    def default_text(cls) -> str:
        return cls._format_config(BeakConfig())

    @staticmethod
    def _format_config(config: BeakConfig) -> str:
        return json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"


def _host_from_url(url: str) -> str:
    return (urlparse(url).hostname or "").strip().lower().rstrip(".")


def _host_matches_any(host: str, patterns: list[str]) -> bool:
    return any(_host_matches(host, pattern) for pattern in patterns)


def _host_matches(host: str, pattern: str) -> bool:
    normalized = pattern.strip().lower().rstrip(".")
    if not normalized:
        return False
    if normalized.startswith("*."):
        suffix = normalized[2:]
        return host == suffix or host.endswith(f".{suffix}")
    if "*" in normalized:
        return fnmatch(host, normalized)
    return host == normalized


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            ip = _parse_ip(str(sockaddr[0]))
            if ip is not None:
                addresses.append(ip)
    except socket.gaierror:
        return []
    return addresses
