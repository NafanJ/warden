"""OpenAI function-calling agent loop.

warden drives the loop itself: the model proposes tool calls, each one passes
through tiers.decide_tool (the same safety gate the Claude path uses) before
warden executes it, and results are fed back until the model writes its report
or the turn/budget limit is hit. No SDK, no implicit tool execution.
"""
from __future__ import annotations

import json
from typing import Any

from warden.agent import openai_tools
from warden.agent.tiers import decide_tool, tier_of
from warden.backends import Backend
from warden.config import Config
from warden.notifier import Channel
from warden.store import Store

MAX_TURNS = 40

# Approx USD per 1M tokens (input, output) for budget tracking. Unknown models
# fall back to no cost estimate.
_PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o4-mini": (1.10, 4.40),
    "gpt-4o": (2.50, 10.00),
}


def _usage_cost(usage: Any, model: str) -> float:
    price = _PRICES.get(model)
    if not price or usage is None:
        return 0.0
    pin, pout = price
    return (getattr(usage, "prompt_tokens", 0) * pin
            + getattr(usage, "completion_tokens", 0) * pout) / 1_000_000


def run_openai_agent(prompt_text: str, system_prompt: str, config: Config,
                     backend: Backend, store: Store, channel: Channel,
                     incident_id: int | None, run_result: dict[str, Any],
                     client: Any = None) -> tuple[str, float | None]:
    """Returns (final_text, cost_usd). `client` is injectable for tests."""
    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=config.openai_api_key)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt_text},
    ]
    cost = 0.0
    final_text = ""
    seen: dict[str, int] = {}  # loop guard: count identical (tool, args) calls

    for _ in range(MAX_TURNS):
        resp = client.chat.completions.create(
            model=config.openai_model,
            messages=messages,
            tools=openai_tools.TOOL_SCHEMAS,
            tool_choice="auto",
            temperature=0,
        )
        cost += _usage_cost(getattr(resp, "usage", None), config.openai_model)
        message = resp.choices[0].message
        messages.append(message)  # echo the assistant turn (carries tool_call ids)

        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            final_text = message.content or ""
            break

        for call in tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            # Loop guard: weaker models can thrash, re-issuing the same call.
            # A mutation (Tier 1/2) should never repeat with identical args; a
            # read may legitimately be re-checked once, so allow it twice.
            key = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
            seen[key] = seen.get(key, 0) + 1
            tier = tier_of(name, args)
            limit = 1 if (tier is not None and tier >= 1) else 2
            if seen[key] > limit:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": (
                    f"Loop guard: you have already called `{name}` with these exact "
                    "arguments; repeating it changes nothing. Take any remaining single "
                    "corrective action, then call write_report now to finish.")})
                continue

            allowed, deny_msg = decide_tool(config, store, channel, incident_id, name, args, backend)
            if allowed:
                content = openai_tools.execute_tool(
                    name, args, backend=backend, config=config, store=store,
                    incident_id=incident_id, run_result=run_result)
            else:
                content = deny_msg
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

        if "report_path" in run_result:
            break  # report written — investigation is complete
        if config.max_budget_usd and cost >= config.max_budget_usd:
            messages.append({"role": "user",
                             "content": "Budget reached. Call write_report now with what you have."})

    return final_text, round(cost, 6)
