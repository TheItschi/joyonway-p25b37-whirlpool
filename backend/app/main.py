"""FastAPI backend for the Joyonway hot tub web app.

Serves:
- GET  /api/status              current parsed spa state + connection info
- GET  /api/history             temperature history (1 sample/min, 24 h)
- POST /api/heater               {"on": bool}
- POST /api/heater/mode          {"mode": "auto"|"manual"}
- POST /api/jets                 {"target": "off"|"low"|"high"}   (P25 dual-speed pump)
- POST /api/light                {"on": bool, "color": "red"|...|null}
- POST /api/temperature          {"celsius": int}
- POST /api/ozone/mode           {"mode": "auto"|"manual"}   (if supported)
- POST /api/ozone/manual         {"on": bool}
- POST /api/schedule/{heat|filter}  {"slots":[{"start":"HH:MM","end":"HH:MM","enabled":bool} x2]}
- GET/POST /api/modes, PUT/DELETE /api/modes/{id}, POST /api/modes/{id}/apply   (named presets)
- WS   /ws                       pushes the latest status dict on every update

Static files (the frontend) are served from /frontend, mounted at "/".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Any
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .domoticz import DomoticzPusher
from .coordinator import SEND_DELAYS, IntentBuildError, JoyonwayCoordinator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOGGER = logging.getLogger("hottub.main")

BRIDGE_HOST = os.environ.get("HOTTUB_BRIDGE_HOST", "192.168.100.210")
BRIDGE_PORT = int(os.environ.get("HOTTUB_BRIDGE_PORT", "8899"))
SPA_MODEL = os.environ.get("HOTTUB_MODEL", "P25B37")
# HOTTUB_JETS_DIRECT=1 (default, verified on the real P25B37): jump directly to the target
# jets level. =0 steps through the panel cycle Aus->Niedrig->Hoch.
JETS_DIRECT = os.environ.get("HOTTUB_JETS_DIRECT", "1") == "1"

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

coordinator: JoyonwayCoordinator | None = None
domoticz: DomoticzPusher | None = None
_ws_clients: set[WebSocket] = set()

# In-memory temperature history for the chart: one sample per minute, 24 h.
# Lost on container restart (acceptable: it is only a display aid).
HISTORY_INTERVAL_S = 60
HISTORY_MAX_POINTS = 24 * 60
_history: deque[dict] = deque(maxlen=HISTORY_MAX_POINTS)


def _broadcast_update() -> None:
    """Called synchronously from the coordinator on every parsed frame."""
    if not _ws_clients:
        return
    payload = json.dumps({"type": "status", "data": coordinator.data})
    for ws in list(_ws_clients):
        asyncio.create_task(_safe_send(ws, payload))


async def _safe_send(ws: WebSocket, payload: str) -> None:
    try:
        await ws.send_text(payload)
    except Exception:
        _ws_clients.discard(ws)


async def _history_sampler() -> None:
    """Append (current, setpoint) to the history once per interval."""
    while True:
        await asyncio.sleep(HISTORY_INTERVAL_S)
        data = coordinator.data if coordinator is not None else None
        if not data or data.get("current_temperature") is None:
            continue
        _history.append(
            {
                "t": int(time.time()),
                "v": data.get("current_temperature"),
                "s": data.get("setpoint"),
            }
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global coordinator
    coordinator = JoyonwayCoordinator(BRIDGE_HOST, BRIDGE_PORT, SPA_MODEL)
    coordinator.register_data_callback(_broadcast_update)
    await coordinator.start()
    _LOGGER.info("Coordinator started for model=%s bridge=%s:%s", SPA_MODEL, BRIDGE_HOST, BRIDGE_PORT)
    sampler = asyncio.create_task(_history_sampler())
    auto_sync = asyncio.create_task(_auto_time_sync()) if AUTO_TIME_SYNC else None
    global domoticz
    domoticz = DomoticzPusher(DATA_DIR / "domoticz.json", lambda: coordinator.data if coordinator else None, lambda i: coordinator.adapter.color_index_to_name(i))
    domoticz.start()
    yield
    domoticz.stop()
    sampler.cancel()
    if auto_sync:
        auto_sync.cancel()
    await coordinator.stop()


app = FastAPI(title="Hot Tub Control", lifespan=lifespan)


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """Browsers must revalidate the UI files, otherwise an old index.html can be paired with a new app.js."""
    response = await call_next(request)
    if not request.url.path.startswith(("/api", "/ws")):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _require_coordinator() -> JoyonwayCoordinator:
    if coordinator is None:
        raise HTTPException(status_code=503, detail="Coordinator not initialized")
    return coordinator


# ── status & diagnostics ────────────────────────────────────────────────


@app.get("/api/status")
async def get_status() -> dict:
    co = _require_coordinator()
    return {
        "connected": co.available,
        "model": co.model,
        "bridge": f"{BRIDGE_HOST}:{BRIDGE_PORT}",
        "data": co.data,
        "rx_frame_stats": co.rx_frame_stats,
        "unsupported_board_version": co.unsupported_board_version,
        "last_unrecognized_frame": co.last_unrecognized_frame,
        "capabilities": {
            "jets": [{"id": j.id, "name": j.name, "type": j.type} for j in co.adapter.jets],
            "supported_light_colors": co.adapter.supported_light_colors,
            "has_blower": co.adapter.has_blower,
            "temp_min_c": co.adapter.temp_min_c,
            "temp_max_c": co.adapter.temp_max_c,
        },
    }


@app.get("/api/domoticz")
async def get_domoticz_state() -> dict:
    return domoticz.state if domoticz else {"enabled": False}


@app.get("/api/history")
async def get_history() -> dict:
    """Temperature samples (1/min, max 24 h) for the chart."""
    return {"interval_s": HISTORY_INTERVAL_S, "points": list(_history)}


# ── commands ─────────────────────────────────────────────────────────────


class HeaterRequest(BaseModel):
    on: bool


@app.post("/api/heater")
async def set_heater(req: HeaterRequest) -> dict:
    co = _require_coordinator()
    # In timer (auto) mode the controller decides itself; manual on/off is ignored there.
    if (co.data or {}).get("heater_mode") == "auto":
        raise HTTPException(status_code=409, detail="Zeitgesteuerter Betrieb aktiv: erst auf manuell umschalten")

    def build(overrides: dict, data: dict | None) -> bytes | None:
        if data is not None and data.get("heater_enabled") == overrides["on"]:
            return None
        return co.adapter.build_heater_command(overrides["on"])

    co.intent_queue.submit(
        group="heater",
        overrides={"on": req.on, "heater_enabled": req.on},
        build_fn=build,
        verify_fn=lambda overrides, data: (data or {}).get("heater_enabled") == overrides["on"],
    )
    return {"accepted": True}


class HeaterModeRequest(BaseModel):
    mode: str  # "auto" | "manual"


@app.post("/api/heater/mode")
async def set_heater_mode(req: HeaterModeRequest) -> dict:
    co = _require_coordinator()
    if req.mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail="mode must be auto or manual")

    def build(overrides: dict, data: dict | None) -> bytes | None:
        if data is not None and data.get("heater_mode") == overrides["mode"]:
            return None
        return co.adapter.build_heater_mode_command(overrides["mode"])

    co.intent_queue.submit(
        group="heater_mode",
        overrides={"mode": req.mode, "heater_mode": req.mode},
        build_fn=build,
        verify_fn=lambda overrides, data: (data or {}).get("heater_mode") == overrides["mode"],
    )
    return {"accepted": True}


class JetsRequest(BaseModel):
    jet_id: str = "jets"
    target: str  # "off" | "low" | "high"


# The P25 pump runs through a fixed cycle (like the touchpad button):
# off -> low -> high -> off. The three command frames are exactly the frames
# for these adjacent transitions, so a jump (e.g. high -> low) is executed as
# a sequence of confirmed single steps (high -> off -> low).
_JET_CYCLE = ["off", "low", "high"]
_jets_target: str | None = None
_jets_task: asyncio.Task | None = None


async def _jets_worker() -> None:
    co = _require_coordinator()
    stalled = 0
    while True:
        target = _jets_target
        current = (co.data or {}).get("jets")
        if target is None or current is None or current == target:
            return
        if JETS_DIRECT:
            nxt = target  # experimental: send the target frame directly, no stepping
        else:
            nxt = _JET_CYCLE[(_JET_CYCLE.index(current) + 1) % len(_JET_CYCLE)]
        frame = co.adapter.build_jets_command("jets", nxt)
        if frame is None:
            _LOGGER.error("jets: no frame for step %s -> %s", current, nxt)
            return
        ok = await co.send_and_verify(
            frame,
            lambda d, nxt=nxt: (d or {}).get("jets") == nxt,
            label=f"jets {current}->{nxt} (target {target})",
        )
        if not ok:
            stalled += 1
            if stalled >= 2:
                return
        else:
            stalled = 0
            await asyncio.sleep(0.5)  # small gap between steps


@app.post("/api/jets")
async def set_jets(req: JetsRequest) -> dict:
    global _jets_target, _jets_task
    co = _require_coordinator()
    if req.jet_id != "jets" or req.target not in _JET_CYCLE:
        raise HTTPException(status_code=400, detail=f"Unsupported jets request: {req.jet_id}/{req.target}")
    _jets_target = req.target  # last request wins
    if _jets_task is None or _jets_task.done():
        _jets_task = asyncio.create_task(_jets_worker())
    return {"accepted": True}


class LightRequest(BaseModel):
    on: bool
    color: str | None = None


@app.post("/api/light")
async def set_light(req: LightRequest) -> dict:
    co = _require_coordinator()

    def build(overrides: dict, data: dict | None) -> bytes | None:
        return co.adapter.build_light_command(overrides["on"], overrides.get("color"))

    color_idx = co.adapter.color_name_to_index(req.color) if (req.on and req.color) else None

    def verify(overrides: dict, data: dict | None) -> bool:
        data = data or {}
        if data.get("light") != overrides["on"]:
            return False
        # A colour change while the light is already on must be verified too,
        # otherwise a lost frame is never retried.
        if color_idx is not None and data.get("light_color_index") != color_idx:
            return False
        return True

    co.intent_queue.submit(
        group="light",
        overrides={"on": req.on, "color": req.color, "light": req.on},
        build_fn=build,
        verify_fn=verify,
    )
    return {"accepted": True}


class TemperatureRequest(BaseModel):
    celsius: int


@app.post("/api/temperature")
async def set_temperature(req: TemperatureRequest) -> dict:
    co = _require_coordinator()
    if not (co.adapter.temp_min_c <= req.celsius <= co.adapter.temp_max_c):
        raise HTTPException(
            status_code=400,
            detail=f"Temperature must be between {co.adapter.temp_min_c} and {co.adapter.temp_max_c} °C",
        )

    def build(overrides: dict, data: dict | None) -> bytes | None:
        if data is not None and data.get("setpoint") == overrides["celsius"]:
            return None
        return co.adapter.build_temp_command(overrides["celsius"])

    co.intent_queue.submit(
        group="temperature",
        overrides={"celsius": req.celsius, "setpoint": req.celsius},
        build_fn=build,
        verify_fn=lambda overrides, data: (data or {}).get("setpoint") == overrides["celsius"],
    )
    return {"accepted": True}


class OzoneModeRequest(BaseModel):
    mode: str  # "auto" | "manual"


@app.post("/api/ozone/mode")
async def set_ozone_mode(req: OzoneModeRequest) -> dict:
    co = _require_coordinator()
    if not co.adapter.supports_mode_switching:
        raise HTTPException(status_code=400, detail="This model does not support ozone mode switching")

    def build(overrides: dict, data: dict | None) -> bytes | None:
        return co.adapter.build_ozone_mode_command(overrides["mode"])

    co.intent_queue.submit(
        group="ozone_mode",
        overrides={"mode": req.mode, "ozone_mode": req.mode},
        build_fn=build,
        verify_fn=lambda overrides, data: (data or {}).get("ozone_mode") == overrides["mode"],
    )
    return {"accepted": True}


class OzoneManualRequest(BaseModel):
    on: bool


@app.post("/api/ozone/manual")
async def set_ozone_manual(req: OzoneManualRequest) -> dict:
    co = _require_coordinator()

    def build(overrides: dict, data: dict | None) -> bytes | None:
        return co.adapter.build_ozone_manual_command(overrides["on"])

    co.intent_queue.submit(
        group="ozone_manual",
        overrides={"on": req.on, "ozone_active": req.on},
        build_fn=build,
        verify_fn=lambda overrides, data: bool((data or {}).get("ozone_active")) == overrides["on"],
    )
    return {"accepted": True}


# ── schedules (heat / filter, 2 slots each) ──────────────────────────────

_SCHED_KINDS = ("heat", "filter")
_sched_target: dict[str, list[dict]] = {}
_sched_tasks: dict[str, asyncio.Task] = {}


class SlotModel(BaseModel):
    start: str  # "HH:MM"
    end: str
    enabled: bool


class ScheduleRequest(BaseModel):
    slots: list[SlotModel]


def _parse_hm(text: str) -> tuple[int, int]:
    try:
        h, m = text.split(":")
        h, m = int(h), int(m)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid time: {text!r}") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise HTTPException(status_code=400, detail=f"Invalid time: {text!r}")
    return h, m


def _current_slots(kind: str, data: dict) -> list[dict] | None:
    out = []
    for n in (1, 2):
        st, en, on = data.get(f"{kind}_slot{n}_start"), data.get(f"{kind}_slot{n}_end"), data.get(f"{kind}_slot{n}_enabled")
        if st is None or en is None or on is None:
            return None
        out.append({"start": tuple(st), "end": tuple(en), "enabled": bool(on)})
    return out


async def _write_schedule(co: JoyonwayCoordinator, kind: str, target: list[dict]) -> bool:
    """Write the 2 slots of `kind` and wait for the controller to report them."""
    cur = _current_slots(kind, co.data or {})
    if cur == target:
        return True
    # Enable flags only -> "state" write; any time change -> "time" write
    times_same = cur is not None and all(
        cur[i]["start"] == target[i]["start"] and cur[i]["end"] == target[i]["end"] for i in range(2)
    )
    mode = "state" if times_same else "time"
    frame = co.adapter.build_schedule_command(
        kind,
        target[0]["start"], target[0]["end"], target[1]["start"], target[1]["end"],
        target[0]["enabled"], target[1]["enabled"],
        write_mode=mode,
    )
    return await co.send_and_verify(
        frame,
        lambda d, t=target: _current_slots(kind, d or {}) == t,
        label=f"schedule {kind} ({mode} write)",
    )


async def _schedule_worker(kind: str) -> None:
    co = _require_coordinator()
    while kind in _sched_target:
        await _write_schedule(co, kind, _sched_target.pop(kind))
        await asyncio.sleep(0.5)


@app.post("/api/schedule/{kind}")
async def set_schedule(kind: str, req: ScheduleRequest) -> dict:
    _require_coordinator()
    if kind not in _SCHED_KINDS:
        raise HTTPException(status_code=404, detail="kind must be heat or filter")
    if len(req.slots) != 2:
        raise HTTPException(status_code=400, detail="exactly 2 slots required")
    _sched_target[kind] = [
        {"start": _parse_hm(sl.start), "end": _parse_hm(sl.end), "enabled": sl.enabled} for sl in req.slots
    ]  # last request wins
    task = _sched_tasks.get(kind)
    if task is None or task.done():
        _sched_tasks[kind] = asyncio.create_task(_schedule_worker(kind))
    return {"accepted": True}


# ── modes (named presets: 2 heat slots + 2 filter slots + setpoint) ─────────
# The controller only knows one active set. Named modes are stored here
# (JSON file in the data volume); activating one writes the three parts.

DATA_DIR = Path(os.environ.get("HOTTUB_DATA_DIR", "/data"))
MODES_FILE = DATA_DIR / "modes.json"
MAX_MODES = 20
_mode_apply: dict = {"id": None, "state": "idle", "step": ""}
_mode_task: asyncio.Task | None = None


class ModeModel(BaseModel):
    name: str
    setpoint: int
    heat: list[SlotModel]
    filter: list[SlotModel]


def _load_modes() -> list[dict]:
    try:
        return json.loads(MODES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        _LOGGER.exception("modes.json unreadable, starting empty")
        return []


def _save_modes(modes: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MODES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(modes, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, MODES_FILE)


def _validate_mode(m: ModeModel) -> dict:
    co = _require_coordinator()
    name = m.name.strip()
    if not name or len(name) > 40:
        raise HTTPException(status_code=400, detail="Name required (max 40 characters)")
    if not (co.adapter.temp_min_c <= m.setpoint <= co.adapter.temp_max_c):
        raise HTTPException(status_code=400, detail="Setpoint out of range")
    if len(m.heat) != 2 or len(m.filter) != 2:
        raise HTTPException(status_code=400, detail="2 heat and 2 filter slots required")
    for sl in (*m.heat, *m.filter):
        _parse_hm(sl.start)
        _parse_hm(sl.end)
    return {
        "name": name,
        "setpoint": m.setpoint,
        "heat": [sl.model_dump() for sl in m.heat],
        "filter": [sl.model_dump() for sl in m.filter],
    }


def _slots_of(raw: list[dict]) -> list[dict]:
    return [{"start": _parse_hm(x["start"]), "end": _parse_hm(x["end"]), "enabled": bool(x["enabled"])} for x in raw]


async def _apply_mode(mode: dict) -> None:
    co = _require_coordinator()
    _mode_apply.update(id=mode["id"], state="running", step="Heizzeiten")
    try:
        ok = await _write_schedule(co, "heat", _slots_of(mode["heat"]))
        if ok:
            _mode_apply["step"] = "Filterzeiten"
            ok = await _write_schedule(co, "filter", _slots_of(mode["filter"]))
        if ok:
            _mode_apply["step"] = "Solltemperatur"
            frame = co.adapter.build_temp_command(mode["setpoint"])
            ok = frame is None or await co.send_and_verify(
                frame, lambda d: (d or {}).get("setpoint") == mode["setpoint"], label=f"mode setpoint {mode['setpoint']}"
            )
        _mode_apply.update(state="done" if ok else "failed", step="")
    except Exception:
        _LOGGER.exception("apply mode failed")
        _mode_apply.update(state="failed", step="")


@app.get("/api/modes")
async def list_modes() -> dict:
    return {"modes": _load_modes(), "apply": _mode_apply, "max": MAX_MODES}


@app.post("/api/modes")
async def create_mode(m: ModeModel) -> dict:
    modes = _load_modes()
    if len(modes) >= MAX_MODES:
        raise HTTPException(status_code=400, detail=f"At most {MAX_MODES} schedules")
    new = {"id": uuid.uuid4().hex[:8], **_validate_mode(m)}
    modes.append(new)
    _save_modes(modes)
    return new


@app.put("/api/modes/{mode_id}")
async def update_mode(mode_id: str, m: ModeModel) -> dict:
    modes = _load_modes()
    for i, x in enumerate(modes):
        if x["id"] == mode_id:
            modes[i] = {"id": mode_id, **_validate_mode(m)}
            _save_modes(modes)
            return modes[i]
    raise HTTPException(status_code=404, detail="Schedule not found")


@app.delete("/api/modes/{mode_id}")
async def delete_mode(mode_id: str) -> dict:
    modes = _load_modes()
    rest = [x for x in modes if x["id"] != mode_id]
    if len(rest) == len(modes):
        raise HTTPException(status_code=404, detail="Schedule not found")
    _save_modes(rest)
    return {"deleted": True}


@app.post("/api/modes/{mode_id}/apply")
async def apply_mode(mode_id: str) -> dict:
    global _mode_task
    _require_coordinator()
    mode = next((x for x in _load_modes() if x["id"] == mode_id), None)
    if mode is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if _mode_task is not None and not _mode_task.done():
        raise HTTPException(status_code=409, detail="Another schedule is being applied")
    _mode_task = asyncio.create_task(_apply_mode(mode))
    return {"accepted": True}


# ── clock sync ───────────────────────────────────────────────────────────

_time_task: asyncio.Task | None = None
_time_sync: dict = {"state": "idle"}


class TimeSyncRequest(BaseModel):
    iso: str  # local wall-clock time of the browser, e.g. 2026-10-05T22:25:30


async def _time_sync_worker(offset: timedelta) -> None:
    """Set the spa clock to (server clock + offset). A fresh frame is built per attempt."""
    co = _require_coordinator()

    def target_now() -> datetime:
        return datetime.now() + offset

    def verify(d: dict | None) -> bool:
        raw = (d or {}).get("spa_datetime")
        if not raw:
            return False
        try:
            return abs((datetime.fromisoformat(raw) - target_now()).total_seconds()) <= 5
        except ValueError:
            return False

    _time_sync.update(state="running")
    event = asyncio.Event()
    co.register_data_callback(event.set)
    try:
        for attempt in range(1, 5):
            t = target_now()
            frame = co.adapter.build_datetime_command(t.year, t.month, t.day, t.hour, t.minute, t.second)
            await co.async_send_command(frame, SEND_DELAYS[(attempt - 1) % len(SEND_DELAYS)])
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if verify(co.data):
                    _LOGGER.info("[clock sync] confirmed on attempt %d", attempt)
                    _time_sync.update(state="done")
                    return
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=max(0.1, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    break
            _LOGGER.warning("[clock sync] attempt %d not confirmed", attempt)
        _LOGGER.error("[clock sync] not confirmed after 4 attempts")
        _time_sync.update(state="failed")
    finally:
        co.unregister_data_callback(event.set)


# Automatic clock sync: the container clock is UTC, so the local zone is configurable.
TIMEZONE = os.environ.get("HOTTUB_TZ", "Europe/Berlin")
AUTO_TIME_SYNC = os.environ.get("HOTTUB_AUTO_TIME_SYNC", "1") == "1"
AUTO_SYNC_THRESHOLD_S = 30
AUTO_SYNC_RETRY_S = 600  # after a failed/unnecessary attempt wait before trying again


def _local_offset() -> timedelta:
    """Offset that turns the server's naive clock into local wall-clock time."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None) - datetime.now()


async def _auto_time_sync() -> None:
    global _time_task
    last_try = 0.0
    while True:
        await asyncio.sleep(30)
        co = coordinator
        if co is None or not co.data or not co.available:
            continue
        raw = co.data.get("spa_datetime")
        if not raw or (_time_task is not None and not _time_task.done()):
            continue
        try:
            drift = (datetime.fromisoformat(raw) - (datetime.now() + _local_offset())).total_seconds()
        except Exception:
            continue
        if abs(drift) <= AUTO_SYNC_THRESHOLD_S or time.monotonic() - last_try < AUTO_SYNC_RETRY_S:
            continue
        last_try = time.monotonic()
        _LOGGER.info("[clock sync] spa clock off by %.0f s, syncing automatically", drift)
        _time_task = asyncio.create_task(_time_sync_worker(_local_offset()))


@app.post("/api/time/sync")
async def sync_time(req: TimeSyncRequest) -> dict:
    global _time_task
    _require_coordinator()
    try:
        target = datetime.fromisoformat(req.iso).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail="iso must be a local time like 2026-10-05T22:25:30") from None
    if _time_task is not None and not _time_task.done():
        raise HTTPException(status_code=409, detail="Clock sync already running")
    _time_task = asyncio.create_task(_time_sync_worker(target - datetime.now()))
    return {"accepted": True}


@app.get("/api/time/sync")
async def time_sync_state() -> dict:
    return _time_sync


# ── websocket ────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _ws_clients.add(ws)
    co = _require_coordinator()
    try:
        # Send current state immediately on connect
        await ws.send_text(json.dumps({"type": "status", "data": co.data}))
        while True:
            # We don't expect inbound messages, but need to await something
            # to detect disconnects.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


# ── static frontend ──────────────────────────────────────────────────────

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
