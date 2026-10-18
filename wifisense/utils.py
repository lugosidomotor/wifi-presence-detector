"""Rotating logs and the bounded side-effect worker (same contract as the camera stack)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import threading


def setup_logging(data_dir: Path, level: str):
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(logs / "wifisense.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)


class TaskWorker:
    """Bounded queue, timeout-controlled tasks, drain on shutdown."""

    def __init__(self, name, callback, capacity=64):
        self.queue = queue.Queue(maxsize=capacity)
        self.callback = callback
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self):
        self.thread.start()

    def submit(self, item):
        if self.stopping.is_set():
            return False
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            logging.getLogger(__name__).warning("side-effect queue full; task skipped")
            return False

    def _run(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                item = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.callback(item)
            except Exception as exc:
                logging.getLogger(__name__).error("background task failed (%s)", type(exc).__name__)
            finally:
                self.queue.task_done()

    def close(self, timeout=15):
        self.stopping.set()
        if self.thread.is_alive():
            self.thread.join(timeout=timeout)
        return not self.thread.is_alive()
