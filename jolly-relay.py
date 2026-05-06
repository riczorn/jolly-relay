#!/usr/bin/env python3
"""
Jolly Relay - async SMTP relay server

Accepts inbound SMTP connections from authorised Postfix hosts and
delivers mail to the appropriate external MX server selected by the
routing rules in the configuration file, or forwards inbound mail to
the configured local delivery MTA.

Usage:
    python3 jolly-relay.py [options]

Options:
    -c, --config FILE    Path to configuration file (default: jolly-relay.yaml)
    -p, --port PORT      Port to listen on (default: 9725)
    -H, --host HOST      Host to bind to (default: 127.0.0.1)
    --cache-ttl SEC      Cache TTL in seconds (default: 3600; 0 disables)
    --timeout SEC        Client inactivity timeout in seconds (default: 600)
    -v, --verbose        Increase verbosity level of logging

Configuration File Format:
    See jolly-relay.yaml.example for a fully annotated example.
"""

import os
import time
import asyncio

os.chdir(os.path.dirname(os.path.abspath(__file__)))

import aiodns
import src.config as cfg
from src.service import RelayService

config = cfg.Config()

# Shared DNS resolver — created once when the event loop starts.
_dns_resolver = None


def get_dns_resolver():
    global _dns_resolver
    if _dns_resolver is None:
        _dns_resolver = aiodns.DNSResolver()
    return _dns_resolver


# ── DNS / MX Cache ───────────────────────────────────────────────────

async def get_mx_records(domain, cache_ttl):
    """Return (mx_records, from_cache) for domain. Non-blocking via aiodns."""
    from src.config import log

    current_time = time.time()

    with service.cache_lock:
        if cache_ttl > 0 and domain in service.mx_cache:
            cache_time, mx_records = service.mx_cache[domain]
            if current_time - cache_time < cache_ttl:
                return mx_records, True

    try:
        answers = await asyncio.wait_for(
            get_dns_resolver().query(domain, 'MX'),
            timeout=3.0,
        )
        mx_records = sorted(answers, key=lambda r: r.priority)
        mx_records = [r.host.rstrip('.').lower() for r in mx_records]
    except asyncio.TimeoutError:
        log(f"WARNING: DNS timeout resolving MX for '{domain}'", to_stderr=True)
        mx_records = []
    except aiodns.error.DNSError as e:
        # NXDOMAIN (4) and NODATA (0) are normal; anything else is worth logging
        if e.args[0] not in (0, 4):
            log(f"WARNING: DNS error resolving MX for '{domain}': {e}", to_stderr=True)
        mx_records = []
    except Exception as e:
        log(f"WARNING: Unexpected error resolving MX for '{domain}': {e}", to_stderr=True)
        mx_records = []

    if cache_ttl > 0:
        with service.cache_lock:
            # Enforce size cap: evict the oldest entry if at limit
            cache_max = config.cache_max_size
            if cache_max > 0 and len(service.mx_cache) >= cache_max and domain not in service.mx_cache:
                oldest = min(service.mx_cache, key=lambda k: service.mx_cache[k][0])
                del service.mx_cache[oldest]
            service.mx_cache[domain] = (current_time, mx_records)

    return mx_records, False


# ── Business Logic ───────────────────────────────────────────────────

async def get_mx_for_message(sender, recipient, cache_ttl):
    """
    Main routing function.  Returns (Server, group_name) or (None, 'n/a').
    """
    sender_result = "n/a"
    recipient_result = "n/a"

    if sender:
        sender_result, _ = await get_rule_match_for_email(sender, cache_ttl, rule_type="sender_rules")

    if recipient:
        recipient_result, _ = await get_rule_match_for_email(recipient, cache_ttl, rule_type="recipient_rules")

    # 1. Combined rules take highest priority
    combined_key = f"{sender_result},{recipient_result}"
    if config.combined_rule_groups and combined_key in config.combined_rule_groups:
        servers_obj = config.combined_rule_groups[combined_key]
        server = servers_obj.get_next()
        return server, f"combined:{combined_key}"

    # 2. Recipient rule fallback
    if recipient_result and recipient_result != "n/a":
        server, group = pick_server_for_group(recipient_result)
        if server and server != "NO RESULT":
            return server, group

    # 3. Sender rule fallback
    if sender_result and sender_result != "n/a":
        server, group = pick_server_for_group(sender_result)
        if server and server != "NO RESULT":
            return server, group

    # 4. servers.default
    if config.servers_default_obj:
        server = config.servers_default_obj.get_next()
        if server:
            return server, "default"

    return None, "n/a"


async def get_rule_match_for_email(email, cache_ttl, rule_type):
    mx_server_group = False
    default = False
    domain = email.split('@')[1] if '@' in email else ''

    if domain:
        if rule_type != "sender_rules":
            mx_records, _ = await get_mx_records(domain, cache_ttl)
            for mx in mx_records:
                mx_server_group, default = config.test_domain_rules(email, mx, rule_type=rule_type)
                if mx_server_group:
                    break

        if not mx_server_group:
            mx_server_group, default = config.test_domain_rules(email, domain, rule_type=rule_type)

    if not mx_server_group:
        mx_server_group = default if default else "n/a"

    return mx_server_group, default


def pick_server_for_group(mx_server_group):
    if mx_server_group == 'NO RESULT':
        return None, mx_server_group

    if not mx_server_group or mx_server_group == "n/a":
        return None, None

    servers_obj = config.get_server_group(mx_server_group)
    if not servers_obj:
        return None, mx_server_group

    server = servers_obj.get_next(mx_server_group)
    return server, mx_server_group


# ── Entry Point ──────────────────────────────────────────────────────

service = RelayService(config, get_mx_for_message)


def main():
    cfg.config = config
    service.run()


if __name__ == "__main__":
    main()
