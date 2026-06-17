from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from deepem.protocol import EvidenceRef


@dataclass(slots=True)
class DeviceCapability:
    name: str
    description: str


@dataclass(slots=True)
class DeviceCommand:
    device_id: str
    capability: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DeviceResult:
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    attachments: list[EvidenceRef] = field(default_factory=list)
    error: str | None = None


class DeviceAdapter(Protocol):
    device_id: str

    def list_capabilities(self) -> list[DeviceCapability]: ...

    def execute(self, command: DeviceCommand) -> DeviceResult: ...
