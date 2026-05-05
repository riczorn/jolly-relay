import yaml
import logging
import sys
import datetime
import threading
import ipaddress
import json
import socket as _socket_module

config = None

def log(message, to_stderr=False):
    """Operational console output (startup, errors, warnings). Always shown."""
    if to_stderr:
        sys.stderr.write(f"{message}\n")
        sys.stderr.flush()
    else:
        sys.stdout.write(f"{message}\n")
        sys.stdout.flush()

def log_debug(message):
    """Console output only when verbose."""
    if config.verbose:
        sys.stdout.write(f"{message}\n")
        sys.stdout.flush()

def log_to_file(message):
    """Write to log file only, never to console."""
    if config.logger:
        config.logger.info(message)

def log_request(sender, recipient, group, mx, action, envelope=None, direction="", client_address="", sasl_username=""):
    """Per-request output to console and log file."""
    summary = f"{sender}\t{recipient}\t{group}\t{mx}\t{action}\t{client_address}"
    if direction:
        summary += f"\t{direction}"
    if sasl_username and sasl_username != sender:
        summary += f"\t(sasl:{sasl_username})"

    if config.verbose and envelope:
        payload = f"  from={envelope.mail_from}\n  to={envelope.rcpt_tos}"
        sys.stdout.write(f"{payload}\n{summary}\n")
        sys.stdout.flush()
        log_to_file(f"{payload}\n{summary}")
    else:
        sys.stdout.write(f"{summary}\n")
        sys.stdout.flush()


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
        self.weight_target = 0
        self.weight_current = 0
        self.mails_sent = 0


class Servers:
    def __init__(self, server_list):
        self.servers = []
        self.current = -1
        self.lock = threading.Lock()
        weight_sum = 0
        for attr in vars(server_list):
            if not attr.startswith('__'):
                value = getattr(server_list, attr)
                if not hasattr(value, 'weight'):
                    value.weight = 100
                weight_sum += value.weight
                self.servers.append(Server(attr, value.address, value.weight))
                log_debug(f"  {attr}: {value.address:30s} - {value.weight:4,d} %")

        if len(self.servers) > 0:
            for server in self.servers:
                server.weight_target = server.weight / weight_sum

    def print(self):
        self.calc_weight()
        usage = f"  Name          # Sent |  curr. % / target %"
        for i in self.servers:
            usage = f"{usage}\n    {i.name:10s} {i.mails_sent:7,d} | {i.weight_current*100:8.4f} / {i.weight_target*100:8.4f}"
        return usage

    def calc_weight(self):
        total_mails = sum(s.mails_sent for s in self.servers)
        if total_mails > 0:
            for server in self.servers:
                server.weight_current = server.mails_sent / total_mails

    def get_next(self, mx_identifier=False):
        with self.lock:
            chosen_server = False

            if mx_identifier:
                chosen_server = self.get(mx_identifier)

            if not chosen_server:
                current = (self.current + 1) % len(self.servers)
                self.calc_weight()

                found = False
                iteration = 0
                while iteration < len(self.servers) and not found:
                    iteration += 1
                    if self.servers[current].weight_current < self.servers[current].weight_target:
                        self.current = current
                        found = True
                        break
                    current = (current + 1) % len(self.servers)
                chosen_server = self.servers[self.current]

            chosen_server.mails_sent += 1
            return chosen_server

    def get(self, name):
        for server in self.servers:
            if name == server.name:
                return server
        return False


class Config:
    def __init__(self):
        global config
        config = self
        self.config_dict = {}
        self.config_obj = None
        self.servers = []
        self.logger = False
        self.csv_file = None
        self.csv_buffer = []  # kept for compat; no longer used
        self.csv_lock = threading.Lock()
        self.csv_flush_thread = None
        self.reject_sender_login_mismatch = False
        self.allowed_ips = set()
        self.local_networks = []
        self.local_domains = []

        self.verbose = False
        self.cache_ttl = 3600
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
        self.servers_obj = None
        self.server_groups = self.obj_dic({})
        self.servers_default_obj = None
        self.servers_default_action = "DUNNO"
        self.combined_rule_groups = {}

        # Local delivery: where to forward inbound mail
        self.local_delivery_host = '127.0.0.1'
        self.local_delivery_port = 25

        self.parse_args()

    def setup_custom_logger(self, name, filename):
        logger = logging.getLogger(name)
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

    def obj_dic(self, d):
        top = type('new', (object,), d)
        seqs = tuple, list, set, frozenset
        for i, j in d.items():
            if isinstance(j, dict):
                setattr(top, i, self.obj_dic(j))
            elif isinstance(j, seqs):
                setattr(top, i, type(j)(self.obj_dic(sj) if isinstance(sj, dict) else sj for sj in j))
            else:
                setattr(top, i, j)
        return top

    def parse_args(self):
        import argparse
        parser = argparse.ArgumentParser(description='Jolly Relay - async SMTP relay server')
        parser.add_argument('-c', '--config',
                            default=self.config_file,
                            help=f'Path to configuration file (default: {self.config_file})')
        parser.add_argument('-p', '--port',
                            type=int,
                            default=self.port,
                            help=f'Port to listen on (default: {self.port})')
        parser.add_argument('-H', '--host',
                            default=self.host,
                            help=f'Host to bind to (default: {self.host})')
        parser.add_argument('--cache-ttl',
                            type=int,
                            default=self.cache_ttl,
                            help=f'Cache TTL in seconds (default: {self.cache_ttl}, where 0 disables cache)')
        parser.add_argument('--timeout',
                            type=int,
                            default=self.timeout,
                            help=f'Client inactivity timeout in seconds (default: {self.timeout}, where 0 disables timeout)')
        parser.add_argument('-v', '--verbose',
                            action='store_true',
                            default=self.verbose,
                            help='Increase verbosity level (default: false)')
        parsed_args = parser.parse_args()

        self.verbose = parsed_args.verbose
        self.cache_ttl = parsed_args.cache_ttl
        self.timeout = parsed_args.timeout
        self.port = parsed_args.port
        self.host = parsed_args.host
        self.config_file = parsed_args.config

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

        with open(self.config_file) as config_file:
            try:
                self.config_dict = yaml.safe_load(config_file)
            except yaml.YAMLError as exc:
                log(f"ERROR: Failed to parse YAML configuration file {self.config_file}:\n  {exc}", True)
                sys.exit(1)

            if not isinstance(self.config_dict, dict):
                log(f"ERROR: Configuration file {self.config_file} is empty or not formatted correctly as a YAML dictionary.", True)
                sys.exit(1)

            if 'config' not in self.config_dict or not self.config_dict['config']:
                self.config_dict['config'] = {}

            cfg = self.config_dict['config']
            self.reject_sender_login_mismatch = cfg.get('reject_sender_login_mismatch', self.reject_sender_login_mismatch)
            self.log_file = cfg.get('log_file', '/var/log/jolly-relay.log')
            self.csv_file = cfg.get('csv_file', None) or None
            self.graylog_server = cfg.get('graylog_server', None) or None
            self.graylog_port = int(cfg.get('graylog_port', 12201))
            self.servername = cfg.get('servername', None) or None
            if self.graylog_server:
                self._graylog_sock = _socket_module.socket(_socket_module.AF_INET, _socket_module.SOCK_DGRAM)
            self.verbose = cfg.get('verbose', self.verbose)

            # Resolve allowed_hosts to a set of IPs
            allowed_hosts = cfg.get('allowed_hosts', [])
            self.allowed_ips = self._resolve_allowed_hosts(allowed_hosts)

            local_networks = cfg.get('local_networks', ['127.0.0.0/8'])
            for net in local_networks:
                try:
                    self.local_networks.append(ipaddress.ip_network(net, strict=False))
                except ValueError as e:
                    log(f"WARNING: Invalid local network '{net}': {e}", to_stderr=True)
            self.local_domains = [str(d).lower() for d in cfg.get('local_domains', [])]

            self.auto_populate_local_domains = cfg.get('auto_populate_local_domains', False)
            self.postfix_virtual_file = cfg.get('postfix_virtual_file', '')
            self.populate_local_domains()

            # Local delivery destination for inbound mail
            local_delivery = cfg.get('local_delivery', '127.0.0.1:25')
            self.local_delivery_host, self.local_delivery_port = parse_host_port(local_delivery, default_port=25)

            bind_host = cfg.get('bind_host', '127.0.0.1')
            bind_port = int(cfg.get('bind_port', 9725))

            if self.host == '127.0.0.1' and bind_host:
                self.host = bind_host
            if self.port == 9725 and bind_port:
                self.port = bind_port

            self.config_obj = self.obj_dic(self.config_dict)

            self.logger = self.setup_custom_logger('jolly-relay', self.log_file)

            log_debug("# MX Servers")

            self.server_groups = self.obj_dic({})

            if hasattr(self.config_obj, 'servers') and hasattr(self.config_obj.servers, 'hosts'):
                self.servers_obj = Servers(self.config_obj.servers.hosts)
                self.servers = self.servers_obj.servers

                groups_dict = self.config_dict.get('servers', {}).get('groups', {})
                server_groups = {}
                for server_group_name, server_group_list in groups_dict.items():
                    server_group_array = {}
                    for server_name in server_group_list:
                        server_group_array[server_name] = getattr(self.config_obj.servers.hosts, server_name)

                    server_group_dict = self.obj_dic(server_group_array)
                    log_debug(f"# MX group           {server_group_name}")
                    server_groups[server_group_name] = Servers(server_group_dict)

                self.server_groups = self.obj_dic(server_groups)

                self.servers_default_obj = None
                self.servers_default_action = "DUNNO"

                default_val = self.config_dict.get('servers', {}).get('default', 'ALL')
                if isinstance(default_val, list):
                    server_group_array = {}
                    for server_name in default_val:
                        if hasattr(self.config_obj.servers.hosts, server_name):
                            server_group_array[server_name] = getattr(self.config_obj.servers.hosts, server_name)
                        else:
                            log(f"WARNING: servers.default references unknown server '{server_name}'", to_stderr=True)
                    if server_group_array:
                        self.servers_default_obj = Servers(self.obj_dic(server_group_array))
                elif isinstance(default_val, str):
                    if default_val == "ALL":
                        self.servers_default_obj = self.servers_obj
                    elif default_val in server_groups:
                        self.servers_default_obj = server_groups[default_val]
                    else:
                        self.servers_default_action = "DUNNO"

            # Load combined rules
            self.combined_rule_groups = {}
            if 'combined_rules' in self.config_dict and self.config_dict['combined_rules']:
                log_debug("# Combined Rules")
                combined_rules = {}
                for combined_key, server_list in self.config_dict['combined_rules'].items():
                    log_debug(f"  {combined_key}: {server_list}")
                    if isinstance(server_list, str):
                        groups_section = self.config_dict.get('servers', {}).get('groups', {})
                        if server_list in groups_section:
                            server_list = groups_section[server_list]
                        else:
                            log(f"WARNING: Combined rule '{combined_key}' references unknown group '{server_list}'", to_stderr=True)
                            continue
                    server_group_array = {}
                    for server_name in server_list:
                        if hasattr(self.config_obj.servers.hosts, server_name):
                            server_group_array[server_name] = getattr(self.config_obj.servers.hosts, server_name)
                        else:
                            log(f"WARNING: Combined rule '{combined_key}' references unknown server '{server_name}'", to_stderr=True)

                    if server_group_array:
                        server_group_dict = self.obj_dic(server_group_array)
                        combined_rules[combined_key] = Servers(server_group_dict)

                if combined_rules:
                    self.combined_rule_groups = combined_rules

            log_debug("Config.loaded\n")

    def populate_local_domains(self):
        if self.auto_populate_local_domains:
            import os
            if os.path.exists(self.postfix_virtual_file):
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
                    log(f"WARNING: Failed to read postfix_virtual_file {self.postfix_virtual_file}: {e}", to_stderr=True)
            else:
                log(f"WARNING: postfix_virtual_file {self.postfix_virtual_file} not found for auto-population.", to_stderr=True)

    def test_domain_rules(self, email, domain, rule_type="sender_rules"):
        if not hasattr(self.config_obj, rule_type):
            return False, False
        rules_dict = self.config_dict.get(rule_type) or {}
        rules = [r for r in rules_dict if not r.startswith('__')]

        default = False
        result = False
        for rule in rules:
            value = rules_dict[rule]
            if rule == "default":
                default = value
                continue

            matched = False
            if '@' in rule:
                matched = (email == rule)
            elif rule in domain:
                matched = True
            elif domain == rule or domain.endswith('.' + rule):
                matched = True

            if matched:
                result = value
                match_type = f"email {email}" if '@' in rule else f"MX domain {domain}" if rule in domain else f"mail domain {domain}"
                log_debug(f"  Matched {match_type} against {rule} in {rule_type}: {value}")
                break

        if not result:
            result = default

        return result, default

    def get_server_group(self, identifier):
        servers_obj = self.servers_obj

        if identifier:
            server_groups = [sg for sg in vars(self.server_groups) if not sg.startswith('__')]
            if identifier in server_groups:
                servers_obj = getattr(self.server_groups, identifier)
            else:
                log(f"WARNING: Unknown server group '{identifier}', using full server pool", to_stderr=True)

        return servers_obj

    def _resolve_allowed_hosts(self, hosts):
        """Resolve a list of hostnames/IPs to a set of IP addresses."""
        import socket as _socket
        if not hosts:
            return set()
        resolved = set()
        for host in hosts:
            host = str(host).strip()
            if not host or host == '0.0.0.0':
                return set()
            try:
                results = _socket.getaddrinfo(host, None)
                for family, _type, _proto, _canonname, sockaddr in results:
                    resolved.add(sockaddr[0])
            except _socket.gaierror:
                log(f"WARNING: Could not resolve allowed_host '{host}'", to_stderr=True)
        if resolved:
            log_debug(f"# Allowed hosts: {resolved}")
        return resolved

    def is_allowed(self, addr_ip):
        """Check if an IP address is in the allowed set. Empty set = allow all."""
        if not self.allowed_ips:
            return True
        return addr_ip in self.allowed_ips

    def is_local_client(self, ip_str):
        if not ip_str:
            return False
        try:
            ip = ipaddress.ip_address(ip_str)
            for net in self.local_networks:
                if ip in net:
                    return True
        except ValueError:
            pass
        return False

    def is_local_domain(self, domain):
        if not domain or not self.local_domains:
            return False
        domain = domain.lower()
        for local_dom in self.local_domains:
            if domain == local_dom or domain.endswith('.' + local_dom):
                return True
        return False

    def print_usage(self):
        if not self.servers_obj:
            return "(no servers loaded)"
        output = "\nAll Servers\n"
        output += self.servers_obj.print()

        server_groups = [sg for sg in vars(self.server_groups) if not sg.startswith('__')]
        for server_name in server_groups:
            server_obj = self.get_server_group(server_name)
            output += f"\n\nGroup {server_name}\n"
            output += server_obj.print()

        log_to_file(output)
        return output

    def print_csv(self, sender, recipient, mx_group, mx_host, direction="", client_address="", sasl_username=""):
        if not self.csv_file:
            return
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        csv_line = f"{now_str};{sender};{recipient};{mx_group};{mx_host};{client_address};{direction}"
        sasl_info = f"sasl:{sasl_username}" if (sasl_username and sasl_username != sender) else ""
        csv_line += f";{sasl_info}\n"
        try:
            with self.csv_lock:
                with open(self.csv_file, 'a') as f:
                    f.write(csv_line)
        except Exception as e:
            log(f"ERROR: Failed to write to CSV log {self.csv_file} ({e})", to_stderr=True)

    def flush_csv(self):
        pass  # writes are now immediate; kept for compatibility with callers

    def start_csv_flush_thread(self):
        pass  # no-op: writes are immediate

    def send_to_graylog(self, sender, recipient, mx_group, mx_host, direction="", client_address="", sasl_username=""):
        if not self.graylog_server or not self._graylog_sock:
            return
        domain = recipient.split('@')[-1] if '@' in recipient else "unknown"
        sasl_info = sasl_username if (sasl_username and sasl_username != sender) else ""
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
            "_sasl_username": sasl_info,
            "_domain": domain,
        }
        try:
            self._graylog_sock.sendto(
                json.dumps(payload).encode('utf-8'),
                (self.graylog_server, self.graylog_port)
            )
        except Exception as e:
            log(f"WARNING: Graylog send failed: {e}", to_stderr=True)
