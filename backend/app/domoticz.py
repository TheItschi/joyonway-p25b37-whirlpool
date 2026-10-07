"""Periodic push of spa state and measurements to Domoticz virtual devices (HTTP GET).

Configuration lives in a JSON file in the data volume (default /data/domoticz.json).
It is re-read before every cycle, so edits take effect without a restart.
A value is only sent when its idx is set AND the value itself is not empty.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

_LOGGER = logging.getLogger("hottub.domoticz")

DEFAULT_CONFIG: dict[str, Any] = {
    "_hilfe": (
        "url = Domoticz-Basisadresse, z.B. http://192.168.100.100:8080 (leer = Funktion aus). "
        "idx = Nummer des virtuellen Geräts, leer = Wert wird nicht übertragen. "
        "Gerätetypen siehe README."
    ),
    "url": "",
    "username": "",
    "password": "",
    "interval_s": 60,
    "idx": {
        "temperature": "",
        "setpoint": "",
        "heater": "",
        "heating": "",
        "light": "",
        "ozone": "",
        "jets": "",
        "light_color": "",
        "status": "",
        "heater_mode": "",
        "ozone_mode": "",
    },
}

STATUS_LABELS = {"off": "Aus", "standby": "Bereit", "circulation": "Umwälzung", "heating": "Heizt", "ozone": "Ozon"}
JET_LEVELS = {"off": 0, "low": 10, "high": 20}  # Selector switch: Off=0, Low=10, High=20


def _empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _udevice(idx: str, svalue: Any) -> str:
    return f"type=command&param=udevice&idx={idx}&nvalue=0&svalue={urllib.parse.quote(str(svalue), safe='')}"


def _switch(idx: str, on: bool) -> str:
    return f"type=command&param=switchlight&idx={idx}&switchcmd={'On' if on else 'Off'}"


def _level(idx: str, level: int) -> str:
    return f"type=command&param=switchlight&idx={idx}&switchcmd=Set%20Level&level={level}"


def build_queries(data: dict[str, Any], idx_map: dict[str, Any], color_name: Callable[[int], str | None]) -> list[tuple[str, str]]:
    """Return [(name, query-string)] for every value that has an idx and a non-empty value."""
    out: list[tuple[str, str]] = []

    def add(name: str, builder: Callable[[str, Any], str], value: Any) -> None:
        idx = idx_map.get(name)
        if _empty(idx) or _empty(value):
            return
        out.append((name, builder(str(idx).strip(), value)))

    add("temperature", _udevice, data.get("current_temperature"))
    add("setpoint", _udevice, data.get("setpoint"))
    for name, key in (("heater", "heater_enabled"), ("heating", "heater_active"), ("light", "light"), ("ozone", "ozone_active")):
        v = data.get(key)
        add(name, lambda i, val: _switch(i, bool(val)), v if v is not None else None)
    jets = data.get("jets")
    add("jets", lambda i, val: _level(i, JET_LEVELS[val]), jets if jets in JET_LEVELS else None)
    cidx = data.get("light_color_index")
    cname = color_name(cidx) if isinstance(cidx, int) and data.get("light") else None
    add("light_color", _udevice, cname)
    add("status", _udevice, STATUS_LABELS.get(data.get("status") or "", data.get("status")))
    add("heater_mode", _udevice, data.get("heater_mode"))
    add("ozone_mode", _udevice, data.get("ozone_mode"))
    return out


def _http_get(url: str, user: str, password: str) -> str:
    req = urllib.request.Request(url)
    if user:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310 (URL comes from the local config file)
        return resp.read().decode("utf-8", "replace")


class DomoticzPusher:
    def __init__(self, config_path: Path, get_data: Callable[[], dict | None], color_name: Callable[[int], str | None]) -> None:
        self.config_path = config_path
        self._get_data = get_data
        self._color_name = color_name
        self._task: asyncio.Task | None = None
        self.state: dict[str, Any] = {"enabled": False, "last_run": None, "sent": 0, "errors": [], "message": "nicht konfiguriert"}

    def ensure_config(self) -> None:
        if self.config_path.exists():
            return
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
            _LOGGER.info("Domoticz config template written to %s", self.config_path)
        except OSError as err:
            _LOGGER.warning("Could not write %s: %s", self.config_path, err)

    def load_config(self) -> dict[str, Any]:
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as err:
            self.state.update(enabled=False, message=f"Konfiguration unlesbar: {err}")
            _LOGGER.error("domoticz.json unreadable: %s", err)
            return {}

    async def run_once(self) -> None:
        cfg = self.load_config()
        base = str(cfg.get("url") or "").strip().rstrip("/")
        if not base:
            self.state.update(enabled=False, message="nicht konfiguriert (url leer)")
            return
        data = self._get_data()
        if not data or data.get("current_temperature") is None:
            self.state.update(enabled=True, message="warte auf Controller-Daten")
            return
        queries = build_queries(data, cfg.get("idx") or {}, self._color_name)
        errors: list[str] = []
        sent = 0
        for name, q in queries:
            url = f"{base}/json.htm?{q}"
            try:
                body = await asyncio.to_thread(_http_get, url, str(cfg.get("username") or ""), str(cfg.get("password") or ""))
                try:
                    ok = json.loads(body).get("status") == "OK"
                except ValueError:
                    ok = False
                if ok:
                    sent += 1
                else:
                    errors.append(f"{name}: {body[:80]}")
            except (urllib.error.URLError, OSError, ValueError) as err:
                errors.append(f"{name}: {err}")
        self.state.update(
            enabled=True,
            last_run=time.strftime("%Y-%m-%dT%H:%M:%S"),
            sent=sent,
            errors=errors,
            message="ok" if not errors else f"{len(errors)} Fehler",
        )
        if errors:
            _LOGGER.warning("Domoticz: %d/%d values sent, errors: %s", sent, len(queries), "; ".join(errors))
        else:
            _LOGGER.debug("Domoticz: %d values sent", sent)

    async def _loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                await self.run_once()
            except Exception:
                _LOGGER.exception("Domoticz push failed")
            interval = 60
            try:
                interval = max(10, int(self.load_config().get("interval_s", 60)))
            except (TypeError, ValueError):
                pass
            await asyncio.sleep(interval)

    def start(self) -> None:
        self.ensure_config()
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
