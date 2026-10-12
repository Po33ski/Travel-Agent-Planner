"""Middleware shared by the scripts that run the agents."""

import copy
import json

from agent_framework import AgentContext, AgentMiddleware, FunctionInvocationContext

# Contents of an agent's own tool loop: the function call and its raw result
TOOL_CONTENT_TYPES = {"function_call", "function_result"}
# Characters of an agent's reply shown by log_agent_replies
REPLY_PREVIEW_CHARS = 1500
# Characters of the content filter results shown by BlockedReplyMonitor
FILTER_DETAILS_CHARS = 2000


async def log_tool_calls(context: FunctionInvocationContext, call_next):
    """Function middleware: print every local tool call and the start of its result."""
    print(f"[TOOL] -> {context.function.name}({context.arguments})")
    await call_next()

    # The framework wraps the tool's return value in a list of Content objects
    result = context.result
    if isinstance(result, list):
        result = " ".join(str(getattr(item, "text", None) or item) for item in result)
    print(f"[TOOL] <- {context.function.name}: {str(result)[:200]}")


async def log_manager_decisions(context: AgentContext, call_next):
    """Agent middleware for the Magentic manager: print the decision of every progress check.

    At the start of each round the orchestrator asks the manager for a progress ledger (JSON):
    is the request satisfied, is the team making progress, which agent goes next and with what
    instruction. The manager's other replies (facts, plan, final answer) are skipped.
    """
    await call_next()

    ledger = _parse_progress_ledger(getattr(context.result, "text", None) or "")
    if ledger is None:
        return

    def field(name, key):
        item = ledger.get(name)
        return item.get(key) if isinstance(item, dict) else None

    if field("is_request_satisfied", "answer"):
        print(f"[MANAGER] Request satisfied: {field('is_request_satisfied', 'reason')}")
        return

    print(
        f"[MANAGER] Next agent: {field('next_speaker', 'answer')} "
        f"(progress: {field('is_progress_being_made', 'answer')}, loop: {field('is_in_loop', 'answer')})"
    )
    print(f"[MANAGER]   Why: {field('next_speaker', 'reason')}")
    print(f"[MANAGER]   Instruction: {field('instruction_or_question', 'answer')}")


async def log_agent_replies(context: AgentContext, call_next):
    """Agent middleware for the specialist agents: print what the agent sent back to the manager.

    The reply is what the manager bases its next decision on. A reasoning summary is printed
    too when the model returned one.
    """
    await call_next()

    agent_name = context.agent.name
    for message in getattr(context.result, "messages", None) or []:
        for content in message.contents:
            if content.type == "text_reasoning" and content.text:
                print(f"[AGENT] {agent_name} reasoning: {content.text[:REPLY_PREVIEW_CHARS]}")

    reply = getattr(context.result, "text", None) or "(empty reply)"
    shortened = " ..." if len(reply) > REPLY_PREVIEW_CHARS else ""
    print(f"[AGENT] {agent_name} replied: {reply[:REPLY_PREVIEW_CHARS]}{shortened}")


def _parse_progress_ledger(text):
    """Return the progress ledger found in the manager's reply, or None when the reply is not one."""
    start = text.find("{")
    if start == -1:
        return None
    try:
        # raw_decode reads the first JSON object and ignores anything after it
        ledger, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    if isinstance(ledger, dict) and "next_speaker" in ledger:
        return ledger
    return None


class BlockedReplyMonitor(AgentMiddleware):
    """
    Agent middleware that detects replies the model refused or the content filter cut off.
    Such a reply arrives without an error: the text ends early and a refusal such as
    "I'm sorry, but I cannot assist with that request." takes its place. Add one shared
    instance to every agent. The details are printed as [GUARDRAIL] lines.
    """

    def __init__(self):
        # Agent name -> whether its latest reply was refused or blocked
        self._last_reply_blocked = {}

    def reset(self):
        """Forget the recorded replies, e.g. before the next run."""
        self._last_reply_blocked.clear()

    def is_last_reply_blocked(self, agent_name):
        """Return True when the latest reply of this agent was refused or blocked."""
        return self._last_reply_blocked.get(agent_name, False)

    async def process(self, context, call_next):
        await call_next()

        agent_name = context.agent.name
        details = _describe_blocked_reply(context.result)
        self._last_reply_blocked[agent_name] = details is not None
        if details is None:
            return

        print(f"[GUARDRAIL] {agent_name}: the reply was refused by the model or cut off by the content filter.")
        for label, value in details.items():
            print(f"[GUARDRAIL]   {label}: {value}")


def _describe_blocked_reply(response):
    """Return what is known about a refused or filtered reply, or None for a normal reply."""
    # The framework turns a "refusal" part of the reply into text content with this marker
    refusals = [
        content.text
        for message in getattr(response, "messages", None) or []
        for content in message.contents
        if content.type == "text" and content.additional_properties.get("model_output_kind") == "refusal"
    ]
    finish_reason = str(getattr(response, "finish_reason", None) or "")
    if not refusals and finish_reason != "content_filter":
        return None

    details = {}
    if refusals:
        details["Refusal"] = " ".join(refusals)
    if finish_reason:
        details["Finish reason"] = finish_reason
    try:
        details.update(_service_details(response))
    except Exception as error:
        # Diagnostics must never break a run
        details["Service details"] = f"not available ({error})"
    return details


def _service_details(response):
    """Collect what the service reported: status, incomplete reason and content filter results."""
    # AgentResponse -> ChatResponse -> the provider's response object
    raw = response
    while getattr(raw, "raw_representation", None) is not None:
        raw = raw.raw_representation
    if isinstance(raw, list) and raw:
        raw = raw[-1]

    details = {}
    if getattr(raw, "status", None):
        details["Status"] = raw.status
    if getattr(raw, "incomplete_details", None) is not None:
        details["Incomplete details"] = raw.incomplete_details

    # Content filter results have no fixed place in the response, so every field named *filter* is collected
    if callable(getattr(raw, "model_dump", None)):
        filter_fields = {}
        _collect_filter_fields(raw.model_dump(), "", filter_fields)
        details["Content filter results"] = (
            json.dumps(filter_fields, default=str)[:FILTER_DETAILS_CHARS] if filter_fields else "none reported"
        )
    return details


def _collect_filter_fields(node, path, found):
    """Walk a response dictionary and collect the non-empty fields whose name contains 'filter'."""
    if isinstance(node, dict):
        for key, value in node.items():
            if "filter" in str(key).lower():
                if value:
                    found[f"{path}{key}"] = value
            else:
                _collect_filter_fields(value, f"{path}{key}.", found)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _collect_filter_fields(item, f"{path}{index}.", found)


async def drop_foreign_tool_contents(context: AgentContext, call_next):
    """Agent middleware: remove other agents' function calls and results from the incoming messages.

    A group chat workflow shares every agent's reply with the other agents, including the
    function call and its raw result. An agent that continues its server-side conversation
    sends such a result without the matching call, which the service rejects with
    "No tool call found for function call output". The agents only need each other's text
    answers, so the tool contents are dropped. The agent's own tool loop runs below this
    middleware and is not affected.
    """
    cleaned_messages = []
    for message in context.messages:
        contents = [content for content in message.contents if content.type not in TOOL_CONTENT_TYPES]
        if len(contents) == len(message.contents):
            cleaned_messages.append(message)
        elif contents:
            # Copy: the same Message object is shared with the other agents and the workflow events
            message = copy.copy(message)
            message.contents = contents
            cleaned_messages.append(message)

    context.messages[:] = cleaned_messages
    await call_next()
