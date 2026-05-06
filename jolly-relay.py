#!/usr/bin/env python3
"""
Jolly Relay - async SMTP relay server

Usage:
    python3 jolly-relay.py [options]

Options:
    -c, --config FILE    Path to configuration file (default: jolly-relay.yaml)
    -p, --port PORT      Port to listen on (default: 9725)
    -H, --host HOST      Host to bind to (default: 127.0.0.1)
    --cache-ttl SEC      Cache TTL in seconds (default: 3600; 0 disables)
    --timeout SEC        Client inactivity timeout in seconds (default: 600)
    -v, --verbose        Enable verbose logging

Configuration File Format:
    See jolly-relay.yaml.example for a fully annotated example.
"""

import os
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import src.config as cfg
from src.service import RelayService
from src.router import get_mx_for_message


def _make_router(config, service):
    async def router(sender, recipient):
        return await get_mx_for_message(sender, recipient, config, service)
    return router


config = cfg.Config()
service = RelayService(config, None)
service.mx_router = _make_router(config, service)


def main():
    cfg.config = config
    service.run()


if __name__ == "__main__":
    main()
