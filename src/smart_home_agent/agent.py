"""LLM orchestration and the bounded, observable function-calling loop."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import Any, Protocol

from .llm import LLMError
from .tools import TOOLS, SmartHomeTools


SYSTEM_PROMPT = """You are a careful, friendly smart-home assistant for a mock home.
The only appliances are the devices returned by tools. They are simulated; never
claim a real appliance or cloud integration was used.

Use function tools whenever a question needs current device/rule information or a
device change. Do not guess device IDs, capabilities, statuses, or tool results.
Never imitate a tool call in prose and never claim a change succeeded unless its
tool returned success. Ask one concise clarifying question instead of inventing a
room, appliance, temperature, or rule condition when the request is ambiguous.

For a device command, choose the relevant device tool and include only the fields
the user explicitly requested. The server applies stored defaults and time rules;
an explicit value in the current command always wins. Mention which saved rule
affected a change when the tool reports one.

Personalization rules:
- Create a rule only when the user clearly asks you to remember or automatically
  repeat behavior. Use create_rule with a structured action for every named device.
- For every rule action, include every state change the user requested for that
  device. In a phrase routine, if the user says to turn a device on or off, include
  "power": true or false alongside any requested temperature, mode, or fan speed.
  Do not omit a requested power change just because the routine also sets other
  properties. After creating or updating a rule, compare the saved actions returned
  by the tool with the user's request. Correct any omitted or incorrect requested
  fields with update_rule before confirming; if the intended action is ambiguous,
  ask one concise question instead of claiming the routine is complete.
- A default rule supplies preferences when that device is switched on; it does not
  switch the device on by itself.
- A phrase rule runs only when the user later uses the exact saved phrase as a
  command. Call apply_trigger_rules with no arguments before responding to that
  command. The server matches the saved phrase against this message. Do not run a
  phrase rule for a question merely discussing the phrase.
- A time rule applies to a fan power-on at or after its saved local time on the
  same calendar day. The configured local timezone is {timezone}.
- Use list_rules before discussing saved rules. For changes or deletion, identify
  the intended rule from that result; ask if multiple rules could fit.
- Confirm a new or changed rule in plain language, including its trigger/scope and
  actions. When creating a default rule, explain that an explicit value in a
  later command overrides the saved preference. For delete/disable operations,
  state what changed.

Keep replies concise, warm, and grounded in tool results. If a tool reports an
error, explain the actual limitation and do not imply success."""


class LLMClient(Protocol):
    def turn(self) -> AbstractContextManager[None]: ...

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


class SmartHomeAgent:
    def __init__(
        self,
        llm: LLMClient,
        tools: SmartHomeTools,
        *,
        max_tool_rounds: int = 6,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_tool_rounds = max_tool_rounds
        self.system_prompt = SYSTEM_PROMPT.format(timezone=tools.timezone.key)

    @staticmethod
    def _tool_result(
        tool_call: dict[str, Any],
        user_text: str,
        tools: SmartHomeTools,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        function = tool_call.get("function")
        call_id = tool_call.get("id")
        if not isinstance(call_id, str) or not isinstance(function, dict):
            raise LLMError("The model returned an incomplete function call.")
        name = function.get("name")
        if not isinstance(name, str):
            raise LLMError("The model returned a function call without a name.")
        raw_arguments = function.get("arguments", "{}")
        try:
            arguments = (
                json.loads(raw_arguments)
                if isinstance(raw_arguments, str)
                else raw_arguments
            )
            if not isinstance(arguments, dict):
                raise ValueError("Function arguments must be a JSON object.")
            result = tools.call(name, arguments, user_text)
        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as error:
            result = {"error": str(error)}
        tool_message = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False),
        }
        trace = {"tool": name, "result": result}
        return tool_message, trace

    def respond(
        self,
        *,
        user_text: str,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        with self.llm.turn():
            return self._respond(user_text=user_text, history=history)

    def _respond(
        self,
        *,
        user_text: str,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        if history:
            messages.extend(history[-12:])
        messages.append({"role": "user", "content": user_text})
        traces: list[dict[str, Any]] = []

        for _ in range(self.max_tool_rounds):
            assistant = self.llm.complete(messages, TOOLS)
            calls = assistant.get("tool_calls") or []
            if not isinstance(calls, list):
                raise LLMError("The model returned an invalid function-call list.")
            if not calls:
                reply = assistant.get("content")
                if not isinstance(reply, str) or not reply.strip():
                    raise LLMError("The model returned neither a reply nor a function call.")
                return {"reply": reply.strip(), "actions": traces}

            assistant_turn = {
                "role": "assistant",
                "content": assistant.get("content"),
                "tool_calls": calls,
            }
            messages.append(assistant_turn)
            for tool_call in calls:
                if not isinstance(tool_call, dict):
                    raise LLMError("The model returned an invalid function call.")
                tool_message, trace = self._tool_result(
                    tool_call,
                    user_text,
                    self.tools,
                )
                messages.append(tool_message)
                traces.append(trace)

        raise LLMError(
            f"The model exceeded the {self.max_tool_rounds}-round function-call limit."
        )
