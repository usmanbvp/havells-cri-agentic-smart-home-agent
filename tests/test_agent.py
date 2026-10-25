from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from smart_home_agent.agent import SmartHomeAgent
from smart_home_agent.storage import SQLiteStore
from smart_home_agent.tools import SmartHomeTools


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append({"messages": messages, "tools": tools})
        return self.responses.pop(0)

    @contextmanager
    def turn(self) -> Generator[None, None, None]:
        yield


def test_agent_executes_model_selected_tool_and_returns_grounded_reply():
    with TemporaryDirectory() as directory:
        store = SQLiteStore(Path(directory) / "home.sqlite3")
        store.initialize()
        function_call = {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "control_device",
                "arguments": json.dumps(
                    {
                        "device_id": "fan_living",
                        "state": {"power": True, "fan_speed": 2},
                    }
                ),
            },
        }
        llm = FakeLLM(
            [
                {"content": None, "tool_calls": [function_call]},
                {"content": "The living room fan is on at speed 2."},
            ]
        )
        agent = SmartHomeAgent(
            llm,
            SmartHomeTools(store, ZoneInfo("Asia/Kolkata")),
        )

        response = agent.respond(
            user_text="Turn on the living room fan at speed 2.",
            history=[{"role": "user", "content": "Hello"}],
        )

        assert "overrides the saved preference" in llm.calls[0]["messages"][0]["content"]
        system_prompt = llm.calls[0]["messages"][0]["content"]
        assert "include every state change the user requested" in system_prompt
        assert '"power": true or false' in system_prompt
        assert "with update_rule before confirming" in system_prompt
        assert response["reply"] == "The living room fan is on at speed 2."
        assert response["actions"][0]["tool"] == "control_device"
        assert response["actions"][0]["result"]["device"]["state"] == {
            "power": True,
            "fan_speed": 2,
        }
        assert store.get_device("fan_living")["state"]["fan_speed"] == 2
        assert any(
            tool["function"]["name"] == "control_device"
            for tool in llm.calls[0]["tools"]
        )
        assert llm.calls[1]["messages"][-1]["role"] == "tool"


def test_agent_returns_tool_validation_error_to_model_for_recovery():
    with TemporaryDirectory() as directory:
        store = SQLiteStore(Path(directory) / "home.sqlite3")
        store.initialize()
        function_call = {
            "id": "call_2",
            "type": "function",
            "function": {
                "name": "control_device",
                "arguments": json.dumps(
                    {
                        "device_id": "ac_bedroom",
                        "state": {"power": True, "temperature": 40},
                    }
                ),
            },
        }
        llm = FakeLLM(
            [
                {"content": None, "tool_calls": [function_call]},
                {"content": "That AC supports temperatures from 16°C to 30°C."},
            ]
        )
        agent = SmartHomeAgent(
            llm,
            SmartHomeTools(store, ZoneInfo("UTC")),
        )

        response = agent.respond(user_text="Set it to 40 degrees.")

        tool_result = json.loads(llm.calls[1]["messages"][-1]["content"])
        assert "between 16 and 30" in tool_result["error"]
        assert "supports temperatures" in response["reply"]
        assert store.get_device("ac_bedroom")["state"]["power"] is False


def test_agent_honors_explicit_value_when_model_echoes_saved_default():
    with TemporaryDirectory() as directory:
        store = SQLiteStore(Path(directory) / "home.sqlite3")
        store.initialize()
        tools = SmartHomeTools(store, ZoneInfo("Asia/Kolkata"))
        tools.call(
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
        function_call = {
            "id": "call_override",
            "type": "function",
            "function": {
                "name": "control_device",
                "arguments": json.dumps(
                    {
                        "device_id": "ac_bedroom",
                        "state": {"power": True, "temperature": 24, "mode": "Eco"},
                    }
                ),
            },
        }
        llm = FakeLLM(
            [
                {"content": None, "tool_calls": [function_call]},
                {
                    "content": (
                        "The bedroom AC is on at 22°C in Eco mode. "
                        "Your explicit temperature overrides the saved default."
                    )
                },
            ]
        )
        agent = SmartHomeAgent(llm, tools)

        response = agent.respond(
            user_text="Turn on the bedroom AC at 22°C.",
        )

        result = response["actions"][0]["result"]
        expected_state = {"power": True, "temperature": 22, "mode": "Eco"}
        assert result["device"]["state"] == expected_state
        assert result["applied_rules"] == ["Bedroom comfort"]
        assert store.get_device("ac_bedroom")["state"] == expected_state
