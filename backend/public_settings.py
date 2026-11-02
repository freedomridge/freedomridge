"""Public-demo limits, validated once before the API opens its database pool."""

import ipaddress
import math
import os
from dataclasses import dataclass


def positive_number(name, default, *, integer=False):
    try:
        value = int(os.getenv(name, str(default))) if integer else float(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f'{name} must be a positive {"integer" if integer else "number"}') from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be finite and positive')
    return value


def ip_allowlist():
    networks = []
    for entry in os.getenv('FRIDGE_DEMO_IP_ALLOWLIST', '').replace(',', ' ').split():
        try:
            network = ipaddress.ip_network(entry, strict=True)
        except ValueError as error:
            raise ValueError(f'FRIDGE_DEMO_IP_ALLOWLIST invalid entry {entry!r}: {error}') from None
        if network.prefixlen == 0:
            raise ValueError(f'FRIDGE_DEMO_IP_ALLOWLIST invalid entry {entry!r}: /0 is not allowed')
        networks.append(network)
    return tuple(networks)


@dataclass(frozen=True)
class PublicSettings:
    guest_statement_seconds: float = 30
    acquire_seconds: float = 5
    demo_account_cap: int = 2000
    demo_ip_hourly: int = 5
    demo_ip_live: int = 10
    demo_ip_allowlist: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    guest_view_bytes: int = 2 * 1024 * 1024
    guest_view_count: int = 200
    guest_concurrency: int = 2
    guest_queue_seconds: float = 10

    def ip_allowlisted(self, client_ip: str) -> bool:
        try:
            address = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        return any(address in network for network in self.demo_ip_allowlist)


settings = PublicSettings()


def load_settings():
    global settings
    settings = PublicSettings(
        guest_statement_seconds=positive_number('FRIDGE_GUEST_STATEMENT_TIMEOUT_SECONDS', 30),
        acquire_seconds=positive_number('FRIDGE_DB_ACQUIRE_TIMEOUT_SECONDS', 5),
        demo_account_cap=positive_number('FRIDGE_DEMO_ACCOUNT_CAP', 2000, integer=True),
        demo_ip_hourly=positive_number('FRIDGE_DEMO_IP_HOURLY_LIMIT', 5, integer=True),
        demo_ip_live=positive_number('FRIDGE_DEMO_IP_LIVE_CAP', 10, integer=True),
        demo_ip_allowlist=ip_allowlist(),
        guest_view_bytes=positive_number('FRIDGE_GUEST_VIEW_TOTAL_BYTES', 2 * 1024 * 1024, integer=True),
        guest_view_count=positive_number('FRIDGE_GUEST_VIEW_COUNT', 200, integer=True),
        guest_concurrency=positive_number('FRIDGE_GUEST_CONCURRENCY', 2, integer=True),
        guest_queue_seconds=positive_number('FRIDGE_GUEST_QUEUE_WAIT_SECONDS', 10),
    )
    # PostgreSQL statement_timeout is a signed 32-bit count of milliseconds.
    if not 0.001 <= settings.guest_statement_seconds <= 2147483.647:
        raise ValueError('FRIDGE_GUEST_STATEMENT_TIMEOUT_SECONDS must be between 0.001 and 2147483.647')
    return settings
