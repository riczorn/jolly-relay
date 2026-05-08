"""
Server and Servers classes for Jolly Relay.

Server      — a single upstream MX endpoint with weighted round-robin state.
Servers     — a group of Servers; get_next() picks the next one by weight.
parse_host_port — splits 'host:port' strings.
"""

import threading
from src.logging import log, log_debug


def parse_host_port(address, default_port=25):
    """Parse a 'host:port' string into (host, port). Returns (None, None) on error."""
    if not address:
        return None, None
    address = str(address).strip()
    if ':' in address:
        parts = address.rsplit(':', 1)
        host = parts[0].strip()
        try:
            port = int(parts[1].strip())
        except ValueError:
            log(f"WARNING: Invalid port in address '{address}', using {default_port}", to_stderr=True)
            port = default_port
    else:
        host = address
        port = default_port
    return host, port


class Server:
    def __init__(self, name, address, weight_target=100):
        self.name = name
        self.address = address          # raw 'host:port' string from config
        host, port = parse_host_port(address)
        self.host = host
        self.port = port
        self.weight = weight_target
        self.weight_target = 0.0
        self.weight_current = 0.0
        self.mails_sent = 0
        # TLS behaviour derived from port:
        #   25  → plain SMTP (no TLS)
        #   465 → implicit TLS (use_tls=True, connect with TLS from the start)
        #   587 → STARTTLS
        #   other → plain SMTP
        self.use_tls = port in (465, 587)


class Servers:
    def __init__(self, server_dicts):
        """
        Args:
            server_dicts: dict of {name: {'address': str, 'weight': int}}
        """
        self.servers = []
        self.current = -1
        self.lock = threading.Lock()

        weight_sum = 0
        for name, info in server_dicts.items():
            if not isinstance(info, dict) or not info.get('address'):
                log(f"WARNING: Server '{name}' has no valid address — skipped", to_stderr=True)
                continue
            weight = int(info.get('weight', 100))
            weight_sum += weight
            server = Server(name, info['address'], weight)
            self.servers.append(server)
            tls_tag = " [tls]" if server.use_tls else ""
            log_debug(f"  {name}: {info['address']:30s} - {weight:4,d}{tls_tag}")

        if self.servers and weight_sum > 0:
            for server in self.servers:
                server.weight_target = server.weight / weight_sum

    def print(self):
        self.calc_weight()
        usage = "  Name          # Sent |  curr. % / target %"
        for s in self.servers:
            usage += f"\n    {s.name:10s} {s.mails_sent:7,d} | {s.weight_current*100:8.4f} / {s.weight_target*100:8.4f}"
        return usage

    def calc_weight(self):
        total_mails = sum(s.mails_sent for s in self.servers)
        if total_mails > 0:
            for server in self.servers:
                server.weight_current = server.mails_sent / total_mails

    def get_next(self, name=None):
        """
        Return the next Server by weighted round-robin.

        If name is given and matches a server, return that server directly.
        Otherwise walk the server list to find the one most below its target
        weight; if all are at or above target, pick the one furthest below
        (i.e. fall back to plain round-robin rather than failing silently).
        """
        with self.lock:
            if name:
                server = self.get(name)
                if server:
                    server.mails_sent += 1
                    return server

            self.calc_weight()
            n = len(self.servers)
            if n == 0:
                return None

            # Find server furthest below its weight target (most underserved).
            # Start from the position after the last chosen one.
            best = None
            best_deficit = None
            for i in range(n):
                idx = (self.current + 1 + i) % n
                s = self.servers[idx]
                deficit = s.weight_target - s.weight_current
                if best is None or deficit > best_deficit:
                    best = idx
                    best_deficit = deficit

            self.current = best
            chosen = self.servers[best]
            chosen.mails_sent += 1
            return chosen

    def get(self, name):
        for server in self.servers:
            if name == server.name:
                return server
        return None
