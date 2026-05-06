"""
Configuration loader for Jolly Relay.

Reads jolly-relay.yaml, validates it, and populates the Config instance
used throughout the application.
"""

import sys
import json
import logging
import datetime
import asyncio
import threading
import ipaddress
import socket as _socket_module

import yaml

import src.logging as _log_module
from src.logging import log, log_debug, log_to_file
from src.servers import Servers, parse_host_port

# Module-level reference kept for backward compat (service.py uses cfg.config).
config = None


class Config:
    def __init__(self):
        global config
        config = self
        _log_module.config = self

        self.config_dict = {}
        self.servers = []
        self.logger = False
        self.csv_file = None
        self.csv_lock = threading.Lock()
        self.reject_sender_login_mismatch = False
        self.allowed_ips = set()
        self.local_networks = []
        self.local_domains = []

        self.verbose = False
        self.cache_ttl = 3600
        self.cache_max_size = 10000
        self.timeout = 600
        self.port = 9725
        self.host = '127.0.0.1'
        self.config_file = 'jolly-relay.yaml'
        self.auto_populate_local_domains = False
        self.postfix_virtual_file = ''
        self.graylog_server = None
        self.graylog_port = 12201
        self.servername = None
        self._graylog_sock = None

        # Servers
        self.servers_obj = None
        self.server_groups = {}          # name -> Servers
        self.servers_default_obj = None
        self.servers_default_action = "DUNNO"
        self.combined_rule_groups = {}   # key -> Servers

        # Local delivery destination for inbound mail
        self.local_delivery_host = '127.0.0.1'
        self.local_delivery_port = 25

        self.parse_args()

    # ── Argument parsing ──────────────────────────────────────────────

    def parse_args(self):
        import argparse
        parser = argparse.ArgumentParser(description='Jolly Relay - async SMTP relay server')
        parser.add_argument('-c', '--config', default=self.config_file,
                            help=f'Path to configuration file (default: {self.config_file})')
        parser.add_argument('-p', '--port', type=int, default=self.port,
                            help=f'Port to listen on (default: {self.port})')
        parser.add_argument('-H', '--host', default=self.host,
                            help=f'Host to bind to (default: {self.host})')
        parser.add_argument('--cache-ttl', type=int, default=self.cache_ttl,
                            help=f'Cache TTL in seconds (default: {self.cache_ttl}, 0 disables)')
        parser.add_argument('--timeout', type=int, default=self.timeout,
                            help=f'Client inactivity timeout in seconds (default: {self.timeout}, 0 disables)')
        parser.add_argument('-v', '--verbose', action='store_true', default=self.verbose,
                            help='Enable verbose logging (default: false)')
        parsed = parser.parse_args()
        self.verbose = parsed.verbose
        self.cache_ttl = parsed.cache_ttl
        self.timeout = parsed.timeout
        self.port = parsed.port
        self.host = parsed.host
        self.config_file = parsed.config

    # ── Logger setup ──────────────────────────────────────────────────

    def _setup_logger(self, filename):
        logger = logging.getLogger('jolly-relay')
        formatter = logging.Formatter(fmt='%(asctime)s;%(message)s',
                                      datefmt='%Y-%m-%d %H:%M:%S')
        logger.setLevel(logging.DEBUG)
        if filename:
            try:
                handler = logging.FileHandler(filename, mode='a')
                handler.setFormatter(formatter)
                logger.addHandler(handler)
            except Exception as e:
                log(f"ERROR: Failed to setup file logger to {filename} ({e})", to_stderr=True)
                sys.exit(1)
        return logger

    # ── Config loading ────────────────────────────────────────────────

    def load(self):
        import os
        config_path = self.config_file
        if config_path == 'jolly-relay.yaml':
            etc_path = "/etc/postfix/jolly-relay.yaml"
            if os.path.exists(etc_path):
                config_path = etc_path
        self.config_file = config_path

        if not os.path.exists(self.config_file):
            log(f"ERROR: Config file {self.config_file} not found", True)
            sys.exit(1)

        with open(self.config_file) as f:
            try:
                self.config_dict = yaml.safe_load(f)
            except yaml.YAMLError as exc:
                log(f"ERROR: Failed to parse YAML {self.config_file}:\n  {exc}", True)
                sys.exit(1)

        if not isinstance(self.config_dict, dict):
            log(f"ERROR: Config file {self.config_file} is not a YAML dictionary.", True)
            sys.exit(1)

        if 'config' not in self.config_dict or not self.config_dict['config']:
            self.config_dict['config'] = {}

        cfg = self.config_dict['config']

        self.reject_sender_login_mismatch = cfg.get('reject_sender_login_mismatch', False)
        self.cache_max_size = int(cfg.get('cache_max_size', self.cache_max_size))
        self.log_file = cfg.get('log_file', '/var/log/jolly-relay.log')
        self.csv_file = cfg.get('csv_file', None) or None
        self.graylog_server = cfg.get('graylog_server', None) or None
        self.graylog_port = int(cfg.get('graylog_port', 12201))
        self.servername = cfg.get('servername', None) or None
        self.verbose = cfg.get('verbose', self.verbose)

        if self.graylog_server:
            self._graylog_sock = _socket_module.socket(
                _socket_module.AF_INET, _socket_module.SOCK_DGRAM
            )

        allowed_hosts = cfg.get('allowed_hosts', [])
        self.allowed_ips = self._resolve_allowed_hosts(allowed_hosts)

        for net in cfg.get('local_networks', ['127.0.0.0/8']):
            try:
                self.local_networks.append(ipaddress.ip_network(net, strict=False))
            except ValueError as e:
                log(f"WARNING: Invalid local network '{net}': {e}", to_stderr=True)

        self.local_domains = [str(d).lower() for d in cfg.get('local_domains', [])]
        self.auto_populate_local_domains = cfg.get('auto_populate_local_domains', False)
        self.postfix_virtual_file = cfg.get('postfix_virtual_file', '')
        self.populate_local_domains()

        local_delivery = cfg.get('local_delivery', '127.0.0.1:25')
        self.local_delivery_host, self.local_delivery_port = parse_host_port(
            local_delivery, default_port=25
        )

        bind_host = cfg.get('bind_host', '127.0.0.1')
        bind_port = int(cfg.get('bind_port', 9725))
        if self.host == '127.0.0.1' and bind_host:
            self.host = bind_host
        if self.port == 9725 and bind_port:
            self.port = bind_port

        self.logger = self._setup_logger(self.log_file)

        self._load_servers()
        self._load_combined_rules()

        # Validate all rule references resolve to known groups/servers.
        errors = self.validate()
        if errors:
            for err in errors:
                log(f"ERROR: {err}", to_stderr=True)
            sys.exit(1)

        log_debug("Config loaded\n")

    # ── Server loading ────────────────────────────────────────────────

    def _load_servers(self):
        servers_section = self.config_dict.get('servers', {})
        hosts_dict = servers_section.get('hosts', {})
        groups_dict = servers_section.get('groups', {})

        if not hosts_dict:
            return

        log_debug("# MX Servers")
        self.servers_obj = Servers(hosts_dict)
        self.servers = self.servers_obj.servers

        for group_name, member_names in groups_dict.items():
            group_hosts = {}
            for name in member_names:
                if name in hosts_dict:
                    group_hosts[name] = hosts_dict[name]
                else:
                    log(f"WARNING: Group '{group_name}' references unknown server '{name}'",
                        to_stderr=True)
            if group_hosts:
                log_debug(f"# MX group {group_name}")
                self.server_groups[group_name] = Servers(group_hosts)

        default_val = servers_section.get('default', 'DUNNO')
        self._load_default(default_val, hosts_dict)

    def _load_default(self, default_val, hosts_dict):
        if isinstance(default_val, list):
            group_hosts = {}
            for name in default_val:
                if name in hosts_dict:
                    group_hosts[name] = hosts_dict[name]
                else:
                    log(f"WARNING: servers.default references unknown server '{name}'",
                        to_stderr=True)
            if group_hosts:
                self.servers_default_obj = Servers(group_hosts)
        elif isinstance(default_val, str):
            if default_val == "ALL":
                self.servers_default_obj = self.servers_obj
            elif default_val == "DUNNO":
                self.servers_default_action = "DUNNO"
            elif default_val in self.server_groups:
                self.servers_default_obj = self.server_groups[default_val]
            else:
                log(f"WARNING: servers.default references unknown group '{default_val}'",
                    to_stderr=True)
                self.servers_default_action = "DUNNO"

    def _load_combined_rules(self):
        combined_section = self.config_dict.get('combined_rules') or {}
        hosts_dict = self.config_dict.get('servers', {}).get('hosts', {})
        groups_dict = self.config_dict.get('servers', {}).get('groups', {})

        if not combined_section:
            return

        log_debug("# Combined Rules")
        for key, server_list in combined_section.items():
            log_debug(f"  {key}: {server_list}")

            if isinstance(server_list, str):
                if server_list in groups_dict:
                    server_list = groups_dict[server_list]
                elif server_list in self.server_groups:
                    # Already a Servers object — reference directly.
                    self.combined_rule_groups[key] = self.server_groups[server_list]
                    continue
                else:
                    log(f"WARNING: combined_rule '{key}' references unknown group '{server_list}'",
                        to_stderr=True)
                    continue

            group_hosts = {}
            for name in server_list:
                if name in hosts_dict:
                    group_hosts[name] = hosts_dict[name]
                else:
                    log(f"WARNING: combined_rule '{key}' references unknown server '{name}'",
                        to_stderr=True)
            if group_hosts:
                self.combined_rule_groups[key] = Servers(group_hosts)

    # ── Validation ────────────────────────────────────────────────────

    def validate(self):
        """
        Check that all rule references resolve to known groups or servers.
        Returns a list of error strings; empty list means valid.
        """
        errors = []
        hosts_dict = self.config_dict.get('servers', {}).get('hosts', {})

        for rule_type in ('sender_rules', 'recipient_rules'):
            rules = self.config_dict.get(rule_type) or {}
            for rule_key, group_name in rules.items():
                if rule_key == 'default':
                    continue
                if not self._group_or_server_exists(group_name, hosts_dict):
                    errors.append(
                        f"{rule_type}['{rule_key}'] references unknown group/server '{group_name}'"
                    )

        for key, server_list in (self.config_dict.get('combined_rules') or {}).items():
            if isinstance(server_list, str):
                if not self._group_or_server_exists(server_list, hosts_dict):
                    errors.append(
                        f"combined_rules['{key}'] references unknown group/server '{server_list}'"
                    )
            elif isinstance(server_list, list):
                for name in server_list:
                    if name not in hosts_dict:
                        errors.append(
                            f"combined_rules['{key}'] references unknown server '{name}'"
                        )

        for name, info in hosts_dict.items():
            addr = info.get('address', '') if isinstance(info, dict) else ''
            host, port = parse_host_port(addr)
            if not host:
                errors.append(f"servers.hosts['{name}'] has no valid address")

        return errors

    def _group_or_server_exists(self, name, hosts_dict):
        return name in self.server_groups or name in hosts_dict

    # ── Domain helpers ────────────────────────────────────────────────

    def populate_local_domains(self):
        if not self.auto_populate_local_domains:
            return
        import os
        if not os.path.exists(self.postfix_virtual_file):
            log(f"WARNING: postfix_virtual_file {self.postfix_virtual_file} not found.",
                to_stderr=True)
            return
        try:
            with open(self.postfix_virtual_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '@' in line:
                        continue
                    parts = line.split()
                    if parts:
                        domain = parts[0].lower()
                        if domain not in self.local_domains:
                            self.local_domains.append(domain)
            log_debug(f"# Auto-populated local_domains from {self.postfix_virtual_file}")
        except Exception as e:
            log(f"WARNING: Failed to read postfix_virtual_file {self.postfix_virtual_file}: {e}",
                to_stderr=True)

    def test_domain_rules(self, email, domain, rule_type="sender_rules"):
        rules_dict = self.config_dict.get(rule_type) or {}
        if not rules_dict:
            return False, False

        default = False
        result = False
        for rule, value in rules_dict.items():
            if rule == "default":
                default = value
                continue

            if '@' in rule:
                matched = (email == rule)
            elif rule in domain:
                matched = True
            elif domain == rule or domain.endswith('.' + rule):
                matched = True
            else:
                matched = False

            if matched:
                match_type = (f"email {email}" if '@' in rule
                              else f"MX domain {domain}" if rule in domain
                              else f"mail domain {domain}")
                log_debug(f"  Matched {match_type} against {rule} in {rule_type}: {value}")
                result = value
                break

        return result or default, default

    # ── Server helpers ────────────────────────────────────────────────

    def get_server_group(self, identifier):
        if identifier and identifier in self.server_groups:
            return self.server_groups[identifier]
        if identifier:
            log(f"WARNING: Unknown server group '{identifier}', using full server pool",
                to_stderr=True)
        return self.servers_obj

    # ── IP / network helpers ──────────────────────────────────────────

    def _resolve_allowed_hosts(self, hosts):
        if not hosts:
            return set()
        resolved = set()
        for host in hosts:
            host = str(host).strip()
            if not host or host == '0.0.0.0':
                return set()
            try:
                results = _socket_module.getaddrinfo(host, None)
                for family, _t, _p, _c, sockaddr in results:
                    resolved.add(sockaddr[0])
            except _socket_module.gaierror:
                log(f"WARNING: Could not resolve allowed_host '{host}'", to_stderr=True)
        if resolved:
            log_debug(f"# Allowed hosts: {resolved}")
        return resolved

    def is_allowed(self, addr_ip):
        if not self.allowed_ips:
            return True
        return addr_ip in self.allowed_ips

    def is_local_client(self, ip_str):
        if not ip_str:
            return False
        try:
            ip = ipaddress.ip_address(ip_str)
            return any(ip in net for net in self.local_networks)
        except ValueError:
            return False

    def is_local_domain(self, domain):
        if not domain or not self.local_domains:
            return False
        domain = domain.lower()
        return any(domain == d or domain.endswith('.' + d) for d in self.local_domains)

    # ── Stats / CSV / Graylog ─────────────────────────────────────────

    def print_usage(self):
        if not self.servers_obj:
            return "(no servers loaded)"
        output = "\nAll Servers\n" + self.servers_obj.print()
        for group_name, servers_obj in self.server_groups.items():
            output += f"\n\nGroup {group_name}\n" + servers_obj.print()
        log_to_file(output)
        return output

    def _write_csv_line(self, csv_line):
        try:
            with self.csv_lock:
                with open(self.csv_file, 'a') as f:
                    f.write(csv_line)
        except Exception as e:
            log(f"ERROR: Failed to write to CSV log {self.csv_file} ({e})", to_stderr=True)

    def print_csv(self, sender, recipient, mx_group, mx_host,
                  direction="", client_address="", sasl_username=""):
        if not self.csv_file:
            return
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sasl_info = f"sasl:{sasl_username}" if (sasl_username and sasl_username != sender) else ""
        csv_line = (f"{now_str};{sender};{recipient};{mx_group};{mx_host}"
                    f";{client_address};{direction};{sasl_info}\n")
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._write_csv_line, csv_line)
        except RuntimeError:
            self._write_csv_line(csv_line)

    def flush_csv(self):
        pass  # writes are immediate

    def start_csv_flush_thread(self):
        pass  # no-op

    def send_to_graylog(self, sender, recipient, mx_group, mx_host,
                        direction="", client_address="", sasl_username=""):
        if not self.graylog_server or not self._graylog_sock:
            return
        domain = recipient.split('@')[-1] if '@' in recipient else "unknown"
        payload = {
            "version": "1.1",
            "host": self.servername or "jolly-relay",
            "short_message": f"Jolly Relay > Mail routed from {sender}",
            "full_message": f"Mail to {recipient} via {mx_host}",
            "level": 6,
            "_sender": sender,
            "_recipient": recipient,
            "_mx_group": mx_group,
            "_mx_host": mx_host,
            "_direction": direction,
            "_client_address": client_address,
            "_sasl_username": sasl_username if (sasl_username and sasl_username != sender) else "",
            "_domain": domain,
        }
        try:
            self._graylog_sock.sendto(
                json.dumps(payload).encode('utf-8'),
                (self.graylog_server, self.graylog_port)
            )
        except Exception as e:
            log(f"WARNING: Graylog send failed: {e}", to_stderr=True)
