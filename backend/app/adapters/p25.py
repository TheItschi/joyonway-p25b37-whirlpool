"""P25 model family adapter — byte map and entity definitions.

Ported verbatim from https://github.com/alexbde/ha-joyonway (MIT License).
Byte positions validated by that project against local RS485 captures.
All indexes are 0-based logical-frame positions (after full-frame unescape).
"""

from __future__ import annotations

from typing import ClassVar

from .base import (
    JoyonwayBaseAdapter,
    JetDescription,
    JetType,
    SpaEntityDescription,
    MASK_OZONE_MODE_MANUAL,
    MASK_HEATER_MODE_MANUAL,
    MASK_BLOWER_CONFIG,
    celsius_to_fahrenheit,
)

# Broadcast header for P25 (bytes 0-8). Byte 7 is the board version and is
# matched against KNOWN_BOARD_VERSIONS; byte 8 (0x03) is the family ID,
# shared by P25B85 and P25B37.
P25_SIGNATURE = bytes([0x1A, 0xFF, 0x01, 0x3C, 0xD2, 0xB4, 0xFF, 0x08, 0x03])

# Jet masks
MASK_JET_LOW = 0x02
MASK_JET_HIGH = 0x04

LIGHT_COLOR_INDEX_TO_NAME: dict[int, str] = {
    1: "auto",
    2: "red",
    3: "green",
    4: "yellow",
    5: "blue",
    6: "purple",
    7: "cyan",
    8: "white",
}
LIGHT_COLOR_NAME_TO_INDEX: dict[str, int] = {
    v: k for k, v in LIGHT_COLOR_INDEX_TO_NAME.items()
}

_JET_TARGET_BYTES: dict[str, tuple[int, int]] = {
    "off": (0x04, 0x00),
    "low": (0x02, 0x02),
    "high": (0x06, 0x04),
}


class P25BaseAdapter(JoyonwayBaseAdapter):
    """Base adapter for the Joyonway P25 model family."""

    model: str
    broadcast_signature: bytes = P25_SIGNATURE
    supports_writes: bool = True
    jets: list[JetDescription] = [
        JetDescription(id="jets", name="Jets", type=JetType.DUAL),
    ]
    supported_light_colors: list[str] = []
    has_blower: bool = True

    heater_state_map = {
        0x40: "off",
        0x50: "standby",
        0x51: "circulation",
        0x55: "heating",
        0x54: "heating",
        0x41: "ozone",
        0xC1: "ozone",
    }

    _cmd_prefix_byte = 0x20
    _cmd_context_flag = 0x10
    _context_byte: ClassVar[int]

    def color_index_to_name(self, index: int) -> str | None:
        return LIGHT_COLOR_INDEX_TO_NAME.get(index)

    def color_name_to_index(self, name: str) -> int | None:
        return LIGHT_COLOR_NAME_TO_INDEX.get(name)

    def get_jets_state(self, data: dict, jet_id: str) -> str:
        if jet_id == "jets":
            return data.get("jets", "off")
        return "off"

    def _post_parse_status(
        self,
        result: dict,
        frame: bytes,
        jet_byte: int,
        ozone_mode_byte: int,
        heater_byte: int,
    ) -> None:
        if jet_byte & MASK_JET_HIGH:
            jets = "high"
        elif jet_byte & MASK_JET_LOW:
            jets = "low"
        else:
            jets = "off"

        result["jet_low"] = bool(jet_byte & MASK_JET_LOW)
        result["jet_high"] = bool(jet_byte & MASK_JET_HIGH)
        result["jets"] = jets

        ozone_mode_manual = bool(ozone_mode_byte & MASK_OZONE_MODE_MANUAL)
        heater_mode_manual = bool(ozone_mode_byte & MASK_HEATER_MODE_MANUAL)
        result["ozone_mode"] = "manual" if ozone_mode_manual else "auto"
        result["heater_mode"] = "manual" if heater_mode_manual else "auto"
        result["blower_present"] = bool(ozone_mode_byte & MASK_BLOWER_CONFIG)

    def _build_button_command(
        self,
        jet_b7: int = 0x00,
        jet_b8: int = 0x00,
        btn_group: int = 0x00,
        btn_action: int = 0x00,
        modifier: int = 0x00,
        context: int | None = None,
        setpoint_f: int = 0x62,
        tail_byte: int = 0x00,
    ) -> bytes:
        """Build a type-0xA1 button command frame with CRC."""
        from ..protocol import build_frame

        if context is None:
            context = self._context_byte

        payload = bytearray(
            [
                0x01,
                0x20,
                0x10,
                0x3C,
                0xA1,
                0x10,
                0xA1,
                jet_b7,
                jet_b8,
                btn_group,
                btn_action,
                modifier,
                context,
                0x00,
                setpoint_f,
                tail_byte,
            ]
        )
        return build_frame(bytes(payload))

    def build_light_command(self, on: bool, color: str | None = None) -> bytes:
        if not on:
            tail = 0x80
        elif color is not None:
            if color not in self.supported_light_colors:
                raise ValueError(f"Unsupported light color: {color}")
            tail = 0x80 + LIGHT_COLOR_NAME_TO_INDEX[color]
        else:
            tail = 0x81  # Default to auto-cycle ON

        return self._build_button_command(
            btn_group=0x40,
            btn_action=0x40,
            context=0x40,  # Light color commands on P25 use context 0x40
            tail_byte=tail,
        )

    def build_jets_command(self, jet_id: str, target: str) -> bytes | None:
        if jet_id != "jets" or target not in _JET_TARGET_BYTES:
            return None
        b7, b8 = _JET_TARGET_BYTES[target]
        return self._build_button_command(jet_b7=b7, jet_b8=b8)

    def build_heater_command(self, on: bool) -> bytes:
        return self._build_button_command(
            btn_group=0x08,
            btn_action=0x08 if on else 0x00,
        )

    def build_blower_command(self, on: bool) -> bytes:
        return self._build_button_command(
            btn_group=0x04,
            btn_action=0x0C if on else 0x00,
        )

    def build_temp_command(self, target_celsius: int) -> bytes | None:
        if target_celsius < self.temp_min_c or target_celsius > self.temp_max_c:
            return None
        target_f = celsius_to_fahrenheit(target_celsius)
        return self._build_button_command(
            btn_group=0x80,
            btn_action=0x98,
            setpoint_f=target_f,
        )

    def build_ozone_mode_command(self, mode: str, setpoint_f: int = 0x62) -> bytes:
        if mode == "auto":
            context = 0xC0
        elif mode == "manual":
            context = 0x40
        else:
            raise ValueError(f"Unsupported ozone mode: {mode}")

        return self._build_button_command(
            modifier=0x80,
            context=context,
            setpoint_f=setpoint_f,
        )

    def build_heater_mode_command(self, mode: str, setpoint_f: int = 0x62) -> bytes:
        if mode == "auto":
            context = 0x80
        elif mode == "manual":
            context = 0xC0
        else:
            raise ValueError(f"Unsupported heater mode: {mode}")

        return self._build_button_command(
            modifier=0x40,
            context=context,
            setpoint_f=setpoint_f,
        )

    def build_ozone_manual_command(self, on: bool, setpoint_f: int = 0x62) -> bytes:
        return self._build_button_command(
            btn_group=0x01,
            btn_action=0x01 if on else 0x10,
            context=0x40,
            setpoint_f=setpoint_f,
        )

    def entity_descriptions(self) -> list[SpaEntityDescription]:
        return _P25_ENTITIES


class P25B85Adapter(P25BaseAdapter):
    """Adapter for the Joyonway P25B85 controller."""

    model = "P25B85"
    _context_byte = 0xC0


class P25B37Adapter(P25BaseAdapter):
    """Adapter for the Joyonway P25B37 controller (this project's target)."""

    model = "P25B37"
    _context_byte = 0x40
    has_blower = False
    supported_light_colors = [
        "auto",
        "red",
        "green",
        "yellow",
        "blue",
        "purple",
        "cyan",
        "white",
    ]
    _ozone_bit = 0x80
    heater_state_map = {
        0x00: "off",
        0x01: "ozone",
        0x10: "standby",
        0x11: "circulation",
        0x14: "heating",
        0x15: "heating",
        0x81: "ozone",
    }


_P25_ENTITIES: list[SpaEntityDescription] = [
    SpaEntityDescription(
        platform="sensor",
        key="current_temperature",
        name="Current temperature",
        icon="mdi:thermometer-water",
        device_class="temperature",
        state_class="measurement",
        native_unit="°C",
    ),
    SpaEntityDescription(
        platform="sensor",
        key="setpoint",
        name="Setpoint temperature",
        icon="mdi:thermometer-check",
        device_class="temperature",
        state_class="measurement",
        native_unit="°C",
    ),
    SpaEntityDescription(
        platform="sensor",
        key="status",
        name="Status",
        icon="mdi:waves",
        icon_map={
            "off": "mdi:waves",
            "standby": "mdi:timer-sand",
            "circulation": "mdi:pump",
            "heating": "mdi:fire",
            "ozone": "mdi:shield-sun",
            "unknown": "mdi:help-circle-outline",
        },
        device_class="enum",
        options=["off", "standby", "circulation", "heating", "ozone", "unknown"],
    ),
    SpaEntityDescription(
        platform="sensor",
        key="jets",
        name="Jets",
        icon="mdi:weather-windy",
        device_class="enum",
        options=["off", "low", "high"],
    ),
    SpaEntityDescription(
        platform="sensor",
        key="spa_datetime",
        name="Spa clock",
        icon="mdi:clock-outline",
        entity_category="diagnostic",
        enabled_by_default=False,
    ),
]
