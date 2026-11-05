import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import yaml
from agent_framework import (
    AgentExecutor,
    AgentExecutorResponse,
    AgentMiddleware,
    AgentResponse,
    ConversationSplit,
    EvalItem,
    FunctionTool,
    Message,
    add_usage_details,
    evaluate_agent,
    evaluate_workflow,
    normalize_tools,
)
from agent_framework.foundry import FoundryEvals

from classes.scope_guard_services import SCOPE_GUARD_NAME
from utils.agent_middleware import on_final_response
from utils.utils import add_todays_date

# Token counts reported in usage_details of an agent response. The input count includes
# the cached input tokens and the output count includes the reasoning tokens.
TOKEN_FIELDS = (
    "input_token_count",
    "cache_read_input_token_count",
    "output_token_count",
    "reasoning_output_token_count",
    "total_token_count",
)


# Contents of an agent response that are tool calls, and the attribute that holds the tool name.
# Function tools run locally; MCP and search tools run server-side and are reported by the service.
TOOL_CALL_NAME_ATTRIBUTES = {
    "function_call": "name",
    "mcp_server_tool_call": "tool_name",
    "search_tool_call": "tool_name",
}

# Contents of an agent response that hold what a tool returned, and the attribute that holds it.
# Web search results are not included: they carry links, not the text of the pages.
TOOL_OUTPUT_ATTRIBUTES = {
    "function_result": "result",
    "mcp_server_tool_result": "output",
}

# Characters of the judge's reason printed for a failed evaluator
JUDGE_REASON_CHARS = 300

# Typographic quotes and apostrophes replaced by plain ones before an exact answer is compared
PLAIN_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})

# Context of an agent call whose tools returned nothing. groundedness needs a context in every item.
NO_TOOL_OUTPUT = "The agent's tools returned no content."


class TokenUsageMiddleware(AgentMiddleware):
    """
    Agent middleware that records the token usage of every agent call.
    Add one shared instance to the middleware list of every evaluated agent: the single agent,
    or every agent of a workflow, the manager included. Usage is read from usage_details of
    the agent response. In a streaming run (a concierge subagent: as_tool() always streams)
    the response exists only after the stream has been read, so it is read through a result hook.
    """

    def __init__(self):
        self.usage_by_agent = {}
        self.calls_by_agent = {}

    def reset(self):
        """Forget the recorded usage, e.g. before the next run."""
        self.usage_by_agent.clear()
        self.calls_by_agent.clear()

    async def process(self, context, call_next):
        await call_next()

        agent_name = context.agent.name or "unknown"
        self.calls_by_agent[agent_name] = self.calls_by_agent.get(agent_name, 0) + 1
        # The agent counts as called even when its stream never finishes
        self.usage_by_agent.setdefault(agent_name, {})
        on_final_response(context, lambda response: self._add_usage(agent_name, response))

    def _add_usage(self, agent_name, response):
        usage = getattr(response, "usage_details", None)
        self.usage_by_agent[agent_name] = add_usage_details(self.usage_by_agent.get(agent_name), usage)


class ConciergeCallRecorder(AgentMiddleware):
    """
    Agent middleware for the concierge's subagents in the evaluation: records every subagent call
    (the agent, the task it received and its final response), so each call can be scored like a
    specialist turn of the Magentic workflow.

    A subagent runs inside a tool call of the concierge (agent as a tool), so it leaves no workflow
    events to read afterwards. as_tool() streams it, so the response is read through a result hook.
    Put it after add_todays_date_to_task, so the recorded task is the one the subagent saw.
    """

    def __init__(self):
        # (agent, task messages, final response) in the order the calls finished
        self.calls = []

    def reset(self):
        """Forget the recorded calls, e.g. before the next test case."""
        self.calls.clear()

    async def process(self, context, call_next):
        task_messages = list(context.messages)
        await call_next()

        agent = context.agent
        on_final_response(context, lambda response: self.calls.append((agent, task_messages, response)))


class EvaluateService:
    """
    Evaluation of a single agent, a multi-agent workflow or the concierge with its subagents (the
    *_concierge_* methods): answer quality, rule checks and token usage.
    Quality is scored by the Microsoft Foundry built-in evaluators (an LLM judge running in the
    Foundry project), rule checks run locally without model calls and token usage is collected
    by usage_middleware. Both kinds of runs produce the same report, so a single agent and
    different workflows can be compared.
    """

    def __init__(self, judge_client, config, max_answer_words, scope_guard=None):
        """
        Args:
            judge_client: FoundryChatClient whose model deployment is used as the LLM judge.
            config: content of config.yaml; the "evaluation" section is used.
            max_answer_words: word limit of the final answer, as set in the system prompt
                of the evaluated agent or workflow manager.
            scope_guard: ScopeGuard that checks every test prompt before the run, as in the chat.
                None runs every prompt without a check.
        """
        settings = config["evaluation"]
        self.judge_client = judge_client
        self.scope_guard = scope_guard
        self.max_answer_words = max_answer_words
        self.test_cases_path = settings["test_cases_path"]
        self.concierge_test_cases_path = settings.get("concierge_test_cases_path")
        self.output_dir = settings["output_dir"]
        self.final_answer_evaluators = settings["final_answer_evaluators"]
        # The concierge's conversation: {"per_turn": [...], "whole_conversation": [...]}
        self.concierge_answer_evaluators = settings.get("concierge_answer_evaluators") or {}
        self.agent_evaluators = settings["agent_evaluators"]
        self.agent_evaluation_enabled = settings["evaluate_agents"]
        self.timeout_seconds = settings["timeout_seconds"]
        # Add this instance to the middleware of every evaluated agent
        self.usage_middleware = TokenUsageMiddleware()

    def load_test_cases(self):
        """Read the test prompts (see inputs/evaluation_prompts.yaml for the format)."""
        with open(self.test_cases_path, "r", encoding="utf-8") as file:
            return yaml.safe_load(file)["test_cases"]

    # ------------------------------------------------------------------ token usage
    def count_workflow_tokens(self):
        """Sum the token usage of all agents recorded since the last reset.

        Returns {"total": {...}, "per_agent": {agent_name: {...}}}. Every entry holds the
        token counts (TOKEN_FIELDS) and the number of agent calls. A single agent gives
        one entry in "per_agent".
        """
        per_agent = {}
        total_usage = None
        for agent_name, usage in self.usage_middleware.usage_by_agent.items():
            calls = self.usage_middleware.calls_by_agent[agent_name]
            per_agent[agent_name] = self._usage_to_dict(usage, calls)
            total_usage = add_usage_details(total_usage, usage)

        total_calls = sum(self.usage_middleware.calls_by_agent.values())
        return {
            "total": self._usage_to_dict(total_usage or {}, total_calls),
            "per_agent": per_agent,
        }

    # ------------------------------------------------------------------ runs
    async def run_workflow(self, workflow, query):
        """Run the workflow once and return its result, final answer, duration and token usage."""
        self.usage_middleware.reset()
        started = time.perf_counter()
        workflow_result = await workflow.run(query)
        duration_seconds = time.perf_counter() - started

        return {
            "workflow_result": workflow_result,
            "final_answer": self._final_answer_text(workflow_result),
            "duration_seconds": round(duration_seconds, 1),
            "token_usage": self.count_workflow_tokens(),
        }

    async def run_agent(self, agent, query):
        """Run the single agent once and return its response, final answer, called tools, duration and token usage."""
        self.usage_middleware.reset()
        started = time.perf_counter()
        # No session: every test case is an independent conversation
        response = await agent.run(query)
        duration_seconds = time.perf_counter() - started

        return {
            "response": response,
            "final_answer": response.text,
            "called_tools": self.count_tool_calls(response),
            "duration_seconds": round(duration_seconds, 1),
            "token_usage": self.count_workflow_tokens(),
        }

    @staticmethod
    def count_tool_calls(response):
        """Return {tool name: number of calls} for the tool calls found in an agent response.

        This is the single agent's counterpart of the agents called in a workflow: it shows
        which tools the agent decided to use. Server-side tools appear only when the service
        reports their calls in the response.
        """
        calls = {}
        for message in response.messages:
            for content in message.contents:
                name_attribute = TOOL_CALL_NAME_ATTRIBUTES.get(content.type)
                if name_attribute:
                    tool_name = getattr(content, name_attribute, None) or "unknown"
                    calls[tool_name] = calls.get(tool_name, 0) + 1
        return calls

    # ------------------------------------------------------------------ workflow evaluation
    async def evaluate_final_answer(self, workflow, workflow_result, eval_name):
        """Score the final answer of the workflow with the Foundry evaluators (LLM judge)."""
        results = await evaluate_workflow(
            workflow=workflow,
            workflow_result=workflow_result,
            evaluators=self._foundry_evals(self.final_answer_evaluators),
            eval_name=eval_name,
            include_per_agent=False,
        )
        return self._results_to_dict(results[0])

    async def evaluate_agents(self, workflow, workflow_result, eval_name):
        """Score every specialist agent of the workflow separately: the steps that led to the final answer.

        Each agent is scored with its own evaluators from agent_evaluators; every call of the
        agent is one item. evaluate_workflow applies one set of evaluators to all agents and
        does not pass a context, so the items are built here from the workflow events.
        The result holds one entry per agent under "agents".
        """
        items_by_agent = {}
        for event in workflow_result:
            if event.type != "executor_completed":
                continue
            # The manager is not a workflow executor; the specialists run in agent executors
            executor = workflow.executors.get(event.executor_id)
            if not isinstance(executor, AgentExecutor):
                continue

            responses = event.data if isinstance(event.data, list) else [event.data]
            for response in responses:
                if not isinstance(response, AgentExecutorResponse):
                    continue
                # The instruction the agent received: the user messages of its conversation
                query_messages = [m for m in response.full_conversation if m.role == "user"]
                item = self._agent_item(
                    query_messages or response.full_conversation, response.agent_response, executor.agent
                )
                items_by_agent.setdefault(event.executor_id, []).append(item)

        return await self._evaluate_agent_items(items_by_agent, eval_name)

    # ------------------------------------------------------------------ single agent evaluation
    async def evaluate_agent_final_answer(self, query, response, eval_name):
        """Score the single agent's final answer with the Foundry evaluators (LLM judge).

        Same evaluators and same input as evaluate_final_answer for a workflow (the request
        and the answer text, no system prompt, no tool calls and no tool definitions), so the
        two can be compared. The agent's tool calls are scored by evaluate_agent_work.
        """
        # evaluate_workflow judges only the text of the final output; given the agent and its full
        # response, evaluate_agent would also show the judge the tool calls, results and definitions
        answer = AgentResponse(messages=[Message("assistant", [response.text])])
        results = await evaluate_agent(
            queries=query,
            responses=answer,
            evaluators=self._foundry_evals(self.final_answer_evaluators),
            eval_name=eval_name,
        )
        return self._results_to_dict(results[0])

    async def evaluate_agent_work(self, agent, query, response, eval_name):
        """Score how the single agent worked, its function tool calls included.

        Counterpart of evaluate_agents for a workflow: the agent's evaluators come from
        agent_evaluators and the result has the same format.
        """
        item = self._agent_item([Message("user", [query])], response, agent)
        return await self._evaluate_agent_items({agent.name: [item]}, eval_name)

    async def _evaluate_agent_items(self, items_by_agent, eval_name):
        """Score the items of every agent with the agent's evaluators; one Foundry evaluation run per agent.

        Agents without evaluators in agent_evaluators are skipped. A failed evaluation of one
        agent is recorded under its name and does not stop the others.
        """
        agents = {}
        for agent_name, items in items_by_agent.items():
            evaluators = self.agent_evaluators.get(agent_name)
            if not evaluators:
                continue
            agents[agent_name] = await self._run_evaluation(
                self._evaluate_items(items, evaluators, f"{eval_name} - {agent_name}")
            )

        if not agents:
            return self._skipped_evaluation()
        completed = all(results["status"] == "completed" for results in agents.values())
        return {"status": "completed" if completed else "partial", "error": None, "items": [], "agents": agents}

    async def _evaluate_items(self, items, evaluators, eval_name):
        """Run one Foundry evaluation of the items; tool evaluators are dropped for items without function tools."""
        results = await self._foundry_evals(evaluators).evaluate(items, eval_name=eval_name)
        return self._results_to_dict(results)

    # ------------------------------------------------------------------ rule checks
    def check_final_answer(self, final_answer, test_case, called_agents=None, called_tools=None):
        """Run the rule checks on the final answer and on the routing; no model calls.

        The answer length is always checked. The optional test case fields expected_keywords
        and expected_answer add one check each. The routing checks depend on what was run:
        expected_agents and unexpected_agents are checked only when called_agents is given
        (a workflow), expected_tools and unexpected_tools only when called_tools is given
        (a single agent).
        """
        checks = []

        def add_check(name, passed, details):
            checks.append({"name": name, "score": 1.0 if passed else 0.0, "passed": passed, "details": details})

        word_count = len(final_answer.split())
        add_check(
            "answer_length",
            0 < word_count <= self.max_answer_words,
            f"{word_count} words (limit: {self.max_answer_words})",
        )

        if "expected_keywords" in test_case:
            missing = [k for k in test_case["expected_keywords"] if k.lower() not in final_answer.lower()]
            add_check("expected_keywords", not missing, f"missing: {missing}" if missing else "all keywords found")

        if "expected_answer" in test_case:
            expected_answer = test_case["expected_answer"]
            # Models often write typographic quotes ("I’m"), which must not fail an otherwise exact answer
            found = expected_answer.translate(PLAIN_QUOTES) in final_answer.translate(PLAIN_QUOTES)
            add_check("expected_answer", found, f"expected: {expected_answer}")

        if called_agents is not None and "expected_agents" in test_case:
            not_called = [a for a in test_case["expected_agents"] if a not in called_agents]
            add_check(
                "expected_agents",
                not not_called,
                f"not called: {not_called}" if not_called else "all expected agents called",
            )

        if called_agents is not None and "unexpected_agents" in test_case:
            called = [a for a in test_case["unexpected_agents"] if a in called_agents]
            add_check("unexpected_agents", not called, f"called: {called}" if called else "none called")

        if called_tools is not None and "expected_tools" in test_case:
            not_called = [t for t in test_case["expected_tools"] if t not in called_tools]
            add_check(
                "expected_tools",
                not not_called,
                f"not called: {not_called}" if not_called else "all expected tools called",
            )

        if called_tools is not None and "unexpected_tools" in test_case:
            called = [t for t in test_case["unexpected_tools"] if t in called_tools]
            add_check("unexpected_tools", not called, f"called: {called}" if called else "none called")

        return checks

    # ------------------------------------------------------------------ test cases
    async def evaluate_test_case(self, workflow, test_case, workflow_name):
        """Run one test case through the workflow and evaluate the run.

        A failed run or a failed evaluation is recorded in the returned record instead of
        raised, so one error does not lose the results of the other test cases.
        """
        record = {"id": test_case["id"], "query": test_case["query"]}
        # The agents get today's date in front of the query, as in the chat
        task = add_todays_date(test_case["query"])

        scope = None
        try:
            scope = await self._check_scope(record, test_case)
            if scope is not None and not scope.in_scope:
                return self._refused_record(record, test_case, scope, called_agents=[])
            run = await self.run_workflow(workflow, task)
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
            # Tokens used before the failure still count
            record["token_usage"] = self._add_scope_usage(self.count_workflow_tokens(), scope)
            return record

        workflow_result = run.pop("workflow_result")
        record.update(self._add_scope_run(run, scope))

        called_agents = list(record["token_usage"]["per_agent"])
        record["rule_checks"] = self.check_final_answer(record["final_answer"], test_case, called_agents)

        # The evaluation name is shown in the Foundry portal
        eval_name = f"{workflow_name} - {test_case['id']}"
        if test_case.get("judge_final_answer", True):
            record["final_answer_evaluation"] = await self._run_evaluation(
                self.evaluate_final_answer(workflow, workflow_result, eval_name)
            )
        else:
            record["final_answer_evaluation"] = self._skipped_evaluation()
        if self.agent_evaluation_enabled:
            # Without a specialist call (e.g. a refused request) the evaluation is skipped
            record["agent_evaluation"] = await self._run_evaluation(
                self.evaluate_agents(workflow, workflow_result, eval_name)
            )

        return record

    async def evaluate_agent_test_case(self, agent, test_case):
        """Run one test case through the single agent and evaluate the run.

        The record has the same format as the one of evaluate_test_case, plus "called_tools".
        The final answer and the agent's work are scored as for a workflow, and the rule
        checks cover the tools the agent called instead of the agents of a workflow. Errors
        are recorded instead of raised.
        """
        record = {"id": test_case["id"], "query": test_case["query"]}
        # The agent and the judge get today's date in front of the query, as in a workflow run
        task = add_todays_date(test_case["query"])

        scope = None
        try:
            scope = await self._check_scope(record, test_case)
            if scope is not None and not scope.in_scope:
                return self._refused_record(record, test_case, scope, called_tools={})
            run = await self.run_agent(agent, task)
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
            # Tokens used before the failure still count
            record["token_usage"] = self._add_scope_usage(self.count_workflow_tokens(), scope)
            return record

        response = run.pop("response")
        record.update(self._add_scope_run(run, scope))

        record["rule_checks"] = self.check_final_answer(
            record["final_answer"], test_case, called_tools=record["called_tools"]
        )

        # The evaluation name is shown in the Foundry portal
        eval_name = f"{agent.name} - {test_case['id']}"
        if test_case.get("judge_final_answer", True):
            record["final_answer_evaluation"] = await self._run_evaluation(
                self.evaluate_agent_final_answer(task, response, f"{eval_name} - final answer")
            )
        else:
            record["final_answer_evaluation"] = self._skipped_evaluation()
        if self.agent_evaluation_enabled:
            record["agent_evaluation"] = await self._run_evaluation(
                self.evaluate_agent_work(agent, task, response, eval_name)
            )

        return record

    # ------------------------------------------------------------------ concierge (concierge_agent_and_eval.py)
    def load_concierge_test_cases(self):
        """Read the concierge's test conversations (see inputs/concierge_evaluation_prompts.yaml for the format).

        A case is scripted (query and optional follow_ups) or simulated (query and user, played by the
        UserSimulator). A case with both, or a user without a goal, raises ValueError.
        """
        if not self.concierge_test_cases_path:
            raise ValueError("evaluation.concierge_test_cases_path is not set in config.yaml")
        with open(self.concierge_test_cases_path, "r", encoding="utf-8") as file:
            test_cases = yaml.safe_load(file)["test_cases"]

        for test_case in test_cases:
            user = test_case.get("user")
            if user is None:
                continue
            if test_case.get("follow_ups"):
                raise ValueError(f"Test case '{test_case['id']}': give either follow_ups or user, not both")
            if not isinstance(user, dict) or not user.get("goal"):
                raise ValueError(f"Test case '{test_case['id']}': user needs a goal")
        return test_cases

    async def evaluate_concierge_test_case(
        self, concierge_agent, call_recorder, test_case, workflow_name, user_simulator=None
    ):
        """Run one test conversation with the concierge and evaluate it.

        A test case is scripted or simulated, and either way one session, as in the chat:
        - scripted: the first user message (query) plus optional follow_ups, sent one after another;
          the subagents' questions (ask_user) are skipped;
        - simulated (a "user" field with goal and facts): after the first message, user_simulator plays the
          traveller. After every reply it writes the next message or ends the conversation (goal reached,
          the assistant cannot help, or max_turns user messages), and it answers the subagents' questions.
        Every message is checked by the scope guard first, with the concierge's previous reply as context;
        a refused message ends the conversation, because the concierge never sees it.

        The record has the format of evaluate_test_case, plus "turns" (every message with its scope check,
        the subagents' questions and the reply), "subagent_calls" and, for a simulated case,
        "user_simulation". The final answer is the last reply. The rule checks cover every reply and the
        subagents called in the whole conversation (check_concierge_conversation), the LLM judge scores the
        conversation (evaluate_concierge_conversation), and every subagent call is scored with the
        subagent's evaluators from agent_evaluators. call_recorder is the ConciergeCallRecorder on the
        subagents; user_simulator is the UserSimulator whose ask_user tools the subagents have (needed for
        a simulated case). Errors are recorded instead of raised.
        """
        record = {"id": test_case["id"], "query": test_case["query"]}
        follow_ups = test_case.get("follow_ups") or []
        if follow_ups:
            record["follow_ups"] = follow_ups
        user = test_case.get("user")
        # How a simulated conversation ended; None for a scripted case
        simulation = None
        if user is not None:
            max_turns = test_case.get("max_turns") or (user_simulator.max_turns if user_simulator else 0)
            simulation = {"goal": user["goal"], "max_turns": max_turns, "end": None, "reason": None}
        self.usage_middleware.reset()
        call_recorder.reset()

        # A new session per test case: the concierge remembers the earlier turns of this conversation only
        session = concierge_agent.create_session()
        turns = []
        scopes = []
        # The user's messages (with the date line, as the concierge got them) and its replies, for the judge
        conversation = []
        refused = False
        # The subagents' ask_user tools log their questions in the current turn and, in a simulated case,
        # the simulator answers them
        simulated = user_simulator.start(user, turns) if user_simulator is not None else None
        started = time.perf_counter()
        try:
            if user is not None and user_simulator is None:
                raise ValueError("a simulated test case (user) needs a UserSimulator")
            user_message = test_case["query"]
            while user_message is not None:
                if turns:
                    # The earlier replies of a multi-turn case are not printed anywhere else
                    self._print_concierge_turn(turns[-1], user_message, simulated=simulation is not None)
                previous_reply = turns[-1]["reply"] if turns else None
                turn = {"user": user_message}
                turns.append(turn)

                if self.scope_guard is not None:
                    scope = await self.scope_guard.check(user_message, previous_reply=previous_reply)
                    scope.print_line()
                    scopes.append(scope)
                    turn["scope_guard"] = scope.as_dict()
                    if not scope.in_scope:
                        turn["reply"] = self.scope_guard.refusal
                        refused = True
                        break

                # The concierge gets today's date in front of every message, as in the chat
                task = add_todays_date(user_message)
                response = await concierge_agent.run(task, session=session)
                turn["reply"] = response.text
                conversation += [Message("user", [task]), Message("assistant", [response.text])]
                user_message = await self._next_concierge_message(test_case, turns, user_simulator, simulation)
        except Exception as error:
            record["turns"] = turns
            record["error"] = f"{type(error).__name__}: {error}"
            # Tokens used before the failure still count
            record["token_usage"] = self._add_concierge_scope_usage(self.count_workflow_tokens(), scopes)
            if simulation is not None and simulated is not None:
                simulation["end"], simulation["reason"] = "error", record["error"]
                record["user_simulation"] = self._concierge_simulation_record(simulation, simulated, len(turns))
            return record
        finally:
            if user_simulator is not None:
                user_simulator.finish()

        record["turns"] = turns
        record["final_answer"] = turns[-1]["reply"]
        # The scope checks are inside the measured time: in the chat the user waits for them too. The
        # simulated user's time is not: in the chat the assistant waits for the user then.
        user_seconds = simulated.seconds if simulated is not None else 0.0
        record["duration_seconds"] = round(time.perf_counter() - started - user_seconds, 1)
        record["token_usage"] = self._add_concierge_scope_usage(self.count_workflow_tokens(), scopes)
        record["subagent_calls"] = self._concierge_subagent_calls(call_recorder)
        if scopes:
            # The check that decided: the refusal, or the check of the first message
            record["scope_guard"] = (scopes[-1] if refused else scopes[0]).as_dict()
        if simulation is not None:
            if refused:
                simulation["end"], simulation["reason"] = "refused", "The scope guard refused a message."
            record["user_simulation"] = self._concierge_simulation_record(simulation, simulated, len(turns))

        called_agents = list(record["token_usage"]["per_agent"])
        record["rule_checks"] = self.check_concierge_conversation(
            turns, test_case, called_agents, record.get("user_simulation")
        )

        # The evaluation name is shown in the Foundry portal
        eval_name = f"{workflow_name} - {test_case['id']}"
        # A refusal is not judged: the judge does not know the assistant's scope (see _refused_record)
        if not refused and test_case.get("judge_final_answer", True):
            record["final_answer_evaluation"] = await self._run_evaluation(
                self.evaluate_concierge_conversation(conversation, f"{eval_name} - conversation")
            )
        else:
            record["final_answer_evaluation"] = self._skipped_evaluation()
        if self.agent_evaluation_enabled:
            # Without a subagent call (e.g. a refused request) the evaluation is skipped
            record["agent_evaluation"] = await self._run_evaluation(
                self.evaluate_concierge_subagents(call_recorder, eval_name)
            )

        return record

    async def _next_concierge_message(self, test_case, turns, user_simulator, simulation):
        """Return the next user message of a test conversation, None when it ends.

        Scripted case: the next follow-up. Simulated case: the simulated user's next message. It ends when
        the simulator reports the goal reached or gives up, when max_turns user messages have been sent,
        or when the simulator fails; simulation["end"] and simulation["reason"] say why.
        """
        if simulation is None:
            follow_ups = test_case.get("follow_ups") or []
            # turns holds the first message and the follow-ups sent so far
            sent = len(turns) - 1
            return follow_ups[sent] if sent < len(follow_ups) else None

        try:
            step = await user_simulator.next_step()
        except Exception as error:
            simulation["end"], simulation["reason"] = "error", f"{type(error).__name__}: {error}"
            print(f"[SIMULATED USER] Failed, the conversation ends: {simulation['reason']}")
            return None

        simulation["reason"] = step.reason
        message = step.message.strip()
        if step.status != "continue" or not message:
            simulation["end"] = "gave_up" if step.status == "continue" else step.status
            print(f"[SIMULATED USER] {simulation['end'].replace('_', ' ').capitalize()}: {step.reason}")
            return None
        if len(turns) >= simulation["max_turns"]:
            # The goal is not reached, and the user may send no further message
            simulation["end"] = "max_turns"
            print(f"[SIMULATED USER] Limit of {simulation['max_turns']} messages reached: {step.reason}")
            return None
        print(f"[SIMULATED USER] Goes on: {step.reason}")
        return message

    def check_concierge_conversation(self, turns, test_case, called_agents, simulation=None):
        """Run the rule checks on a concierge conversation; no model calls.

        The checks of check_final_answer, with these differences:
        - answer_length: every reply must keep the word limit, not only the last one;
        - expected_keywords and expected_answer: the last reply of a scripted case, all replies of a
          simulated one (its goal is often met over several replies);
        - goal_reached, for a simulated case only (simulation = record["user_simulation"]): the simulated
          user reported its goal reached within max_turns. The verdict comes from the simulator's model; it
          is read here, without another model call.
        """
        replies = [turn["reply"] for turn in turns]
        text = "\n\n".join(replies) if simulation is not None else replies[-1]
        checks = self.check_final_answer(text, test_case, called_agents)

        word_counts = [len(reply.split()) for reply in replies]
        counts = f"{word_counts[0]} words" if len(word_counts) == 1 else f"words per reply: {word_counts}"
        length_check = self._rule_check(
            "answer_length",
            all(0 < count <= self.max_answer_words for count in word_counts),
            f"{counts} (limit: {self.max_answer_words})",
        )
        checks = [length_check if check["name"] == "answer_length" else check for check in checks]

        if simulation is not None:
            reached = simulation["end"] == "goal_reached"
            if reached:
                details = f"after {simulation['user_messages']} user messages (limit: {simulation['max_turns']})"
            else:
                details = f"not reached ({simulation['end']}): {simulation['reason']}"
            checks.append(self._rule_check("goal_reached", reached, details))
        return checks

    async def evaluate_concierge_conversation(self, conversation, eval_name):
        """Score the concierge's conversation with the evaluators of concierge_answer_evaluators (LLM judge).

        conversation: the user's messages (with the date line, as the concierge got them) and its replies,
        text only, as for the other set-ups. Agent Framework splits it into what the judge sees as the
        query and as the response:
        - per_turn: every reply, with the earlier turns as the query (EvalItem.per_turn_items: one item per reply);
        - whole_conversation: the first message as the query and the rest of the conversation as the
          response (ConversationSplit.FULL): did the conversation serve the request?
        With one message both are the same item, so it is scored once with all evaluators: the same input as
        evaluate_agent_final_answer. Otherwise the two Foundry evaluation runs run at the same time, and the
        result merges them (_merge_concierge_evaluations), so the report and the summary keep their format.
        """
        per_turn = self.concierge_answer_evaluators.get("per_turn") or []
        whole = self.concierge_answer_evaluators.get("whole_conversation") or []
        if not per_turn and not whole:
            raise ValueError("evaluation.concierge_answer_evaluators is not set in config.yaml")

        turn_items = EvalItem.per_turn_items(conversation)
        if len(turn_items) <= 1:
            evaluators = list(dict.fromkeys([*per_turn, *whole]))
            result = await self._run_evaluation(self._evaluate_items(turn_items, evaluators, eval_name))
            return self._merge_concierge_evaluations([("whole conversation", result)])

        runs = []
        if per_turn:
            runs.append(("reply", self._evaluate_items(turn_items, per_turn, f"{eval_name} per reply")))
        if whole:
            whole_item = EvalItem(conversation=conversation, split_strategy=ConversationSplit.FULL)
            runs.append(("whole conversation", self._evaluate_items([whole_item], whole, f"{eval_name} as a whole")))
        results = await asyncio.gather(*(self._run_evaluation(evaluation) for _, evaluation in runs))
        return self._merge_concierge_evaluations([(label, result) for (label, _), result in zip(runs, results)])

    async def evaluate_concierge_subagents(self, call_recorder, eval_name):
        """Score every subagent call of a concierge conversation with the subagent's evaluators.

        Counterpart of evaluate_agents for the Magentic workflow, with the same result format: one item
        per call, holding the task the concierge wrote (with the date line), the subagent's reply with its
        function tool calls and, as context, what its tools returned. The concierge itself is not scored
        here: its routing is checked by the rule checks and its replies by evaluate_concierge_conversation.
        """
        items_by_agent = {}
        for agent, task_messages, response in call_recorder.calls:
            items_by_agent.setdefault(agent.name, []).append(self._agent_item(task_messages, response, agent))
        return await self._evaluate_agent_items(items_by_agent, eval_name)

    def _add_concierge_scope_usage(self, token_usage, scopes):
        """Add the tokens of all scope checks of a conversation as one scopeGuard entry next to the agents."""
        checks = [scope for scope in scopes if not scope.skipped]
        if not checks:
            return token_usage
        guard = self._sum_counts(self._usage_to_dict(scope.usage, 1) for scope in checks)
        return {
            "total": self._sum_counts([token_usage["total"], guard]),
            "per_agent": {SCOPE_GUARD_NAME: guard, **token_usage["per_agent"]},
        }

    @classmethod
    def _concierge_subagent_calls(cls, call_recorder):
        """Return {subagent: {"calls": n, "tool_calls": {tool: n}}}; ask_user shows how often a subagent asked."""
        activity = {}
        for agent, _, response in call_recorder.calls:
            entry = activity.setdefault(agent.name, {"calls": 0, "tool_calls": {}})
            entry["calls"] += 1
            for tool_name, count in cls.count_tool_calls(response).items():
                entry["tool_calls"][tool_name] = entry["tool_calls"].get(tool_name, 0) + count
        return activity

    def _concierge_simulation_record(self, simulation, simulated, user_messages):
        """Return record["user_simulation"]: how the simulated conversation went, and the simulator's own cost.

        The simulator's tokens and time are kept apart from token_usage and duration_seconds, which measure
        the assistant: the simulated user is part of the test.
        """
        return {
            "goal": simulation["goal"],
            "max_turns": simulation["max_turns"],
            "user_messages": user_messages,
            # goal_reached, gave_up, max_turns, refused or error
            "end": simulation["end"],
            "reason": simulation["reason"],
            "questions_answered": simulated.questions_answered,
            "duration_seconds": round(simulated.seconds, 1),
            "token_usage": self._usage_to_dict(simulated.usage or {}, simulated.calls),
        }

    def summarize_concierge_simulation(self, case_records):
        """Aggregate the simulated conversations of a report; None when there were none.

        concierge_agent_and_eval.py adds it to the summary as "user_simulation".
        """
        simulations = [record["user_simulation"] for record in case_records if "user_simulation" in record]
        if not simulations:
            return None
        ends = {}
        for simulation in simulations:
            ends[simulation["end"]] = ends.get(simulation["end"], 0) + 1
        return {
            "conversations": len(simulations),
            "goal_reached": ends.get("goal_reached", 0),
            "ends": ends,
            "average_user_messages": round(sum(s["user_messages"] for s in simulations) / len(simulations), 1),
            "questions_answered": sum(s["questions_answered"] for s in simulations),
            "tokens": self._sum_counts(s["token_usage"] for s in simulations),
        }

    @staticmethod
    def _merge_concierge_evaluations(parts):
        """Merge the evaluation runs of one concierge conversation into the result format.

        parts: (label, result) per run; every item gets the label ("reply" or "whole conversation"), because
        the judge's item order is not guaranteed and the metric names alone do not say which split was scored.
        The status is "partial" when a run failed and another completed.
        """
        results = [result for _, result in parts]
        statuses = {result["status"] for result in results}
        if statuses == {"completed"}:
            status = "completed"
        elif "completed" in statuses:
            status = "partial"
        else:
            status = results[0]["status"] if len(statuses) == 1 else "error"
        errors = [result["error"] for result in results if result.get("error")]
        report_urls = [result["report_url"] for result in results if result.get("report_url")]
        return {
            "status": status,
            "passed": sum(result.get("passed") or 0 for result in results),
            "failed": sum(result.get("failed") or 0 for result in results),
            "error": "; ".join(errors) or None,
            "report_url": report_urls[0] if report_urls else None,
            "report_urls": report_urls,
            "items": [{"label": label, **item} for label, result in parts for item in result["items"]],
            "agents": {},
        }

    @staticmethod
    def _rule_check(name, passed, details):
        """One rule check in the format of check_final_answer."""
        return {"name": name, "score": 1.0 if passed else 0.0, "passed": passed, "details": details}

    @staticmethod
    def _print_concierge_turn(turn, next_message, simulated=False):
        """Print a reply of a multi-turn test case and the next user message."""
        print("\n" + "=" * 50)
        print("CONCIERGE (earlier turn):")
        print("=" * 50)
        print(turn["reply"])
        print("=" * 50)
        print(f"\n{'Simulated user' if simulated else 'You'}: {next_message}")

    # ------------------------------------------------------------------ scope guard
    async def _check_scope(self, record, test_case):
        """Run the scope guard on the test prompt, as the chat does; None without a guard.

        The check is stored in record["scope_guard"]. A prompt is checked as the user wrote it,
        without the date line.
        """
        # Tokens of the previous test case must not count when this one fails before its run
        self.usage_middleware.reset()
        if self.scope_guard is None:
            return None
        scope = await self.scope_guard.check(test_case["query"])
        scope.print_line()
        record["scope_guard"] = scope.as_dict()
        return scope

    def _add_scope_usage(self, token_usage, scope):
        """Add the scope guard's tokens to the token usage of a run, as one more entry next to the agents."""
        if scope is None or scope.skipped:
            return token_usage
        guard = self._usage_to_dict(scope.usage, 1)
        return {
            "total": self._sum_counts([token_usage["total"], guard]),
            "per_agent": {SCOPE_GUARD_NAME: guard, **token_usage["per_agent"]},
        }

    def _add_scope_run(self, run, scope):
        """Add the scope guard's tokens and duration to a finished run: in the chat the user waits for both."""
        if scope is None or scope.skipped:
            return run
        run["token_usage"] = self._add_scope_usage(run["token_usage"], scope)
        run["duration_seconds"] = round(run["duration_seconds"] + scope.duration_seconds, 1)
        return run

    def _refused_record(self, record, test_case, scope, called_agents=None, called_tools=None):
        """Complete the record of a test prompt that the scope guard refused: no agent ran.

        The rule checks run on the refusal as on any final answer, so a travel prompt refused by
        mistake fails its expected_agents / expected_tools check. The LLM judge is skipped: there is
        no workflow or agent response to give it.
        """
        record["final_answer"] = self.scope_guard.refusal
        record["duration_seconds"] = scope.duration_seconds
        record["token_usage"] = self._add_scope_usage(self.count_workflow_tokens(), scope)
        if called_tools is not None:
            record["called_tools"] = called_tools
        record["rule_checks"] = self.check_final_answer(
            record["final_answer"], test_case, called_agents=called_agents, called_tools=called_tools
        )
        record["final_answer_evaluation"] = self._skipped_evaluation()
        if self.agent_evaluation_enabled:
            record["agent_evaluation"] = self._skipped_evaluation()
        return record

    # ------------------------------------------------------------------ report
    def create_report(self, workflow_name, case_records):
        """Build the report: metadata, the summary over all runs and the record of every run."""
        return {
            "workflow_name": workflow_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "judge_model": self.judge_client.model,
            "summary": self.summarize(case_records),
            "cases": case_records,
        }

    def summarize(self, case_records):
        """Aggregate the records: passed / failed counts and average score per metric, tokens and duration."""
        completed = [record for record in case_records if "error" not in record]
        durations = [record["duration_seconds"] for record in completed]

        final_answer_scores = []
        scores_by_agent = {}
        judge_tokens = 0
        for record in completed:
            for target, item in self._evaluated_items(record):
                judge_tokens += self._judge_tokens(item)
                if target == "final_answer":
                    final_answer_scores.extend(item["scores"])
                else:
                    scores_by_agent.setdefault(target, []).extend(item["scores"])

        # Failed runs are included: they used tokens too
        usages = [record["token_usage"] for record in case_records]
        workflow_total = self._sum_counts(usage["total"] for usage in usages)
        agent_names = sorted({name for usage in usages for name in usage["per_agent"]})

        return {
            "runs": len(case_records),
            "failed_runs": len(case_records) - len(completed),
            # Test prompts the scope guard refused before any agent ran
            "scope_guard_refusals": sum(
                1 for record in case_records if record.get("scope_guard", {}).get("in_scope") is False
            ),
            "average_duration_seconds": round(sum(durations) / len(durations), 1) if durations else None,
            "final_answer": self._aggregate_scores(final_answer_scores),
            "rule_checks": self._aggregate_scores(
                check for record in completed for check in record["rule_checks"]
            ),
            "agents": {name: self._aggregate_scores(scores) for name, scores in sorted(scores_by_agent.items())},
            "tokens": {
                "workflow_total": workflow_total,
                "workflow_average_per_run": {
                    key: round(value / len(usages)) for key, value in workflow_total.items()
                },
                "per_agent_total": {
                    name: self._sum_counts(
                        usage["per_agent"][name] for usage in usages if name in usage["per_agent"]
                    )
                    for name in agent_names
                },
                # Tokens used by the LLM judge
                "judge_total": judge_tokens,
            },
        }

    def save_report(self, report):
        """Write the report as a JSON file in the output directory and return the file path."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path(self.output_dir) / f"evaluation_{report['workflow_name']}_{timestamp}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as file:
            # default=str: evaluator details can hold objects that are not JSON types
            json.dump(report, file, indent=2, ensure_ascii=False, default=str)

        return output_path

    def print_case_result(self, record):
        """Print the outcome of one test case to the console."""
        if "error" in record:
            print(f"❌ Run failed: {record['error']}")
            return

        print("\n" + "=" * 50)
        print("ASSISTANT REPLY:")
        print("=" * 50)
        print(record["final_answer"])
        print("=" * 50)

        tokens = record["token_usage"]["total"]
        print(
            f"[EVAL] Duration: {record['duration_seconds']} s, tokens: {tokens['total_token_count']} "
            f"(input {tokens['input_token_count']}, output {tokens['output_token_count']}) "
            f"in {tokens['calls']} agent calls"
        )
        scope = record.get("scope_guard")
        if scope and not scope["skipped"]:
            outcome = "travel request" if scope["in_scope"] else "refused, no agent ran"
            failed = f", check failed: {scope['error']}" if scope["error"] else ""
            print(f"[EVAL] Scope guard: {outcome} ({scope['duration_seconds']} s{failed})")
        if "called_tools" in record:
            print(f"[EVAL] Tools called: {record['called_tools'] or 'none'}")
        for check in record["rule_checks"]:
            print(f"[EVAL] {'✅' if check['passed'] else '❌'} {check['name']}: {check['details']}")

        self._print_evaluation("Final answer (LLM judge)", record["final_answer_evaluation"])
        for agent_name, results in record.get("agent_evaluation", {}).get("agents", {}).items():
            self._print_evaluation(f"Agent {agent_name} (LLM judge)", results)

    def print_summary(self, summary):
        """Print the summary of a report to the console."""
        print("\n" + "=" * 50)
        print("EVALUATION SUMMARY")
        print("=" * 50)
        print(f"Runs: {summary['runs']} (failed: {summary['failed_runs']})")
        print(f"Refused by the scope guard: {summary['scope_guard_refusals']}")
        print(f"Average duration: {summary['average_duration_seconds']} s")

        self._print_metrics("Final answer (LLM judge)", summary["final_answer"])
        self._print_metrics("Rule checks", summary["rule_checks"])
        for agent_name, metrics in summary["agents"].items():
            self._print_metrics(f"Agent: {agent_name}", metrics)

        tokens = summary["tokens"]
        print("\nTokens:")
        print(f"  total: {tokens['workflow_total'].get('total_token_count', 0)}")
        print(f"  average per run: {tokens['workflow_average_per_run'].get('total_token_count', 0)}")
        for agent_name, usage in tokens["per_agent_total"].items():
            print(f"  {agent_name}: {usage['total_token_count']} tokens in {usage['calls']} calls")
        print(f"Tokens (LLM judge): {tokens['judge_total']}")
        print("=" * 50)

    # ------------------------------------------------------------------ helpers
    def _foundry_evals(self, evaluators):
        """Return the Foundry evaluation provider for the given built-in evaluator names."""
        return FoundryEvals(client=self.judge_client, evaluators=evaluators, timeout=self.timeout_seconds)

    def _results_to_dict(self, results):
        """Convert EvalResults and its per-agent sub-results into JSON-serialisable data."""
        return {
            "status": results.status,
            "passed": results.passed,
            "failed": results.failed,
            "error": results.error,
            "report_url": results.report_url,
            "items": [
                {
                    "status": item.status,
                    "error": item.error_message,
                    "judge_tokens": item.token_usage,
                    "scores": [
                        {"name": score.name, "score": score.score, "passed": score.passed, "details": score.sample}
                        for score in item.scores
                    ],
                }
                for item in results.items
            ],
            "agents": {name: self._results_to_dict(sub) for name, sub in results.sub_results.items()},
        }

    @classmethod
    def _agent_item(cls, query_messages, response, agent):
        """Build the evaluation item of one agent call.

        The item holds the instruction, the agent's reply with its function tool calls and
        results, the agent's function tools (for the tool evaluators) and, as context, the text
        of everything its tools returned (for groundedness). FoundryEvals does not pass MCP
        results such as knowledge base passages to the judge, so they reach it only as context.
        """
        tools = normalize_tools(getattr(agent, "default_options", {}).get("tools", []))
        function_tools = [tool for tool in tools if isinstance(tool, FunctionTool)]

        tool_outputs = [
            cls._as_text(getattr(content, attribute))
            for message in response.messages
            for content in message.contents
            if (attribute := TOOL_OUTPUT_ATTRIBUTES.get(content.type))
        ]
        context = "\n\n".join(output for output in tool_outputs if output)

        return EvalItem(
            conversation=[*query_messages, *response.messages],
            tools=function_tools or None,
            context=context or NO_TOOL_OUTPUT,
        )

    @classmethod
    def _as_text(cls, value):
        """Return a tool output as text: a string, a list of text contents or a JSON-like value."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(cls._as_text(item) for item in value)
        text = getattr(value, "text", None)
        if text is not None:
            return text
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _skipped_evaluation():
        """Return the result of an evaluation that was not run, in the result format."""
        return {"status": "skipped", "error": None, "items": [], "agents": {}}

    @staticmethod
    async def _run_evaluation(evaluation):
        """Await one evaluation; an error is returned in the result format instead of raised."""
        try:
            return await evaluation
        except Exception as error:
            print(f"[EVAL] Evaluation failed: {error}")
            return {"status": "error", "error": f"{type(error).__name__}: {error}", "items": [], "agents": {}}

    @staticmethod
    def _final_answer_text(workflow_result):
        """Return the text of the last workflow output, '' when the workflow produced none."""
        outputs = workflow_result.get_outputs()
        if not outputs:
            return ""

        final_output = outputs[-1]
        if isinstance(final_output, list):
            return " ".join(str(getattr(message, "text", message)) for message in final_output)
        return str(getattr(final_output, "text", final_output))

    @staticmethod
    def _usage_to_dict(usage, calls):
        """Return the token counts of one usage_details as plain integers plus the number of calls."""
        tokens = {field: usage.get(field) or 0 for field in TOKEN_FIELDS}
        if not tokens["total_token_count"]:
            tokens["total_token_count"] = tokens["input_token_count"] + tokens["output_token_count"]
        tokens["calls"] = calls
        return tokens

    @staticmethod
    def _evaluated_items(record):
        """Yield (target, item) for every judged item of a record; target is 'final_answer' or the agent name."""
        for item in record["final_answer_evaluation"]["items"]:
            yield "final_answer", item
        for agent_name, results in record.get("agent_evaluation", {}).get("agents", {}).items():
            for item in results["items"]:
                yield agent_name, item

    @staticmethod
    def _judge_tokens(item):
        """Return the tokens the LLM judge used for one judged item.

        Foundry reports the usage per evaluator in the details of every score; the usage of
        the item itself is used when it is given.
        """
        item_usage = item["judge_tokens"] or {}
        if item_usage.get("total_tokens"):
            return item_usage["total_tokens"]

        total = 0
        for score in item["scores"]:
            details = score["details"] if isinstance(score["details"], dict) else {}
            total += (details.get("usage") or {}).get("total_tokens") or 0
        return total

    @staticmethod
    def _aggregate_scores(scores):
        """Return the passed / failed counts and the average score per metric name."""
        metrics = {}
        for score in scores:
            metric = metrics.setdefault(score["name"], {"passed": 0, "failed": 0, "values": []})
            if score["passed"] is True:
                metric["passed"] += 1
            elif score["passed"] is False:
                metric["failed"] += 1
            if score["score"] is not None:
                metric["values"].append(score["score"])

        return {
            name: {
                "passed": metric["passed"],
                "failed": metric["failed"],
                "average_score": round(sum(metric["values"]) / len(metric["values"]), 2) if metric["values"] else None,
            }
            for name, metric in metrics.items()
        }

    @staticmethod
    def _sum_counts(entries):
        """Sum dictionaries of integer counts key by key."""
        total = {}
        for entry in entries:
            for key, value in entry.items():
                total[key] = total.get(key, 0) + value
        return total

    @classmethod
    def _print_evaluation(cls, title, evaluation):
        """Print one evaluation: the score of every evaluator, and the judge's reason for a failed one.

        An agent called several times has one item per call, so its evaluators are listed once per call.
        A concierge conversation has labelled items ("reply", "whole conversation") and can be "partial".
        """
        if evaluation["status"] not in ("completed", "partial"):
            error = f": {evaluation['error']}" if evaluation.get("error") else ""
            print(f"[EVAL] {title}: {evaluation['status']}{error}")
            return

        print(f"[EVAL] {title}:")
        if evaluation["status"] == "partial":
            print(f"[EVAL]   ⚠️ partly failed: {evaluation['error']}")
        for item in evaluation["items"]:
            if item["error"]:
                print(f"[EVAL]   ⚠️ item error: {item['error']}")
            label = f" ({item['label']})" if item.get("label") else ""
            for score in item["scores"]:
                mark = {True: "✅", False: "❌"}.get(score["passed"], "➖")
                line = f"[EVAL]   {mark} {score['name']}{label}: {score['score']}"
                reason = cls._judge_reason(score["details"]) if score["passed"] is False else ""
                print(f"{line} - {reason[:JUDGE_REASON_CHARS]}" if reason else line)

    @staticmethod
    def _judge_reason(details):
        """Return the judge's reason from the raw evaluator output, '' when it has none.

        The judge answers with a JSON object that holds the reason; tool evaluators may return no details.
        """
        if not isinstance(details, dict):
            return ""
        for output in details.get("output") or []:
            content = output.get("content") if isinstance(output, dict) else None
            try:
                parsed = json.loads(content)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict) and parsed.get("reason"):
                return str(parsed["reason"])
        return ""

    @staticmethod
    def _print_metrics(title, metrics):
        if not metrics:
            return
        print(f"\n{title}:")
        for name, metric in metrics.items():
            print(f"  {name}: {metric['passed']} passed, {metric['failed']} failed, average score {metric['average_score']}")
