"""
Logging helpers for Jolly Relay.

Four output channels:
  log()         — operational messages (startup, errors, warnings); always shown
  log_debug()   — verbose-only console output
  log_to_file() — log file only, never console
  log_request() — per-message routing summary to console + log file

Structured fields
-----------------
Key log lines carry a structured suffix in the form:
  [key=value key=value ...]

This makes them easy to grep and parse by log-aggregation tools without
requiring JSON. Example:

  sender@x.com  user@gmail.com  gmail  mx1.example.com:25  250 OK  127.0.0.1
      [direction=OUTGOING group=gmail mx=mx1.example.com:25 action=250 OK]
"""

import sys

# Injected by Config.__init__; used by log_debug and log_request.
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
    if config and config.verbose:
        sys.stdout.write(f"{message}\n")
        sys.stdout.flush()


def log_to_file(message):
    """Write to log file only, never to console."""
    if config and config.logger:
        config.logger.info(message)


def _structured(**fields):
    """Format a dict of fields as a bracketed key=value string."""
    parts = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, ""))
    return f"[{parts}]" if parts else ""


def log_request(sender, recipient, group, mx, action, envelope=None,
                direction="", client_address="", sasl_username=""):
    """
    Per-request routing summary to console and log file.

    Tabular columns (unchanged for backward compat):
        sender  recipient  group  mx  action  client_ip  direction  [sasl]

    Followed by a structured field block for log-aggregation tools:
        [direction=… group=… mx=… action=… client=… sasl=…]
    """
    tab = f"{sender}\t{recipient}\t{group}\t{mx}\t{action}\t{client_address}"
    if direction:
        tab += f"\t{direction}"
    sasl_tag = f"sasl:{sasl_username}" if (sasl_username and sasl_username != sender) else ""
    if sasl_tag:
        tab += f"\t({sasl_tag})"

    struct = _structured(
        direction=direction,
        group=group,
        mx=mx,
        action=action,
        client=client_address,
        sasl=sasl_username if (sasl_username and sasl_username != sender) else "",
    )
    line = f"{tab}  {struct}" if struct else tab

    if config and config.verbose and envelope:
        payload = f"  from={envelope.mail_from}\n  to={envelope.rcpt_tos}"
        sys.stdout.write(f"{payload}\n{line}\n")
        sys.stdout.flush()
        log_to_file(f"{payload}\n{line}")
    else:
        sys.stdout.write(f"{line}\n")
        sys.stdout.flush()
        log_to_file(line)
