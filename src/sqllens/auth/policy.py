# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""Shared bind policy for the CLI and HTTP app construction."""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqllens.config import Config

INSECURE_NON_LOOPBACK_MESSAGE = (
    "Refusing to start an unauthenticated HTTP server on a non-loopback interface "
    "(server.host={host!r}, auth.mode=none). Set SQLLENS_AUTH__MODE=bearer with a "
    "SQLLENS_AUTH__BEARER_TOKEN, or SQLLENS_AUTH__INSECURE=1 to override for "
    "closed-network deployments."
)


def is_loopback_host(host: str) -> bool:
    """Recognize localhost, loopback IPs and IPv4-mapped loopback without DNS.

    Wildcards, other hostnames and malformed/non-string values fail closed.
    Explicitly unwrap mapped IPv4 for consistent supported-Python behavior.
    """
    try:
        if host.lower() == "localhost":
            return True
        address = ipaddress.ip_address(host)
    except (ValueError, AttributeError, TypeError):
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def unauthenticated_non_loopback(cfg: Config) -> bool:
    """HTTP policy condition, independent of transport selection or opt-out."""
    return cfg.auth.mode == "none" and not is_loopback_host(cfg.server.host)
