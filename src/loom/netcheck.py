"""Outbound-URL validation for user-configurable endpoints.

The compression LLM endpoint (``compression.llm_url``) is mutable at runtime
via ``PATCH /api/config/compression``, which makes it an SSRF vector: a caller
who can reach the config API could point the gateway at a cloud metadata
service or an internal host and read the response through compression output.

Policy: scheme must be http/https, and every resolved address must be either
loopback (always trusted — the documented default is a local Ollama) or a
globally routable address. Private/link-local/metadata ranges require the
explicit ``allow_private_llm_url`` opt-in, which homelab users pointing at a
LAN Ollama are expected to set.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    pass


def validate_outbound_url(url: str, allow_private: bool = False) -> None:
    """Raise UnsafeURLError unless ``url`` passes the outbound policy.

    Resolution happens here, so a hostname that maps to a blocked range is
    caught even when the literal URL looks harmless.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"unsupported scheme {parsed.scheme!r} (http/https only)")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    try:
        infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve host {host!r}: {exc}") from exc

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_loopback:
            continue
        # Link-local (incl. 169.254.169.254 cloud metadata) is never allowed,
        # even with the private opt-in — no legitimate LLM endpoint lives there.
        if addr.is_link_local:
            raise UnsafeURLError(
                f"host {host!r} resolves to link-local address {addr}"
            )
        if allow_private and addr.is_private:
            continue
        if not addr.is_global:
            raise UnsafeURLError(
                f"host {host!r} resolves to non-public address {addr} "
                "(set compression.allow_private_llm_url to allow LAN endpoints)"
            )
