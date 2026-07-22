from __future__ import annotations

from deepem.devices.abstract import DeviceAdapter


class DeviceRegistry:
    def __init__(self) -> None:
        self._items: dict[str, DeviceAdapter] = {}

    def register(self, adapter: DeviceAdapter) -> None:
        self._items[adapter.device_id] = adapter

    def get(self, device_id: str) -> DeviceAdapter:
        return self._items[device_id]

    def find_by_capability(self, capability: str) -> DeviceAdapter | None:
        for adapter in self._items.values():
            if any(item.name == capability for item in adapter.list_capabilities()):
                return adapter
        return None

    def list_device_ids(self) -> list[str]:
        return list(self._items.keys())
