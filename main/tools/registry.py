from __future__ import annotations

from typing import Any

from deepem.tools.base import ToolContext, ToolDefinition, ToolExecutionResult


class ToolValidationError(ValueError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._items: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        self._items[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        return self._items[name]

    def specs(self, allowed_tools: list[str]) -> list[ToolDefinition]:
        return [self._items[name] for name in allowed_tools]

    def execute(self, name: str, args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
        definition = self._items[name]
        validated_args = self._validate_value(args, definition.input_schema, path="args")
        if not isinstance(validated_args, dict):
            raise ToolValidationError(f"Tool '{name}' expects an object argument payload.")
        return definition.handler(validated_args, context)

    def _validate_value(self, value: object, schema: dict[str, Any], *, path: str) -> object:
        schema_type = schema.get("type")
        if schema_type is None:
            return value
        if schema_type == "object":
            if not isinstance(value, dict):
                raise ToolValidationError(f"{path} must be an object.")
            properties = schema.get("properties", {}) or {}
            required = schema.get("required", []) or []
            missing = [item for item in required if item not in value]
            if missing:
                missing_text = ", ".join(sorted(missing))
                raise ToolValidationError(f"{path} is missing required field(s): {missing_text}.")
            additional_properties = schema.get("additionalProperties")
            allow_extra = additional_properties if additional_properties is not None else not properties
            validated: dict[str, object] = {}
            for key, item in value.items():
                property_schema = properties.get(key)
                if property_schema is not None:
                    validated[key] = self._validate_value(item, property_schema, path=f"{path}.{key}")
                    continue
                if allow_extra is True:
                    validated[key] = item
                    continue
                if isinstance(allow_extra, dict):
                    validated[key] = self._validate_value(item, allow_extra, path=f"{path}.{key}")
                    continue
                raise ToolValidationError(f"{path}.{key} is not allowed by the tool schema.")
            return validated
        if schema_type == "array":
            if not isinstance(value, list):
                raise ToolValidationError(f"{path} must be an array.")
            item_schema = schema.get("items")
            if not item_schema:
                return list(value)
            return [self._validate_value(item, item_schema, path=f"{path}[{index}]") for index, item in enumerate(value)]
        if schema_type == "string":
            if not isinstance(value, str):
                raise ToolValidationError(f"{path} must be a string.")
            return value
        if schema_type == "boolean":
            if not isinstance(value, bool):
                raise ToolValidationError(f"{path} must be a boolean.")
            return value
        if schema_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolValidationError(f"{path} must be an integer.")
            return value
        if schema_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolValidationError(f"{path} must be a number.")
            return value
        return value
