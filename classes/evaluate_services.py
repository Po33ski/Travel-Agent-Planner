import json
import time
from datetime import datetime
from pathlib import Path

import yaml
from agent_framework import AgentMiddleware, Message, add_usage_details, evaluate_agent, evaluate_workflow
from agent_framework.foundry import FoundryEvals, evaluate_traces

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


class TokenUsageMiddleware(AgentMiddleware):
    """
    Agent middleware that records the token usage and the response id of every agent call.
    Add one shared instance to the middleware list of every evaluated agent: the single agent,
    or every agent of a workflow, the manager included. Usage is read from usage_details of
    the agent response, which is only filled for non-streaming runs. The response id names
    the response stored in Foundry, which the judge can read itself.
    """

    def __init__(self):
        self.usage_by_agent = {}
        self.calls_by_agent = {}
        # Agent name -> ids of the final response of every call of that agent
        self.response_ids_by_agent = {}

    def reset(self):
        """Forget the recorded calls, e.g. before the next run."""
        self.usage_by_agent.clear()
        self.calls_by_agent.clear()
        self.response_ids_by_agent.clear()

    async def process(self, context, call_next):
        await call_next()

        agent_name = context.agent.name or "unknown"
        usage = getattr(context.result, "usage_details", None)
        self.calls_by_agent[agent_name] = self.calls_by_agent.get(agent_name, 0) + 1
        self.usage_by_agent[agent_name] = add_usage_details(self.usage_by_agent.get(agent_name), usage)

        response_id = getattr(context.result, "response_id", None)
        if response_id:
            self.response_ids_by_agent.setdefault(agent_name, []).append(response_id)


class _AgentEvaluators:
    """
    Evaluation provider for agent_evaluation_source "conversation": the script sends the conversation.
    It gives the judge the agent's system prompt and applies agent_evaluator_exclusions.
    Without the system prompt the judge knows only the instruction the agent received, and
    fails the agent for everything the instruction asked for and the agent's rules do not require.
    """

    name = "Microsoft Foundry"

    def __init__(self, service, agent_name=None):
        """
        Args:
            service: the EvaluateService that owns the settings.
            agent_name: the scored agent. Leave empty for the agents of a workflow:
                evaluate_workflow() calls evaluate() once per agent and ends the evaluation
                name with " — <agent name>", which is the only place the agent can be read from.
        """
        self._service = service
        self._agent_name = agent_name

    async def evaluate(self, items, *, eval_name="Agent Framework Eval"):
        agent_name = self._agent_name or next(
            (name for name in self._service.known_agent_names() if eval_name.endswith(f" — {name}")), None
        )

        system_prompt = self._service.agent_system_prompts.get(agent_name)
        if system_prompt:
            self._service.agents_judged_with_prompt.add(agent_name)
            for item in items:
                item.conversation = [Message("system", [system_prompt]), *item.conversation]

        evaluators = self._service._agent_evaluators_for(agent_name)
        return await self._service._foundry_evals(evaluators).evaluate(items, eval_name=eval_name)


class EvaluateService:
    """
    Evaluation of a single agent or a multi-agent workflow: answer quality, rule checks and token usage.
    Quality is scored by the Microsoft Foundry built-in evaluators (an LLM judge running in the
    Foundry project), rule checks run locally without model calls and token usage is collected
    by usage_middleware. Both kinds of runs produce the same report, so a single agent and
    different workflows can be compared.
    """

    def __init__(self, judge_client, config, max_answer_words):
        """
        Args:
            judge_client: FoundryChatClient whose model deployment is used as the LLM judge.
            config: content of config.yaml; the "evaluation" section is used.
            max_answer_words: word limit of the final answer, as set in the system prompt
                of the evaluated agent or workflow manager.
        """
        settings = config["evaluation"]
        self.judge_client = judge_client
        self.max_answer_words = max_answer_words
        self.test_cases_path = settings["test_cases_path"]
        self.output_dir = settings["output_dir"]
        self.final_answer_evaluators = settings["final_answer_evaluators"]
        self.agent_evaluators = settings["agent_evaluators"]
        # "responses": Foundry reads the agents' stored responses; "conversation": the script sends them
        self.agent_evaluation_source = settings["agent_evaluation_source"]
        self.agent_evaluator_exclusions = settings["agent_evaluator_exclusions"][self.agent_evaluation_source]
        # Agent name -> system prompt, given to the judge when the agent's own work is scored
        self.agent_system_prompts = self._load_agent_system_prompts(settings["agent_prompts_dir"])
        self.agents_judged_with_prompt = set()
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

        The result holds one entry per agent under "agents". The evaluators listed for an agent
        in agent_evaluator_exclusions are skipped for that agent.

        With agent_evaluation_source "responses" Foundry reads every agent's stored responses
        itself, so the judge sees the calls and results of all tools, the server-side ones
        included. With "conversation" the script sends the conversation: the judge sees function
        tools only and gets the agent's system prompt.
        """
        if self.agent_evaluation_source == "responses":
            # The manager is recorded too, but it is not a workflow executor: its replies are
            # progress checks, and its final answer is scored by evaluate_final_answer
            agent_names = [
                name for name in self.usage_middleware.response_ids_by_agent if name in workflow.executors
            ]
            agents = {}
            for agent_name in agent_names:
                agents[agent_name] = await self._run_evaluation(self._evaluate_stored_responses(agent_name, eval_name))
            return self._combine_agent_results(agents)

        results = await evaluate_workflow(
            workflow=workflow,
            workflow_result=workflow_result,
            evaluators=_AgentEvaluators(self),
            eval_name=eval_name,
            include_overall=False,
        )
        return self._results_to_dict(results[0])

    # ------------------------------------------------------------------ single agent evaluation
    async def evaluate_agent_final_answer(self, agent, query, response, eval_name):
        """Score the single agent's final answer with the Foundry evaluators (LLM judge).

        Same evaluators and same input as evaluate_final_answer for a workflow (the request
        and the answer, no system prompt), so the two can be compared.
        """
        results = await evaluate_agent(
            agent=agent,
            queries=query,
            responses=response,
            evaluators=self._foundry_evals(self.final_answer_evaluators),
            eval_name=eval_name,
        )
        return self._results_to_dict(results[0])

    async def evaluate_agent_work(self, agent, query, response, eval_name):
        """Score how the single agent worked: its tool use and its reply against its own rules.

        Counterpart of evaluate_agents for a workflow, with the same two sources: Foundry reads
        the agent's stored response ("responses"), or the script sends the conversation with the
        agent's system prompt ("conversation"). agent_evaluator_exclusions are applied.
        """
        if self.agent_evaluation_source == "responses":
            return await self._evaluate_stored_responses(agent.name, eval_name)

        results = await evaluate_agent(
            agent=agent,
            queries=query,
            responses=response,
            evaluators=_AgentEvaluators(self, agent.name),
            eval_name=eval_name,
        )
        return self._results_to_dict(results[0])

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
            add_check("expected_answer", expected_answer in final_answer, f"expected: {expected_answer}")

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

        try:
            run = await self.run_workflow(workflow, test_case["query"])
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
            # Tokens used before the failure still count
            record["token_usage"] = self.count_workflow_tokens()
            return record

        workflow_result = run.pop("workflow_result")
        record.update(run)

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
            # The manager is not a workflow executor; without a specialist call (e.g. a refused
            # request) there is nothing to evaluate per agent
            if any(agent_name in workflow.executors for agent_name in called_agents):
                record["agent_evaluation"] = await self._run_evaluation(
                    self.evaluate_agents(workflow, workflow_result, eval_name)
                )
            else:
                record["agent_evaluation"] = self._skipped_evaluation()

        return record

    async def evaluate_agent_test_case(self, agent, test_case):
        """Run one test case through the single agent and evaluate the run.

        The record has the same format as the one of evaluate_test_case, plus "called_tools".
        The final answer is scored as for a workflow, the agent's work (tool use included)
        against the agent's system prompt, and the rule checks cover the tools the agent
        called instead of the agents of a workflow. Errors are recorded instead of raised.
        """
        record = {"id": test_case["id"], "query": test_case["query"]}

        try:
            run = await self.run_agent(agent, test_case["query"])
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
            # Tokens used before the failure still count
            record["token_usage"] = self.count_workflow_tokens()
            return record

        response = run.pop("response")
        record.update(run)

        record["rule_checks"] = self.check_final_answer(
            record["final_answer"], test_case, called_tools=record["called_tools"]
        )

        # The evaluation name is shown in the Foundry portal
        eval_name = f"{agent.name} - {test_case['id']}"
        if test_case.get("judge_final_answer", True):
            record["final_answer_evaluation"] = await self._run_evaluation(
                self.evaluate_agent_final_answer(agent, test_case["query"], response, f"{eval_name} - final answer")
            )
        else:
            record["final_answer_evaluation"] = self._skipped_evaluation()
        if self.agent_evaluation_enabled:
            results = await self._run_evaluation(
                self.evaluate_agent_work(agent, test_case["query"], response, f"{eval_name} - agent")
            )
            record["agent_evaluation"] = {
                "status": results["status"],
                "error": results["error"],
                "items": [],
                "agents": {agent.name: results},
            }

        return record

    # ------------------------------------------------------------------ report
    def create_report(self, workflow_name, case_records):
        """Build the report: metadata, the summary over all runs and the record of every run."""
        return {
            "workflow_name": workflow_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "judge_model": self.judge_client.model,
            "agent_evaluation_source": self.agent_evaluation_source,
            # Agents whose system prompt the judge received when their own work was scored
            "agents_judged_with_prompt": sorted(self.agents_judged_with_prompt),
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
        if "called_tools" in record:
            print(f"[EVAL] Tools called: {record['called_tools'] or 'none'}")
        for check in record["rule_checks"]:
            print(f"[EVAL] {'✅' if check['passed'] else '❌'} {check['name']}: {check['details']}")

        final_answer_evaluation = record["final_answer_evaluation"]
        print(
            f"[EVAL] Final answer (LLM judge): {final_answer_evaluation['status']}, "
            f"{final_answer_evaluation.get('passed', 0)} passed, {final_answer_evaluation.get('failed', 0)} failed"
        )

    def print_summary(self, summary):
        """Print the summary of a report to the console."""
        print("\n" + "=" * 50)
        print("EVALUATION SUMMARY")
        print("=" * 50)
        print(f"Runs: {summary['runs']} (failed: {summary['failed_runs']})")
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

    async def _evaluate_stored_responses(self, agent_name, eval_name):
        """Let Foundry score the responses of one agent that are stored in the project.

        Only the response ids are sent. Foundry retrieves each response with its input, its
        output and every tool call, and runs the evaluators on it.
        """
        response_ids = self.usage_middleware.response_ids_by_agent.get(agent_name, [])
        if not response_ids:
            return self._skipped_evaluation()

        results = await evaluate_traces(
            evaluators=self._agent_evaluators_for(agent_name),
            client=self.judge_client,
            model=self.judge_client.model,
            response_ids=response_ids,
            eval_name=f"{eval_name} — {agent_name}",
            timeout=self.timeout_seconds,
        )
        return self._results_to_dict(results)

    @staticmethod
    def _combine_agent_results(agents):
        """Return the per-agent results in the result format, with the counts summed up."""
        all_completed = all(results["status"] == "completed" for results in agents.values())
        return {
            "status": "completed" if all_completed else "partial",
            "passed": sum(results.get("passed", 0) for results in agents.values()),
            "failed": sum(results.get("failed", 0) for results in agents.values()),
            "error": None,
            "report_url": None,
            "items": [],
            "agents": agents,
        }

    def known_agent_names(self):
        """Return the agents that have a system prompt or evaluator exclusions."""
        return set(self.agent_system_prompts) | set(self.agent_evaluator_exclusions)

    @staticmethod
    def _load_agent_system_prompts(prompts_dir):
        """Read the agents' system prompts from the <agent>_config.yaml files: agent name -> prompt.

        These are the local files the agents are deployed from. The judge is only accurate
        when the deployed agents use the same prompts.
        """
        prompts = {}
        # rglob: the single agent's files are in a subfolder
        for path in sorted(Path(prompts_dir).rglob("*_config.yaml")):
            with open(path, "r", encoding="utf-8") as file:
                agent_config = yaml.safe_load(file)
            prompts[agent_config["agent_name"]] = agent_config["system_prompt"]
        return prompts

    def _agent_evaluators_for(self, agent_name):
        """Return agent_evaluators without the evaluators excluded for this agent."""
        excluded = self.agent_evaluator_exclusions.get(agent_name, [])
        return [evaluator for evaluator in self.agent_evaluators if evaluator not in excluded]

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
                    "response_id": item.response_id,
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

    @staticmethod
    def _print_metrics(title, metrics):
        if not metrics:
            return
        print(f"\n{title}:")
        for name, metric in metrics.items():
            print(f"  {name}: {metric['passed']} passed, {metric['failed']} failed, average score {metric['average_score']}")
