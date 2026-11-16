from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


def _public_addresses(value: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError
        port = parsed.port or (443 if scheme == "https" else 80)
        try:
            addresses = (ipaddress.ip_address(host),)
        except ValueError:
            resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = tuple(dict.fromkeys(ipaddress.ip_address(item[4][0]) for item in resolved))
        if not addresses or any(
            not address.is_global or address.is_multicast or address.is_reserved
            or address.is_loopback or address.is_link_local or address.is_unspecified
            for address in addresses
        ):
            raise ValueError
        return addresses
    except (TypeError, ValueError, OSError, socket.gaierror) as error:
        raise ValueError("URL must use HTTP(S) and resolve only to public IP addresses") from error


def validate_remote_url(value: str) -> str:
    """Validate HTTP(S) and reject hosts with any non-public resolved address."""
    _public_addresses(value)
    return value


def pin_remote_url(value: str) -> tuple[str, str, str]:
    """Pin the validated address while preserving Host and TLS SNI validation."""
    addresses = _public_addresses(value)
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    pinned = str(addresses[0])
    if ":" in pinned:
        pinned = f"[{pinned}]"
    port = parsed.port
    netloc = pinned + (f":{port}" if port is not None else "")
    request_url = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
    host_header = parsed.netloc
    sni_hostname = host.encode("idna").decode("ascii") if parsed.scheme.lower() == "https" else ""
    return request_url, host_header, sni_hostname
