"""
Routing logic for Jolly Relay.

get_mx_for_message()        — main routing entry point
get_rule_match_for_email()  — evaluate sender/recipient rules
pick_server_for_group()     — resolve a group name to a Server
get_mx_records()            — async DNS MX lookup with cache
"""

import asyncio
import time

import aiodns

from src.logging import log

# Module-level DNS resolver, created once when the event loop starts.
_dns_resolver = None


def get_dns_resolver():
    global _dns_resolver
    if _dns_resolver is None:
        _dns_resolver = aiodns.DNSResolver()
    return _dns_resolver


async def get_mx_records(domain, service):
    """
    Return (mx_records, from_cache) for domain. Non-blocking via aiodns.

    Args:
        domain:  The domain name to query.
        service: RelayService instance (holds mx_cache, cache_lock, config).
    """
    cache_ttl = service.config.cache_ttl
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
        # NXDOMAIN (4) and NODATA (0) are normal; log anything else.
        if e.args[0] not in (0, 4):
            log(f"WARNING: DNS error resolving MX for '{domain}': {e}", to_stderr=True)
        mx_records = []
    except Exception as e:
        log(f"WARNING: Unexpected error resolving MX for '{domain}': {e}", to_stderr=True)
        mx_records = []

    if cache_ttl > 0:
        with service.cache_lock:
            cache_max = service.config.cache_max_size
            if cache_max > 0 and len(service.mx_cache) >= cache_max and domain not in service.mx_cache:
                oldest = min(service.mx_cache, key=lambda k: service.mx_cache[k][0])
                del service.mx_cache[oldest]
            service.mx_cache[domain] = (current_time, mx_records)

    return mx_records, False


async def get_mx_for_message(sender, recipient, config, service):
    """
    Main routing function. Returns (Server, group_name) or (None, 'n/a').

    Args:
        sender:    Envelope sender address.
        recipient: Envelope recipient address.
        config:    Config instance.
        service:   RelayService instance.
    """
    sender_result = "n/a"
    recipient_result = "n/a"

    if sender:
        sender_result, _ = await _get_rule_match(sender, config, service, rule_type="sender_rules")

    if recipient:
        recipient_result, _ = await _get_rule_match(recipient, config, service, rule_type="recipient_rules")

    # 1. Combined rules — highest priority
    combined_key = f"{sender_result},{recipient_result}"
    if config.combined_rule_groups and combined_key in config.combined_rule_groups:
        servers_obj = config.combined_rule_groups[combined_key]
        server = servers_obj.get_next()
        return server, f"combined:{combined_key}"

    # 2. Recipient rule fallback
    if recipient_result and recipient_result != "n/a":
        server, group = _pick_server_for_group(recipient_result, config)
        if server:
            return server, group

    # 3. Sender rule fallback
    if sender_result and sender_result != "n/a":
        server, group = _pick_server_for_group(sender_result, config)
        if server:
            return server, group

    # 4. servers.default
    if config.servers_default_obj:
        server = config.servers_default_obj.get_next()
        if server:
            return server, "default"

    return None, "n/a"


async def _get_rule_match(email, config, service, rule_type):
    """Evaluate sender or recipient rules for a single address."""
    mx_server_group = False
    default = False
    domain = email.split('@')[1] if '@' in email else ''

    if domain:
        if rule_type != "sender_rules":
            mx_records, _ = await get_mx_records(domain, service)
            for mx in mx_records:
                mx_server_group, default = config.test_domain_rules(email, mx, rule_type=rule_type)
                if mx_server_group:
                    break

        if not mx_server_group:
            mx_server_group, default = config.test_domain_rules(email, domain, rule_type=rule_type)

    if not mx_server_group:
        mx_server_group = default if default else "n/a"

    return mx_server_group, default


def _pick_server_for_group(mx_server_group, config):
    if not mx_server_group or mx_server_group in ('n/a', 'NO RESULT'):
        return None, mx_server_group

    servers_obj = config.get_server_group(mx_server_group)
    if not servers_obj:
        return None, mx_server_group

    server = servers_obj.get_next()
    return server, mx_server_group
