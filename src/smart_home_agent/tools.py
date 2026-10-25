"""LLM tool contracts and validated mock-device operations."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .storage import SQLiteStore


STATE_SCHEMA = {
    "type": "object",
    "properties": {
        "power": {"type": "boolean"},
        "temperature": {"type": "number"},
        "mode": {"type": "string"},
        "fan_speed": {"type": "integer"},
    },
    "additionalProperties": False,
}

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "device_id": {"type": "string"},
        "state": STATE_SCHEMA,
    },
    "required": ["device_id", "state"],
    "additionalProperties": False,
}


def _object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


TOOLS = [
    _tool(
        "list_devices",
        "List the mock home devices, rooms, capabilities, and current states.",
        _object_schema({}),
    ),
    _tool(
        "get_device_state",
        "Get the current state and capabilities for one device.",
        _object_schema({"device_id": {"type": "string"}}, ["device_id"]),
    ),
    _tool(
        "control_device",
        "Set only the requested fields on a device. Use a listed device ID and "
        "allowed values. Saved defaults and time rules are applied by the server; "
        "explicit fields in this request take precedence.",
        _object_schema(
            {"device_id": {"type": "string"}, "state": STATE_SCHEMA},
            ["device_id", "state"],
        ),
    ),
    _tool(
        "create_rule",
        "Create a persistent personalization rule from the user's explicit request. "
        "Use kind 'default' for settings applied when a device is turned on, 'phrase' "
        "for an exact spoken trigger, or 'time' for a daily after-time preference. "
        "Actions must name real device IDs and valid partial states. For phrase "
        "rules, include every requested state change for each device, including "
        "power=true or power=false when the user asks to switch it on or off; also "
        "include any requested temperature, mode, or fan speed.",
        _object_schema(
            {
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["default", "phrase", "time"]},
                "trigger_phrase": {"type": "string"},
                "after_time": {
                    "type": "string",
                    "description": "24-hour local time, HH:MM, for a time rule.",
                },
                "actions": {"type": "array", "items": ACTION_SCHEMA},
            },
            ["name", "kind", "actions"],
        ),
    ),
    _tool(
        "list_rules",
        "List saved personalization rules, including disabled rules.",
        _object_schema({}),
    ),
    _tool(
        "update_rule",
        "Update a saved rule. Supply the rule ID and only the fields the user asked "
        "to change; the server validates the complete resulting rule.",
        _object_schema(
            {
                "rule_id": {"type": "string"},
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["default", "phrase", "time"]},
                "trigger_phrase": {"type": "string"},
                "after_time": {"type": "string"},
                "actions": {"type": "array", "items": ACTION_SCHEMA},
            },
            ["rule_id"],
        ),
    ),
    _tool(
        "set_rule_enabled",
        "Enable or disable a saved rule without deleting it.",
        _object_schema(
            {"rule_id": {"type": "string"}, "enabled": {"type": "boolean"}},
            ["rule_id", "enabled"],
        ),
    ),
    _tool(
        "delete_rule",
        "Permanently delete one saved rule when the user asks to forget or remove it.",
        _object_schema({"rule_id": {"type": "string"}}, ["rule_id"]),
    ),
    _tool(
        "apply_trigger_rules",
        "Find and apply enabled phrase rules whose exact configured phrase appears "
        "in the current user message. Call only when the message is intended to "
        "invoke a routine, not when it merely discusses one.",
        _object_schema({}),
    ),
]


class SmartHomeTools:
    def __init__(
        self,
        store: SQLiteStore,
        timezone: ZoneInfo,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.timezone = timezone
        self.clock = clock or (lambda: datetime.now(timezone))

    @staticmethod
    def _validate_state(device: dict[str, Any], state: Any) -> dict[str, Any]:
        if not isinstance(state, dict) or not state:
            raise ValueError("State must contain at least one field to change.")
        unknown = set(state) - {"power", "temperature", "mode", "fan_speed"}
        if unknown:
            raise ValueError(f"Unsupported state field(s): {', '.join(sorted(unknown))}.")

        capabilities = device["capabilities"]
        validated: dict[str, Any] = {}
        if "power" in state:
            if not isinstance(state["power"], bool):
                raise ValueError("Power must be true or false.")
            validated["power"] = state["power"]
        if "temperature" in state:
            if device["type"] != "air_conditioner":
                raise ValueError("Only air conditioners have an adjustable temperature.")
            value = state["temperature"]
            limits = capabilities["temperature"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Temperature must be a number in degrees Celsius.")
            if not limits["min"] <= value <= limits["max"]:
                raise ValueError(
                    f"Temperature must be between {limits['min']} and "
                    f"{limits['max']}°C for {device['name']}."
                )
            validated["temperature"] = value
        if "mode" in state:
            if device["type"] != "air_conditioner":
                raise ValueError("Only air conditioners have operating modes.")
            if not isinstance(state["mode"], str):
                raise ValueError("Mode must be a supported mode name.")
            modes = capabilities["modes"]
            canonical_mode = next(
                (mode for mode in modes if mode.casefold() == state["mode"].casefold()),
                None,
            )
            if canonical_mode is None:
                raise ValueError(
                    f"Unsupported mode for {device['name']}. Choose from: "
                    f"{', '.join(modes)}."
                )
            validated["mode"] = canonical_mode
        if "fan_speed" in state:
            if device["type"] != "fan":
                raise ValueError("Only fans have an adjustable fan speed.")
            value = state["fan_speed"]
            limits = capabilities["fan_speed"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("Fan speed must be a whole number.")
            if not limits["min"] <= value <= limits["max"]:
                raise ValueError(
                    f"Fan speed must be between {limits['min']} and {limits['max']}."
                )
            validated["fan_speed"] = value
        return validated

    def _device(self, device_id: str) -> dict[str, Any]:
        device = self.store.get_device(device_id)
        if device is None:
            raise ValueError(f"Unknown device ID: {device_id}")
        return device

    def _validate_actions(self, actions: Any, kind: str) -> list[dict[str, Any]]:
        if not isinstance(actions, list) or not actions:
            raise ValueError("A rule needs at least one device action.")
        if len(actions) > 8:
            raise ValueError("A rule can control at most eight device actions.")
        validated: list[dict[str, Any]] = []
        seen_devices: set[str] = set()
        for action in actions:
            if not isinstance(action, dict) or set(action) != {"device_id", "state"}:
                raise ValueError("Each action needs exactly a device_id and a state.")
            device_id = action["device_id"]
            if not isinstance(device_id, str):
                raise ValueError("Action device_id must be a string.")
            if device_id in seen_devices:
                raise ValueError(f"A rule can include {device_id} only once.")
            seen_devices.add(device_id)
            device = self._device(device_id)
            state = self._validate_state(device, action["state"])
            if kind == "default" and "power" in state:
                raise ValueError(
                    "Default rules set preferences, not the power state; "
                    "omit power from this rule's action."
                )
            if kind == "time":
                if device["type"] != "fan":
                    raise ValueError("Time-conditioned preferences currently support fans only.")
                if "power" in state:
                    raise ValueError(
                        "Time-conditioned rules set fan speed preferences; "
                        "they do not change power."
                    )
            validated.append({"device_id": device_id, "state": state})
        return validated

    @staticmethod
    def _validate_rule(
        *,
        name: Any,
        kind: Any,
        trigger_phrase: Any,
        after_time: Any,
        actions: Any,
        validate_actions: Any,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("Rule name must be between 1 and 100 characters.")
        if not isinstance(kind, str) or kind not in {"default", "phrase", "time"}:
            raise ValueError("Rule kind must be default, phrase, or time.")
        if kind == "phrase":
            if not isinstance(trigger_phrase, str) or not trigger_phrase.strip():
                raise ValueError("A phrase rule needs a non-empty trigger phrase.")
            if len(trigger_phrase.strip()) > 100:
                raise ValueError("Trigger phrase cannot exceed 100 characters.")
            trigger_phrase = trigger_phrase.strip()
            after_time = None
        elif kind == "time":
            if not isinstance(after_time, str):
                raise ValueError("A time rule needs a local time in HH:MM format.")
            try:
                datetime.strptime(after_time, "%H:%M")
            except ValueError as error:
                raise ValueError("Time must use 24-hour HH:MM format.") from error
            trigger_phrase = None
        else:
            trigger_phrase = None
            after_time = None
        return {
            "name": name.strip(),
            "kind": kind,
            "trigger_phrase": trigger_phrase,
            "after_time": after_time,
            "actions": validate_actions(actions, kind),
        }

    def _create_rule(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rule = self._validate_rule(
            name=arguments.get("name"),
            kind=arguments.get("kind"),
            trigger_phrase=arguments.get("trigger_phrase"),
            after_time=arguments.get("after_time"),
            actions=arguments.get("actions"),
            validate_actions=self._validate_actions,
        )
        return {"rule": self.store.create_rule(**rule)}

    def _update_rule(self, arguments: dict[str, Any]) -> dict[str, Any]:
        existing = self.store.get_rule(arguments.get("rule_id", ""))
        if existing is None:
            raise ValueError(f"Unknown rule: {arguments.get('rule_id')}")
        merged = {
            "name": existing["name"],
            "kind": existing["kind"],
            "trigger_phrase": existing["trigger_phrase"],
            "after_time": existing["after_time"],
            "actions": existing["actions"],
        }
        for key in merged:
            if key in arguments:
                merged[key] = arguments[key]
        rule = self._validate_rule(
            **merged,
            validate_actions=self._validate_actions,
        )
        rule["rule_id"] = existing["rule_id"]
        return {"rule": self.store.update_rule(rule)}

    def _control_device(
        self,
        arguments: dict[str, Any],
        user_text: str,
    ) -> dict[str, Any]:
        device = self._device(arguments.get("device_id", ""))
        requested = self._validate_state(device, arguments.get("state"))
        explicit_request = self._explicit_request_state(user_text, device)
        requested.update(explicit_request)
        requested = self._validate_state(device, requested)
        now = self.clock().astimezone(self.timezone)
        defaults: dict[str, Any] = {}
        applied_rules: list[str] = []
        matching_rules: list[tuple[dict[str, Any], dict[str, Any]]] = []

        if requested.get("power") is True:
            candidates = sorted(
                self.store.list_rules(enabled_only=True),
                key=lambda rule: rule["updated_at"],
            )
            for rule in candidates:
                action = next(
                    (
                        item
                        for item in rule["actions"]
                        if item["device_id"] == device["device_id"]
                    ),
                    None,
                )
                if action is None:
                    continue
                if rule["kind"] == "default":
                    matching_rules.append((rule, action))
                elif (
                    rule["kind"] == "time"
                    and device["type"] == "fan"
                    and rule["after_time"] <= now.strftime("%H:%M")
                ):
                    matching_rules.append((rule, action))

        for _, action in matching_rules:
            for key, value in action["state"].items():
                if key not in explicit_request:
                    if requested.get(key) == value:
                        requested.pop(key)

        for rule, action in matching_rules:
            rule_defaults = {
                key: value
                for key, value in action["state"].items()
                if key not in requested
            }
            if rule_defaults:
                defaults.update(rule_defaults)
                applied_rules.append(rule["name"])

        next_state = dict(device["state"])
        next_state.update(defaults)
        next_state.update(requested)
        validated_next_state = self._validate_state(device, next_state)
        self.store.update_device_states({device["device_id"]: validated_next_state})
        return {
            "device": {
                **device,
                "state": validated_next_state,
            },
            "requested_changes": requested,
            "applied_rules": list(dict.fromkeys(applied_rules)),
        }

    @staticmethod
    def _explicit_request_state(
        user_text: str,
        device: dict[str, Any],
    ) -> dict[str, Any]:
        message = user_text.casefold()
        is_ac = device["type"] == "air_conditioner"
        type_terms = (
            r"\b(?:air\s+conditioner|a/?c)\b"
            if is_ac
            else r"\bfans?\b"
        )
        other_type_terms = (
            r"\bfans?\b"
            if is_ac
            else r"\b(?:air\s+conditioner|a/?c)\b"
        )
        if re.search(other_type_terms, message):
            return {}

        room_mentions = re.findall(r"\b(?:bedroom|living\s+room)\b", message)
        if room_mentions and device["room"].casefold() not in {
            room.replace("  ", " ") for room in room_mentions
        }:
            return {}
        if len(set(room_mentions)) > 1:
            return {}
        device_mentions = re.findall(type_terms, message)
        if not device_mentions and not room_mentions:
            return {}
        if len(device_mentions) > 1:
            return {}

        explicit: dict[str, Any] = {}
        if is_ac:
            temperature_patterns = (
                r"(?<![\w.])(\d{1,2}(?:\.\d+)?)\s*(?:°\s*c\b|degrees?(?:\s*(?:celsius|centigrade))?\b)",
                r"\b(?:temperature|temp)\s*(?:of|to|at|is|=)\s*(\d{1,2}(?:\.\d+)?)\b",
            )
            temperatures = {
                float(match.group(1))
                for pattern in temperature_patterns
                for match in re.finditer(pattern, user_text, re.IGNORECASE)
            }
            if len(temperatures) == 1:
                temperature = temperatures.pop()
                explicit["temperature"] = (
                    int(temperature) if temperature.is_integer() else temperature
                )

            modes = device["capabilities"]["modes"]
            mode_pattern = re.compile(
                r"\b(?:in|to|mode\s*(?:to|at|is|=)?)\s+("
                + "|".join(re.escape(mode) for mode in modes)
                + r")(?:\s+mode)?\b|\b("
                + "|".join(re.escape(mode) for mode in modes)
                + r")\s+mode\b",
                re.IGNORECASE,
            )
            mode_matches = {
                next(
                    mode
                    for mode in modes
                    if mode.casefold() == (match.group(1) or match.group(2)).casefold()
                )
                for match in mode_pattern.finditer(user_text)
            }
            if len(mode_matches) == 1:
                explicit["mode"] = mode_matches.pop()
        else:
            speed_matches = {
                int(match.group(1))
                for match in re.finditer(
                    r"\b(?:fan\s+)?speed\s*(?:to|at|of|=)?\s*(\d+)\b",
                    user_text,
                    re.IGNORECASE,
                )
            }
            if len(speed_matches) == 1:
                explicit["fan_speed"] = speed_matches.pop()

        return explicit

    def _apply_trigger_rules(
        self,
        user_text: str,
    ) -> dict[str, Any]:
        message = user_text.casefold()
        now_by_device: dict[str, dict[str, Any]] = {}
        matched_rules: list[str] = []
        for rule in self.store.list_rules(enabled_only=True):
            if (
                rule["kind"] != "phrase"
                or rule["trigger_phrase"].casefold() not in message
            ):
                continue
            matched_rules.append(rule["name"])
            for action in rule["actions"]:
                device = self._device(action["device_id"])
                state = now_by_device.setdefault(
                    device["device_id"], dict(device["state"])
                )
                state.update(action["state"])
                state.update(self._explicit_request_state(user_text, device))
                now_by_device[device["device_id"]] = self._validate_state(
                    device,
                    state,
                )

        if not now_by_device:
            return {
                "matched": False,
                "reason": "No enabled rule uses that trigger phrase.",
                "changes": [],
            }
        self.store.update_device_states(now_by_device)
        devices = {device["device_id"]: device for device in self.store.list_devices()}
        changes = [
            {
                "device_id": device_id,
                "name": devices[device_id]["name"],
                "state": state,
            }
            for device_id, state in now_by_device.items()
        ]
        return {
            "matched": True,
            "rules": matched_rules,
            "changes": changes,
        }

    def call(self, name: str, arguments: dict[str, Any], user_text: str) -> Any:
        if name == "list_devices":
            return {"devices": self.store.list_devices()}
        if name == "get_device_state":
            return {"device": self._device(arguments.get("device_id", ""))}
        if name == "control_device":
            return self._control_device(arguments, user_text)
        if name == "create_rule":
            return self._create_rule(arguments)
        if name == "list_rules":
            return {"rules": self.store.list_rules()}
        if name == "update_rule":
            return self._update_rule(arguments)
        if name == "set_rule_enabled":
            if not isinstance(arguments.get("enabled"), bool):
                raise ValueError("Rule enabled must be true or false.")
            rule = self.store.set_rule_enabled(
                arguments.get("rule_id", ""),
                arguments.get("enabled"),
            )
            return {"rule": rule}
        if name == "delete_rule":
            rule_id = arguments.get("rule_id", "")
            self.store.delete_rule(rule_id)
            return {"deleted": True, "rule_id": rule_id}
        if name == "apply_trigger_rules":
            return self._apply_trigger_rules(user_text)
        raise ValueError(f"Unknown tool: {name}")
