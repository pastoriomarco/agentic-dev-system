"""Helpers for outbound URL validation and worker egress policy checks."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)
LINK_LOCAL_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)
LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)
METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
}


class NetworkPolicyError(ValueError):
    """Raised when a configured outbound destination violates network policy."""


@dataclass(frozen=True)
class URLRouteDecision:
    host: str
    route: str
    resolved_ips: tuple[str, ...]
    is_private_like: bool


def normalize_host(value: str) -> str:
    return value.strip().lower().strip("[]")


def parse_host_patterns(raw_value: str) -> tuple[str, ...]:
    parts = []
    for item in raw_value.split(","):
        normalized = normalize_host(item)
        if normalized:
            parts.append(normalized)
    return tuple(dict.fromkeys(parts))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(normalize_host(host))
        return True
    except ValueError:
        return False


def host_matches_allowlist(host: str, patterns: Iterable[str]) -> bool:
    normalized = normalize_host(host)
    for pattern in patterns:
        if not pattern:
            continue
        candidate = normalize_host(pattern)
        if candidate.startswith("."):
            suffix = candidate[1:]
            if normalized == suffix or normalized.endswith(candidate):
                return True
        elif normalized == candidate:
            return True
    return False


def host_matches_no_proxy(host: str, patterns: Iterable[str]) -> bool:
    normalized = normalize_host(host)
    for pattern in patterns:
        if not pattern:
            continue
        candidate = normalize_host(pattern)
        if candidate == "*":
            return True
        if _is_ip_literal(candidate):
            if normalized == candidate:
                return True
            continue
        if candidate.startswith("."):
            suffix = candidate[1:]
            if normalized == suffix or normalized.endswith(candidate):
                return True
            continue
        if normalized == candidate or normalized.endswith("." + candidate):
            return True
    return False


def parse_url_host(url: str, *, context: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise NetworkPolicyError(f"{context} must use http or https.")
    if not parsed.hostname:
        raise NetworkPolicyError(f"{context} must include a hostname.")
    return normalize_host(parsed.hostname)


def parse_proxy_url(url: str, *, context: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise NetworkPolicyError(f"{context} must use http or https.")
    if not parsed.hostname:
        raise NetworkPolicyError(f"{context} must include a hostname.")
    return normalize_host(parsed.hostname)


def resolve_hostname_ips(host: str) -> tuple[ipaddress._BaseAddress, ...]:
    normalized = normalize_host(host)
    try:
        return (ipaddress.ip_address(normalized),)
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(normalized, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise NetworkPolicyError(f"Unable to resolve host '{normalized}': {exc}") from exc

    addresses: list[ipaddress._BaseAddress] = []
    seen: set[str] = set()
    for family, _, _, _, sockaddr in infos:
        if family == socket.AF_INET:
            ip_text = sockaddr[0]
        elif family == socket.AF_INET6:
            ip_text = sockaddr[0]
        else:
            continue
        if ip_text in seen:
            continue
        seen.add(ip_text)
        addresses.append(ipaddress.ip_address(ip_text))
    if not addresses:
        raise NetworkPolicyError(f"Unable to resolve host '{normalized}' to an IP address.")
    return tuple(addresses)


def _is_in_networks(ip: ipaddress._BaseAddress, networks: Iterable[ipaddress._BaseNetwork]) -> bool:
    return any(ip in network for network in networks)


def _classify_ips(ips: Iterable[ipaddress._BaseAddress]) -> dict[str, bool]:
    resolved = tuple(ips)
    has_metadata = any(ip in METADATA_IPS for ip in resolved)
    has_link_local = any(_is_in_networks(ip, LINK_LOCAL_NETWORKS) for ip in resolved)
    has_loopback = any(_is_in_networks(ip, LOOPBACK_NETWORKS) for ip in resolved)
    has_rfc1918 = any(_is_in_networks(ip, RFC1918_NETWORKS) for ip in resolved)
    return {
        "has_metadata": has_metadata,
        "has_link_local": has_link_local,
        "has_loopback": has_loopback,
        "has_rfc1918": has_rfc1918,
        "is_private_like": has_loopback or has_rfc1918,
    }


def validate_public_http_url(url: str, *, context: str) -> str:
    host = parse_url_host(url, context=context)
    resolved_ips = resolve_hostname_ips(host)
    flags = _classify_ips(resolved_ips)
    if flags["has_metadata"]:
        raise NetworkPolicyError(f"{context} may not target metadata addresses.")
    if flags["has_link_local"]:
        raise NetworkPolicyError(f"{context} may not target link-local addresses.")
    if flags["is_private_like"]:
        raise NetworkPolicyError(f"{context} may not target loopback or RFC1918/private addresses.")
    return host


def validate_llm_endpoint(
    url: str,
    *,
    allowlisted_hosts: Iterable[str],
    no_proxy_hosts: Iterable[str],
) -> URLRouteDecision:
    host = parse_url_host(url, context="LLM_API_URL")
    if not host_matches_allowlist(host, allowlisted_hosts):
        raise NetworkPolicyError(f"LLM_API_URL host '{host}' is not present in LLM_HOST_ALLOWLIST.")

    resolved_ips = resolve_hostname_ips(host)
    flags = _classify_ips(resolved_ips)
    if flags["has_metadata"]:
        raise NetworkPolicyError("LLM_API_URL may not target metadata addresses.")
    if flags["has_link_local"]:
        raise NetworkPolicyError("LLM_API_URL may not target link-local addresses.")

    route = "direct" if host_matches_no_proxy(host, no_proxy_hosts) else "proxy"
    if flags["is_private_like"] and route != "direct":
        raise NetworkPolicyError(
            f"Private/loopback LLM host '{host}' must be listed in WORKER_NO_PROXY for direct routing."
        )
    if not flags["is_private_like"] and route != "proxy":
        raise NetworkPolicyError(
            f"Public LLM host '{host}' must not bypass the egress proxy through WORKER_NO_PROXY."
        )

    return URLRouteDecision(
        host=host,
        route=route,
        resolved_ips=tuple(str(ip) for ip in resolved_ips),
        is_private_like=flags["is_private_like"],
    )


def load_squid_allowed_domains(config_path: Path) -> tuple[str, ...]:
    if not config_path.exists():
        raise NetworkPolicyError(f"Squid config file not found: {config_path}")

    entries: list[str] = []
    collecting = False
    for raw_line in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if collecting or line.startswith("acl allowed_domains dstdomain"):
            if not collecting:
                line = line.split("dstdomain", 1)[1].strip()
            if line.endswith("\\"):
                collecting = True
                line = line[:-1].strip()
            else:
                collecting = False
            if line:
                entries.extend(token.strip() for token in line.split() if token.strip())
    if not entries:
        raise NetworkPolicyError(f"No allowed_domains ACL entries found in {config_path}.")
    return tuple(entries)


def host_allowed_by_squid(host: str, allowed_domains: Iterable[str]) -> bool:
    normalized = normalize_host(host)
    for pattern in allowed_domains:
        candidate = normalize_host(pattern)
        if candidate.startswith("."):
            suffix = candidate[1:]
            if normalized == suffix or normalized.endswith(candidate):
                return True
        elif normalized == candidate:
            return True
    return False
