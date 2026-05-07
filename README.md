# Jolly Relay

An async SMTP relay with weighted round-robin routing. Accepts inbound SMTP connections from authorised Postfix instances, determines message direction (inbound / outbound), and routes accordingly:

- **Inbound** (recipient domain is in `local_domains`): forwarded to a configurable local delivery host.
- **Outbound** (everything else): routed to an external MX server selected by weighted round-robin, with sender, recipient and combined rules.

Transient delivery failures return 4xx so Postfix retries automatically.

---

## Main features

- SMTP relay — accepts full SMTP connections
- IP allow-list enforced at connection time, before any SMTP banner
- Direction detection: inbound vs outbound based on `local_domains`
- Automatic population of `local_domains` from a Postfix virtual file
- Weighted round-robin across multiple MX server groups
- Gradually warm up new mail servers via `weight`
- Sender rules, recipient rules, and combined rules (sender+recipient combinations)
- CSV log of every routing decision
- Optional Graylog GELF/UDP logging
- On SIGINT/SIGTERM: graceful shutdown and statistics.

---

## How it works

### Direction

When a message arrives the recipient domain is checked against `local_domains` (which is usually retrieved from the Postfix virtual file):

- **Match** → `INCOMING`: forwarded to `local_delivery: host:port` (i.e. your local Postfix on port 25).
- **No match** → `OUTGOING`: routed to an external MX server chosen by the routing rules.

### Routing (outbound)

Rules are evaluated in this order:

1. **Combined rule** (`"sender_group,recipient_group"` → server/group) — highest priority
2. **Recipient rule** (recipient domain → group)
3. **Sender rule** (sender address or domain → group)
4. **Default** (`servers.default`) — `ALL`, `DUNNO`, or a named group

The selected group is iterated round-robin, respecting server weights.

---

## Installation

### Quick start

```bash
cd /opt
git clone https://github.com/riczorn/jolly-relay.git
cd jolly-relay
./install_service.sh
```

Check the service:

```bash
systemctl status jolly-relay
```

### Manual installation

```bash
sudo apt-get install python3-venv
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp jolly-relay.yaml.example /etc/postfix/jolly-relay.yaml
# edit /etc/postfix/jolly-relay.yaml
touch /var/log/jolly-relay.log /var/log/jolly-relay-messages.csv
python3 jolly-relay.py -v
```

The service looks for its config in `/etc/postfix/jolly-relay.yaml` first, then in its own directory, unless overridden with `-c`.

### Command-line options

```
python3 jolly-relay.py [options]

  -c, --config FILE    Path to configuration file
  -p, --port PORT      Port to bind (default: 9725)
  -H, --host HOST      Host to bind (default: 127.0.0.1)
  --cache-ttl SEC      DNS cache TTL in seconds (default: 3600; 0 = disabled)
  -v, --verbose        Enable verbose logging
```

---

## Integration with Postfix

Postfix submits outbound mail to jolly-relay instead of delivering directly.

### Testing configuration

For testing, you can configure Postfix to only invoke Jolly Relay for a test sender. Then you can try it at will sending from the test sender address, and view stats and logs.
Add an entry in `sender_dependent_default_transport_maps` file to route the test address through `smtp:127.0.0.1:9725` and keep normal Postfix configuration for all other cases.

`/etc/postfix/relay_by_sender`

```
testuser@yourdomain.com   smtp:127.0.0.1:9725
```

Make sure the transport map is referenced in `main.cf`

`/etc/postfix/main.cf`

```
sender_dependent_default_transport_maps = hash:/etc/postfix/relay_by_sender
```

Rebuild the map and reload:

```bash
postmap /etc/postfix/relay_by_sender
postfix reload
```

### Production configuration

Once you are satisfied that configuration works, remove the transport map created above, and

---

## Configuration

Config file: `/etc/postfix/jolly-relay.yaml` (see `jolly-relay.yaml.example` for all options).

Reload after changes:

```bash
systemctl restart jolly-relay
```

### Core settings

```yaml
config:
  bind_host: 127.0.0.1
  bind_port: 9725

  allowed_hosts: [127.0.0.1] # IPs/names allowed to connect; empty = allow all. Add your Postfix host IP(s) to the list.

  local_delivery: 127.0.0.1:25 # where to forward inbound mail (host:port)

  local_domains: [] # domains treated as inbound; empty = all outbound
  auto_populate_local_domains: true
  postfix_virtual_file: /etc/postfix/virtual

  # MX DNS cache: maximum number of domains to cache.
  # When the limit is reached the oldest entry is evicted. 0 = unlimited.
  cache_max_size: 10000

  log_file: /var/log/jolly-relay.log
  csv_file: /var/log/jolly-relay-messages.csv
  verbose: false
```

### Server groups

All server addresses use `host:port` format:

```yaml
servers:
  hosts:
    mx1:
      address: mx1.example.com:25
      weight: 100 # relative weight (default 100); 0 = excluded
    mx2:
      address: mx2.example.com:25
    mx3:
      address: mx3.example.com:25
      weight: 50

  groups:
    good: [mx1, mx2, mx3]
    bad: [mx4, mx5, mx6, mx7]
    picky: [mx3]
    gmail: [mx1, mx3]
    microsoft: [mx2, mx4]

  default: DUNNO # ALL | DUNNO | <group-name>
```

`weight` controls how often a server is chosen: a server with `weight: 50` is selected half as often as one with `weight: 100`. Set `weight: 0` to temporarily exclude a server without removing it.

### Routing rules

```yaml
sender_rules:
  newsletter@example.com: bad
  roger@example.com: good
  example.com: good # domain-level fallback (more specific first)

recipient_rules:
  gmail.com: gmail
  microsoft.com: microsoft
  outlook.com: microsoft
  yahoo.com: picky
  apple.com: picky
```

### Combined rules

When both a sender rule and a recipient rule match, a combined rule takes precedence over both:

```yaml
combined_rules:
  "good,picky": picky # sender=good, recipient=picky → use picky group
  "bad,good": bad # bad sender to good recipient → still bad servers
  "bad,picky": [mx7] # explicit server list
  "bad,gmail": [mx5, mx6]
  "bad,microsoft": microbad
```

If no combined rule matches, the recipient rule is used; if that also misses, the sender rule; if that too misses, `servers.default`.

| `servers.default` | Behaviour                                               |
| ----------------- | ------------------------------------------------------- |
| `ALL`             | Round-robin across all defined servers                  |
| `DUNNO`           | No route — relay returns 451, Postfix retries or defers |
| `<group>`         | Round-robin within that group                           |

### MX-based recipient matching

Recipient rules can also match on the MX records of the recipient domain (substring match). This lets you catch all Microsoft-hosted domains even when they don't end in `microsoft.com`:

```yaml
mx_rules:
  protection.outlook.com: microsoft # matches hotmail-com.olc.protection.outlook.com
```

---

## Logging and monitoring

### Log file

Set `verbose: true` to log full envelope details and routing decisions. Set `verbose: false` in production to log only errors and statistics.

### CSV file

Every routed message is appended to the CSV in the format:

```
date;sender;recipient;group;host;client_ip;direction
```

Direction is `INCOMING` or `OUTGOING`.

### Statistics

On SIGINT or SIGTERM the service prints per-group statistics before exiting:

```
Group good
  Name          # Sent |  curr. % / target %
    mx1         19,654 |  40.0008 /  40.0000
    mx2         19,653 |  39.9988 /  40.0000
    mx3          9,827 |  20.0004 /  20.0000

Group bad
  Name          # Sent |  curr. % / target %
    mx4         79,248 |  32.2579 /  32.2581
    mx5          7,925 |   3.2259 /   3.2258
    mx6         79,248 |  32.2579 /  32.2581
    mx7         79,249 |  32.2583 /  32.2581
```

---

## Test suite

```bash
# Run all tests (except load tests):
python3 tests/run_all.py

# Individual tests:
python3 tests/test_domain_lookup.py    # MX-based recipient matching
python3 tests/test_simple.py           # Smoke test — prints routing CSV
python3 tests/test_full.py             # Full routing assertions (all 53 pairs)
python3 tests/test_direction.py        # Inbound vs outbound direction detection
python3 tests/test_roundrobin.py       # default=ALL / DUNNO / dry-run
python3 tests/test_auto_populate.py    # Auto-populate local_domains from virtual file
python3 tests/test_improper_usage.py   # IP blocking, dry-run, null sender, bad address
python3 tests/test_rules.py            # Rule debugger against a historical CSV

# Load tests:
python3 tests/load_test.py             # Sequential throughput
python3 tests/load_concurrent.py       # Concurrent throughput (10 threads)

# Live smoke test against a running instance:
python3 tests/test_external_service.py [-H host] [-p port]
```

---

## License

BSD 3-Clause — see `LICENSE`.

https://github.com/riczorn/jolly-relay
