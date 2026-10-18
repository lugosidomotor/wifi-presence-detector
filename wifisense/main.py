"""CLI entrypoint: python -m wifisense.main; Uvicorn owns SIGINT/SIGTERM."""

import argparse
import logging
from pathlib import Path
import sys

from dotenv import load_dotenv
import uvicorn

from .config import ConfigError, load_config
from .utils import setup_logging
from .web import create_app


class Server(uvicorn.Server):
    """Uvicorn waits for open connections on shutdown; tell the event streams to finish first."""

    def handle_exit(self, sig, frame):
        self.config.app.state.stopping.set()
        super().handle_exit(sig, frame)


def main(argv=None):
    parser = argparse.ArgumentParser(description="WiFi motion and presence sensing with alarms and a local web UI")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env-file", default=".env", help="Optional dotenv file; exported environment wins")
    parser.add_argument("--demo", action="store_true", help="Built-in room with three simulated links")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv(args.env_file, override=False)
    try:
        cfg = load_config(args.config, demo=args.demo)
    except (ConfigError, OSError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.check_config:
        links = ", ".join(f"{link['id']} ({link['source']})" for link in cfg["links"])
        print(f"Configuration OK (secrets hidden): mode={cfg['source']['mode']}, links: {links}")
        return 0
    setup_logging(Path(cfg["data_dir"]), cfg["logging"]["level"])
    try:
        Server(uvicorn.Config(
            create_app(cfg),
            host=cfg["web"]["host"],
            port=cfg["web"]["port"],
            log_level=cfg["logging"]["level"].lower(),
            access_log=False,
            timeout_graceful_shutdown=10,
        )).run()
    except Exception as exc:
        logging.getLogger(__name__).error("application failed (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
