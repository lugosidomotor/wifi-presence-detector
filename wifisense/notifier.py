"""ntfy push (same topic as the camera stack) and an optional JSON webhook; called from a worker thread."""

from __future__ import annotations

import base64
import logging
import threading
from datetime import datetime

import requests

log = logging.getLogger(__name__)


def mime_header(value: str) -> str:
    """RFC 2047-encode non-ASCII text: ntfy decodes it, while http.client can only send Latin-1 headers."""
    return value if value.isascii() else "=?UTF-8?B?" + base64.b64encode(value.encode()).decode("ascii") + "?="


def alarm_text(event: dict, link_names: dict) -> tuple[str, str]:
    human = event.get("label") == "ember"
    title = ("🛑 WiFi riasztás · ember" if human else "⚠️ WiFi riasztás · mozgás")
    if event.get("simulated"):
        title += " · TESZT"
    links = sorted(event.get("links", {}).items(), key=lambda item: -item[1])
    where = ", ".join(link_names.get(link_id, link_id) for link_id, score in links if score >= 1) or "—"
    started = datetime.fromisoformat(event["timestamp"]).astimezone()
    message = f"Ember valószínűsége {event.get('human', 0):.0%} · {where} · {started:%H:%M:%S}"
    return title, message


class Notifier:
    def __init__(self, cfg: dict, link_names: dict):
        self.cfg = cfg["notifications"]
        self.link_names = link_names
        self.local = threading.local()

    @property
    def enabled(self):
        return self.cfg["enabled"] or bool(self.cfg["webhook_url"])

    def _session(self):
        session = getattr(self.local, "session", None)
        if session is None:
            session = self.local.session = requests.Session()
        return session

    def send_alarm(self, event: dict) -> str:
        """sent / failed / disabled; both channels are tried, one success is enough."""
        results = []
        if self.cfg["enabled"]:
            title, message = alarm_text(event, self.link_names)
            headers = {"Title": mime_header(title), "Priority": "high", "Tags": "rotating_light"}
            if self.cfg["token"]:
                headers["Authorization"] = f"Bearer {self.cfg['token']}"
            results.append(self._post(f"{self.cfg['url']}/{self.cfg['topic']}", data=message.encode(), headers=headers))
        if self.cfg["webhook_url"]:
            payload = {"type": "wifi_presence_alarm", "event": event}
            results.append(self._post(self.cfg["webhook_url"], json=payload))
        if not results:
            return "disabled"
        return "sent" if any(results) else "failed"

    def send_test(self) -> str:
        if not self.enabled:
            return "disabled"
        results = []
        if self.cfg["enabled"]:
            headers = {"Title": mime_header("✅ WiFi jelenlét · teszt"), "Priority": "default"}
            if self.cfg["token"]:
                headers["Authorization"] = f"Bearer {self.cfg['token']}"
            body = "A WiFi jelenlétérzékelő értesítései működnek.".encode()
            results.append(self._post(f"{self.cfg['url']}/{self.cfg['topic']}", data=body, headers=headers))
        if self.cfg["webhook_url"]:
            results.append(self._post(self.cfg["webhook_url"], json={"type": "wifi_presence_test"}))
        return "sent" if any(results) else "failed"

    def _post(self, url, **kwargs) -> bool:
        try:
            response = self._session().post(url, timeout=(3, 7), **kwargs)
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.warning("notification to %s failed (%s)", url.split("/")[2], type(exc).__name__)
            return False
