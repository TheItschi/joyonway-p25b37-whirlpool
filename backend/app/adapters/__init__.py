"""Model adapter registry for Joyonway spa controllers.

Ported from https://github.com/alexbde/ha-joyonway (MIT License), stripped
of Home Assistant dependencies. See LICENSE-ha-joyonway in the project root
for the original license text and copyright notice.
"""

from __future__ import annotations

from .base import JoyonwayBaseAdapter, ModelAdapter
from .p25 import P25B37Adapter, P25B85Adapter

_ADAPTERS: dict[str, type[JoyonwayBaseAdapter]] = {
    "P25B37": P25B37Adapter,
    "P25B85": P25B85Adapter,
}


def get_adapter(model: str) -> ModelAdapter:
    """Instantiate the adapter for the given model name."""
    try:
        adapter_cls = _ADAPTERS[model]
    except KeyError as err:
        raise ValueError(
            f"Unknown model '{model}'. Supported: {', '.join(_ADAPTERS)}"
        ) from err
    return adapter_cls()


def available_models() -> list[str]:
    """Return the list of supported model identifiers."""
    return list(_ADAPTERS)
