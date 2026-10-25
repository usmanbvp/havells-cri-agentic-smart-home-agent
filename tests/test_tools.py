from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pytest

from smart_home_agent.storage import SQLiteStore
from smart_home_agent.tools import SmartHomeTools


@pytest.fixture
def home():
    with TemporaryDirectory() as directory:
        store = SQLiteStore(Path(directory) / "home.sqlite3")
        store.initialize()
        yield store, SmartHomeTools(store, ZoneInfo("Asia/Kolkata"))


def create_default(tools: SmartHomeTools) -> str:
    result = tools.call(
        "create_rule",
        {
            "name": "Bedroom comfort",
            "kind": "default",
            "actions": [
                {
                    "device_id": "ac_bedroom",
                    "state": {"temperature": 24, "mode": "Eco"},
                }
            ],
        },
        "",
    )
    return result["rule"]["rule_id"]


def test_seeded_fleet_has_two_acs_and_two_fans_across_two_rooms(home):
    store, _ = home
    devices = store.list_devices()

    assert len(devices) == 4
    assert sum(device["type"] == "air_conditioner" for device in devices) == 2
    assert sum(device["type"] == "fan" for device in devices) == 2
    assert {device["room"] for device in devices} == {"Bedroom", "Living Room"}


def test_control_applies_default_but_explicit_current_request_wins(home):
    store, tools = home
    create_default(tools)

    defaulted = tools.call(
        "control_device",
        {"device_id": "ac_bedroom", "state": {"power": True}},
        "Turn on the bedroom AC.",
    )
    assert defaulted["device"]["state"] == {
        "power": True,
        "temperature": 24,
        "mode": "Eco",
    }
    assert defaulted["applied_rules"] == ["Bedroom comfort"]

    overridden = tools.call(
        "control_device",
        {
            "device_id": "ac_bedroom",
            "state": {"power": True, "temperature": 24, "mode": "Eco"},
        },
        "Turn on the bedroom AC at 22°C.",
    )
    assert overridden["device"]["state"] == {
        "power": True,
        "temperature": 22,
        "mode": "Eco",
    }
    assert overridden["applied_rules"] == ["Bedroom comfort"]

    arbitrary_override = tools.call(
        "control_device",
        {
            "device_id": "ac_bedroom",
            "state": {"power": True, "temperature": 24, "mode": "Eco"},
        },
        "Keep the bedroom AC at 19.5 degrees Celsius in Cool mode.",
    )
    assert arbitrary_override["device"]["state"] == {
        "power": True,
        "temperature": 19.5,
        "mode": "Cool",
    }


def test_explicit_request_can_override_every_saved_default(home):
    _, tools = home
    create_default(tools)

    result = tools.call(
        "control_device",
        {
            "device_id": "ac_bedroom",
            "state": {"power": True, "temperature": 22, "mode": "Cool"},
        },
        "Turn on the bedroom AC at 22 degrees in Cool mode.",
    )

    assert result["device"]["state"]["temperature"] == 22
    assert result["device"]["state"]["mode"] == "Cool"
    assert result["applied_rules"] == []


def test_phrase_rule_requires_exact_saved_phrase_in_current_message(home):
    store, tools = home
    rule = tools.call(
        "create_rule",
        {
            "name": "Good night",
            "kind": "phrase",
            "trigger_phrase": "good night",
            "actions": [{"device_id": "fan_living", "state": {"power": False}}],
        },
        "",
    )["rule"]
    store.update_device_states({"fan_living": {"power": True, "fan_speed": 3}})

    not_matched = tools.call(
        "apply_trigger_rules",
        {},
        "What routine did I save?",
    )
    assert not_matched["matched"] is False
    assert store.get_device("fan_living")["state"]["power"] is True

    matched = tools.call(
        "apply_trigger_rules",
        {},
        "Good night.",
    )
    assert matched["matched"] is True
    assert matched["rules"] == [rule["name"]]
    assert store.get_device("fan_living")["state"]["power"] is False


def test_phrase_rule_preserves_explicit_temperature_override(home):
    store, tools = home
    tools.call(
        "create_rule",
        {
            "name": "AC on rule",
            "kind": "phrase",
            "trigger_phrase": "AC on",
            "actions": [
                {
                    "device_id": "ac_bedroom",
                    "state": {"power": True, "temperature": 24, "mode": "Eco"},
                }
            ],
        },
        "",
    )

    result = tools.call(
        "apply_trigger_rules",
        {},
        "AC on at 22°C.",
    )

    expected_state = {"power": True, "temperature": 22, "mode": "Eco"}
    assert result["matched"] is True
    assert result["changes"][0]["state"] == expected_state
    assert store.get_device("ac_bedroom")["state"] == expected_state


def test_multi_device_phrase_rule_applies_every_saved_state_field(home):
    store, tools = home
    store.update_device_states(
        {
            "fan_living": {"power": True, "fan_speed": 3},
            "ac_living": {"power": False, "temperature": 26, "mode": "Cool"},
        }
    )
    tools.call(
        "create_rule",
        {
            "name": "Evening comfort",
            "kind": "phrase",
            "trigger_phrase": "evening comfort",
            "actions": [
                {"device_id": "fan_living", "state": {"power": False}},
                {
                    "device_id": "ac_living",
                    "state": {"power": True, "temperature": 23, "mode": "Sleep"},
                },
            ],
        },
        "",
    )

    result = tools.call("apply_trigger_rules", {}, "Evening comfort.")

    expected_ac = {"power": True, "temperature": 23, "mode": "Sleep"}
    expected_fan = {"power": False, "fan_speed": 3}
    assert result["matched"] is True
    assert store.get_device("ac_living")["state"] == expected_ac
    assert store.get_device("fan_living")["state"] == expected_fan
    changes = {change["device_id"]: change["state"] for change in result["changes"]}
    assert changes == {"fan_living": expected_fan, "ac_living": expected_ac}


def test_time_rule_applies_at_threshold_but_not_before_or_after_midnight(home):
    store, tools = home
    tools.call(
        "create_rule",
        {
            "name": "Night fan speed",
            "kind": "time",
            "after_time": "22:00",
            "actions": [{"device_id": "fan_living", "state": {"fan_speed": 2}}],
        },
        "",
    )

    timezone = ZoneInfo("Asia/Kolkata")
    tools.clock = lambda: datetime(2025, 10, 24, 21, 59, tzinfo=timezone)
    too_early = tools.call(
        "control_device",
        {"device_id": "fan_living", "state": {"power": True}},
        "Turn on the living room fan.",
    )
    assert too_early["device"]["state"]["fan_speed"] == 1
    assert too_early["applied_rules"] == []

    tools.clock = lambda: datetime(2025, 10, 24, 22, 0, tzinfo=timezone)
    result = tools.call(
        "control_device",
        {"device_id": "fan_living", "state": {"power": True, "fan_speed": 3}},
        "Turn on the living room fan.",
    )
    assert result["device"]["state"] == {"power": True, "fan_speed": 3}
    assert result["applied_rules"] == []

    tools.clock = lambda: datetime(2025, 10, 24, 22, 0, tzinfo=timezone)
    applied = tools.call(
        "control_device",
        {"device_id": "fan_living", "state": {"power": True}},
        "Turn on the living room fan.",
    )
    assert applied["device"]["state"] == {"power": True, "fan_speed": 2}
    assert applied["applied_rules"] == ["Night fan speed"]

    tools.call(
        "control_device",
        {"device_id": "fan_living", "state": {"power": False, "fan_speed": 1}},
        "Switch the fan off.",
    )
    tools.clock = lambda: datetime(2025, 10, 25, 0, 15, tzinfo=timezone)
    after_midnight = tools.call(
        "control_device",
        {"device_id": "fan_living", "state": {"power": True}},
        "Turn on the living room fan.",
    )
    assert after_midnight["device"]["state"]["fan_speed"] == 1
    assert after_midnight["applied_rules"] == []


def test_invalid_state_is_rejected_without_changing_device(home):
    store, tools = home
    original = store.get_device("ac_bedroom")["state"]

    with pytest.raises(ValueError, match="between 16 and 30"):
        tools.call(
            "control_device",
            {
                "device_id": "ac_bedroom",
                "state": {"power": True, "temperature": 40},
            },
            "Turn the bedroom AC on at 40 degrees.",
        )

    assert store.get_device("ac_bedroom")["state"] == original


def test_rule_actions_are_validated_before_saving(home):
    store, tools = home

    with pytest.raises(ValueError, match="Only air conditioners"):
        tools.call(
            "create_rule",
            {
                "name": "Bad fan preference",
                "kind": "default",
                "actions": [{"device_id": "fan_living", "state": {"temperature": 20}}],
            },
            "",
        )

    assert store.list_rules() == []


def test_time_rule_rejects_actions_outside_its_supported_fan_scope(home):
    store, tools = home

    with pytest.raises(ValueError, match="support fans only"):
        tools.call(
            "create_rule",
            {
                "name": "Late AC",
                "kind": "time",
                "after_time": "22:00",
                "actions": [{"device_id": "ac_bedroom", "state": {"temperature": 24}}],
            },
            "",
        )

    with pytest.raises(ValueError, match="do not change power"):
        tools.call(
            "create_rule",
            {
                "name": "Turn fan off late",
                "kind": "time",
                "after_time": "22:00",
                "actions": [{"device_id": "fan_living", "state": {"power": False}}],
            },
            "",
        )

    assert store.list_rules() == []


def test_rules_survive_store_reinitialization_and_can_be_managed(home):
    store, tools = home
    rule_id = create_default(tools)
    database_path = store.database_path

    reopened = SQLiteStore(database_path)
    reopened.initialize()
    assert reopened.get_rule(rule_id)["name"] == "Bedroom comfort"

    updated = tools.call(
        "update_rule",
        {"rule_id": rule_id, "name": "Sleep comfort"},
        "",
    )["rule"]
    assert updated["name"] == "Sleep comfort"

    disabled = tools.call(
        "set_rule_enabled",
        {"rule_id": rule_id, "enabled": False},
        "",
    )["rule"]
    assert disabled["enabled"] is False
    assert store.list_rules(enabled_only=True) == []

    tools.call("delete_rule", {"rule_id": rule_id}, "")
    assert store.list_rules() == []
