"""
RelayService: aiosmtpd-based SMTP relay handler.

Accepts inbound SMTP connections from authorised hosts, determines
direction (inbound / outbound) and routes the message:
  - OUTBOUND: routing logic selects an external MX server; delivered
              via aiosmtplib.
  - INBOUND:  forwarded as-is to the configured local_delivery host.

On temporary delivery failure a 4xx response is returned to the
sender so Postfix can retry.
"""

import asyncio
import os
import time
import signal
import threading

import psutil
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP as SMTPProtocol

from src.config import log, log_debug, log_to_file, log_request

GC_INTERVAL = 3600
STATS_INTERVAL = 300


class RelayHandler:
    """
    aiosmtpd message handler.

    One instance is shared across all connections; aiosmtpd calls
    handle_DATA for every accepted message.
    """

    def __init__(self, config, mx_router, relay_service):
        """
        Args:
            config:          Config instance
            mx_router:       callable(sender, recipient, cache_ttl) -> (mx_address, group)
            relay_service:   RelayService instance (for cache access / stats)
        """
        self.config = config
        self.mx_router = mx_router
        self.relay_service = relay_service

    # ── aiosmtpd hooks ───────────────────────────────────────────────

    async def handle_RCPT(self, server, _session, envelope, address, _rcpt_options):
        if '@' not in address:
            return '501 Invalid recipient address'
        envelope.rcpt_tos.append(address)
        return '250 OK'

    async def handle_DATA(self, server, session, envelope):
        """Route the message once the DATA phase is complete."""
        client_ip = session.peer[0] if session.peer else ''
        sender = (envelope.mail_from or '').lower()
        recipients = [r.lower() for r in envelope.rcpt_tos]

        # For multi-recipient messages each recipient may route differently.
        # We process them individually and collect any temporary failures.
        temp_failed = []
        perm_failed = []

        for recipient in recipients:
            code, msg = await self._route_one(sender, recipient, envelope, client_ip)
            if code >= 500:
                perm_failed.append((recipient, code, msg))
            elif code >= 400:
                temp_failed.append((recipient, code, msg))

        if temp_failed:
            # 4xx → Postfix will retry
            details = "; ".join(f"{r}: {m}" for r, _, m in temp_failed)
            return f"451 Temporary failure for some recipients: {details}"

        if perm_failed:
            details = "; ".join(f"{r}: {m}" for r, _, m in perm_failed)
            return f"550 Permanent failure for some recipients: {details}"

        return '250 Message accepted for delivery'

    # ── Routing logic ─────────────────────────────────────────────────

    async def _route_one(self, sender, recipient, envelope, client_ip):
        """
        Determine direction and deliver one recipient.
        Returns (smtp_code, description).
        """
        recipient_domain = recipient.split('@')[-1] if '@' in recipient else recipient

        if self.config.is_local_domain(recipient_domain):
            direction = "INCOMING"
            return await self._deliver_local(sender, recipient, envelope, client_ip, direction)
        else:
            direction = "OUTGOING"
            return await self._deliver_outbound(sender, recipient, envelope, client_ip, direction)

    async def _deliver_local(self, sender, recipient, envelope, client_ip, direction):
        """Forward to the configured local delivery MTA."""
        host = self.config.local_delivery_host
        port = self.config.local_delivery_port
        mx_label = f"{host}:{port}"

        log_debug(f"  LOCAL {sender} -> {recipient} via {host}:{port}")

        code, msg = await self._smtp_send(
            host, port, sender, [recipient], envelope.content
        )

        self.config.print_csv(sender, recipient, "local", mx_label,
                               direction=direction, client_address=client_ip)
        self.config.send_to_graylog(sender, recipient, "local", mx_label,
                                    direction=direction, client_address=client_ip)
        log_request(sender, recipient, "local", mx_label,
                    f"{code} {msg}", envelope, direction=direction,
                    client_address=client_ip)

        return code, msg

    async def _deliver_outbound(self, sender, recipient, envelope, client_ip, direction):
        """Select an external MX and deliver."""
        mx_server, group = self.mx_router(sender, recipient, self.config.cache_ttl)

        if not mx_server:
            self.config.print_csv(sender, recipient, group or "n/a", "n/a",
                                  direction=direction, client_address=client_ip)
            log_request(sender, recipient, group or "n/a", "n/a",
                        "no route", envelope,
                        direction=direction, client_address=client_ip)
            return 451, "No route found for this message"

        host = mx_server.host
        port = mx_server.port
        mx_label = f"{host}:{port}"

        log_debug(f"  OUTBOUND {sender} -> {recipient} via {mx_label} (group:{group})")

        code, msg = await self._smtp_send(
            host, port, sender, [recipient], envelope.content
        )

        self.config.print_csv(sender, recipient, group, mx_label,
                               direction=direction, client_address=client_ip)
        self.config.send_to_graylog(sender, recipient, group, mx_label,
                                    direction=direction, client_address=client_ip)
        log_request(sender, recipient, group, mx_label,
                    f"{code} {msg}", envelope,
                    direction=direction, client_address=client_ip)

        return code, msg

    # ── SMTP delivery ─────────────────────────────────────────────────

    @staticmethod
    async def _smtp_send(host, port, sender, recipients, content):
        """
        Deliver via aiosmtplib.  Returns (code, message).
        4xx on any transient error so Postfix can retry.
        """
        import aiosmtplib

        # content may be bytes or str
        if isinstance(content, str):
            raw = content.encode('utf-8', errors='replace')
        else:
            raw = content

        try:
            await aiosmtplib.send(
                raw,
                sender=sender,
                recipients=recipients,
                hostname=host,
                port=port,
                timeout=30,
            )
            return 250, "OK"
        except aiosmtplib.SMTPRecipientsRefused as e:
            # Permanent rejection from remote
            return 550, str(e)
        except aiosmtplib.SMTPException as e:
            return 451, f"Upstream SMTP error: {e}"
        except (OSError, asyncio.TimeoutError) as e:
            return 451, f"Connection failed: {e}"


class RelayService:
    """Owns the aiosmtpd Controller and background jobs."""

    def __init__(self, config, mx_router):
        """
        Args:
            config:    Config instance
            mx_router: callable(sender, recipient, cache_ttl) -> (Server, group)
        """
        self.config = config
        self.mx_router = mx_router

        # MX DNS cache (shared with jolly-relay.py business logic)
        self.mx_cache = {}
        self.cache_lock = threading.Lock()

    # ── Stats ─────────────────────────────────────────────────────────

    def print_stats(self):
        process = psutil.Process(os.getpid())
        memory_mb = process.memory_info().rss / 1024 / 1024
        with self.cache_lock:
            cache_size = len(self.mx_cache)
        return f"Memory usage: {memory_mb:.2f} MB, Cache items: {cache_size}"

    # ── Cache GC ──────────────────────────────────────────────────────

    def cleanup_cache(self):
        cache_ttl = self.config.cache_ttl
        if cache_ttl <= 0:
            return 0

        current_time = time.time()
        expired_keys = []

        with self.cache_lock:
            for domain, (cache_time, _) in self.mx_cache.items():
                if current_time - cache_time >= cache_ttl:
                    expired_keys.append(domain)
            for domain in expired_keys:
                del self.mx_cache[domain]

        if expired_keys:
            log_debug(f"GC: removed {len(expired_keys)} expired cache entries")

        return len(expired_keys)

    # ── Background thread ─────────────────────────────────────────────

    def _jobs_thread(self):
        last_gc = time.time()
        while True:
            log_debug(self.print_stats())
            if self.config.cache_ttl > 0 and time.time() - last_gc >= GC_INTERVAL:
                self.cleanup_cache()
                last_gc = time.time()
            time.sleep(STATS_INTERVAL)

    # ── IP allow-list check (called from the custom Controller) ───────

    def is_allowed(self, peer_ip):
        return self.config.is_allowed(peer_ip)

    # ── Signal handlers ───────────────────────────────────────────────

    def _shutdown(self, loop):
        self.config.verbose = True
        self.config.flush_csv()
        log(self.config.print_usage())
        log(self.print_stats())
        loop.stop()

    def register_signals(self, loop):
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: self._shutdown(loop))

    # ── Main entry point ──────────────────────────────────────────────

    def run(self):
        self.config.load()
        self.config.start_csv_flush_thread()

        handler = RelayHandler(self.config, self.mx_router, self)

        controller = _AllowlistController(
            handler,
            relay_service=self,
            hostname=self.config.host,
            port=self.config.port,
        )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.register_signals(loop)

        bg = threading.Thread(target=self._jobs_thread, daemon=True)
        bg.start()

        try:
            controller.start()
            log(f"Jolly Relay listening on {self.config.host}:{self.config.port}")
            loop.run_forever()
        finally:
            controller.stop()
            self.config.flush_csv()


class _AllowlistController(Controller):
    """
    Subclass of aiosmtpd Controller that enforces the IP allow-list
    at connection time by overriding the SMTP protocol's connection_made hook.
    """

    def __init__(self, handler, relay_service, **kwargs):
        super().__init__(handler, **kwargs)
        self._relay_service = relay_service

    def factory(self):
        """Return a GuardedSMTP that closes denied connections before any banner."""
        relay_service = self._relay_service

        class GuardedSMTP(SMTPProtocol):
            def connection_made(self, transport):
                peer_ip = transport.get_extra_info('peername', ('', 0))[0]
                if not relay_service.is_allowed(peer_ip):
                    log(f"Rejected connection from {peer_ip} (not in allowed_hosts)",
                        to_stderr=True)
                    log_to_file(f"Rejected connection from {peer_ip}")
                    transport.write(b"554 5.7.1 Access denied\r\n")
                    transport.close()
                    return
                super().connection_made(transport)

        return GuardedSMTP(self.handler, **self.SMTP_kwargs)
