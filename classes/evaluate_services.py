import json
import time
from datetime import datetime
from pathlib import Path

from agent_framework import AgentMiddleware, add_usage_details, evaluate_workflow
from agent_framework.foundry import FoundryEvals

# Token counts reported in usage_details of an agent response. The input count includes
# the cached input tokens and the output count includes the reasoning tokens.
TOKEN_FIELDS = (
    "input_token_count",
    "cache_read_input_token_count",
    "output_token_count",
    "reasoning_output_token_count",
    "total_token_count",
)


class TokenUsageMiddleware(AgentMiddleware):
    """
    Agent middleware that records the token usage of every agent call.
    Add one shared instance to the middleware list of every agent of a workflow, the manager
    included. Usage is read from usage_details of the agent response, which is only filled
    for non-streaming runs.
    """

    def __init__(self):
        self.usage_by_agent = {}
        self.calls_by_agent = {}

    def reset(self):
        """Forget the recorded usage, e.g. before the next workflow run."""
        self.usage_by_agent.clear()
        self.calls_by_agent.clear()

    async def process(self, context, call_next):
        await call_next()

        agent_name = context.agent.name or "unknown"
        usage = getattr(context.result, "usage_details", None)
        self.calls_by_agent[agent_name] = self.calls_by_agent.get(agent_name, 0) + 1
        self.usage_by_agent[agent_name] = add_usage_details(self.usage_by_agent.get(agent_name), usage)


class EvaluateService:
    """
    Evaluation of a multi-agent workflow: answer quality, rule checks and token usage.
    Quality is scored by the Microsoft Foundry built-in evaluators (an LLM judge running in the
    Foundry project), rule checks run locally without model calls and token usage is collected
    by usage_middleware. The service is not tied to one orchestration: every workflow whose
    agents carry usage_middleware produces the same report, so workflows can be compared.
    """

    def __init__(self, judge_client, config):
        """
        Args:
            judge_client: FoundryChatClient whose model deployment is used as the LLM judge.
            config: content of config.yaml; the "evaluation" section is used.
        """
        settings = config["evaluation"]
        self.judge_client = judge_client
        self.final_answer_evaluators = settings["final_answer_evaluators"]
        self.agent_evaluators = settings["agent_evaluators"]
        self.agent_evaluation_enabled = settings["evaluate_agents"]
        self.timeout_seconds = settings["timeout_seconds"]
        self.max_answer_words = settings["max_answer_words"]
        # Add this instance to the middleware of every agent of the evaluated workflow
        self.usage_middleware = TokenUsageMiddleware()

    # ------------------------------------------------------------------ token usage
    def count_workflow_tokens(self):
        """Sum the token usage of all agents recorded since the last reset.

        Returns {"total": {...}, "per_agent": {agent_name: {...}}}. Every entry holds the
        token counts (TOKEN_FIELDS) and the number of agent calls.
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

    # ------------------------------------------------------------------ workflow run
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

    # ------------------------------------------------------------------ evaluation
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
        """Score every specialist agent separately: the steps that led to the final answer.

        The result holds one entry per agent under "agents". Tool evaluators are skipped
        by Foundry for agents without function tools.
        """
        results = await evaluate_workflow(
            workflow=workflow,
            workflow_result=workflow_result,
            evaluators=self._foundry_evals(self.agent_evaluators),
            eval_name=eval_name,
            include_overall=False,
        )
        return self._results_to_dict(results[0])

    def check_final_answer(self, final_answer, called_agents, test_case):
        """Run the rule checks on the final answer and on the agent routing; no model calls.

        The answer length is always checked. The optional test case fields expected_keywords,
        expected_answer, expected_agents and unexpected_agents add one check each.
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

        if "expected_agents" in test_case:
            not_called = [a for a in test_case["expected_agents"] if a not in called_agents]
            add_check(
                "expected_agents",
                not not_called,
                f"not called: {not_called}" if not_called else "all expected agents called",
            )

        if "unexpected_agents" in test_case:
            called = [a for a in test_case["unexpected_agents"] if a in called_agents]
            add_check("unexpected_agents", not called, f"called: {called}" if called else "none called")

        return checks

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
        record["rule_checks"] = self.check_final_answer(record["final_answer"], called_agents, test_case)

        # The evaluation name is shown in the Foundry portal
        eval_name = f"{workflow_name} - {test_case['id']}"
        record["final_answer_evaluation"] = await self._run_evaluation(
            self.evaluate_final_answer(workflow, workflow_result, eval_name)
        )
        if self.agent_evaluation_enabled:
            # The manager is not a workflow executor; without a specialist call (e.g. a refused
            # request) there is nothing to evaluate per agent
            if any(agent_name in workflow.executors for agent_name in called_agents):
                record["agent_evaluation"] = await self._run_evaluation(
                    self.evaluate_agents(workflow, workflow_result, eval_name)
                )
            else:
                record["agent_evaluation"] = {"status": "skipped", "error": None, "items": [], "agents": {}}

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
                judge_tokens += (item["judge_tokens"] or {}).get("total_tokens") or 0
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
                # Tokens used by the LLM judge, when Foundry reports them
                "judge_total": judge_tokens,
            },
        }

    def save_report(self, report, output_dir):
        """Write the report as a JSON file in output_dir and return the file path."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path(output_dir) / f"evaluation_{report['workflow_name']}_{timestamp}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as file:
            # default=str: evaluator details can hold objects that are not JSON types
            json.dump(report, file, indent=2, ensure_ascii=False, default=str)

        return output_path

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
        print("\nTokens (workflow):")
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
