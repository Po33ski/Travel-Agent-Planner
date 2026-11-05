import os
import sys
import asyncio
import logging
from pathlib import Path

# This script lives in evaluation/; add the project root so the classes and utils packages can be imported,
# and work from the root, because the relative paths (config.yaml, inputs/, outputs/) are relative to it
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from azure.identity import DefaultAzureCredential
from agent_framework.foundry import FoundryAgent, FoundryChatClient
from azure.keyvault.secrets import SecretClient
import yaml
from classes.weather_services import get_forecast_weather
from classes.google_services import search_hotels_google_maps
from classes.secret_manager_services import SecretManager
from classes.evaluate_services import ConciergeCallRecorder, EvaluateService
from classes.scope_guard_services import ScopeGuard
from classes.user_simulator_services import UserSimulator
from utils.agent_middleware import (
    BlockedReplyMonitor,
    add_todays_date_to_task,
    log_agent_replies,
    log_delegations,
    log_tool_calls,
)
from utils.observability import enable_tracing

# WARNING shows tool-loop problems such as "Maximum consecutive function call errors reached"
logging.getLogger("agent_framework").setLevel(logging.WARNING)

# Name of the evaluated set-up: used in the report file name and in the Foundry evaluation names
WORKFLOW_NAME = "conciergeWorkflow"

# =============================================================================
# CONFIGURATION
# =============================================================================

key_vault_url = os.getenv("KEY_VAULT_URL")
with open("config.yaml", "r") as file:
    config = yaml.safe_load(file)
evaluation_config = config["evaluation"]

# =============================================================================
# AUTHENTICATION
# =============================================================================

credential = DefaultAzureCredential()
secret_client = SecretClient(vault_url=key_vault_url, credential=credential)
secret_manager = SecretManager(secret_client)

# Secret name = setting name with '-' instead of '_' (see resource_deployment.bicep)
endpoint = secret_manager.get_secret("PROJECT-ENDPOINT")
judge_model_deployment_name = secret_manager.get_secret(evaluation_config["judge_model_secret"])

# The Foundry evaluators run in the project and use this deployment as the LLM judge
judge_client = FoundryChatClient(
    project_endpoint=endpoint,
    model=judge_model_deployment_name,
    credential=credential,
)
# As in the chat, the scope guard checks every message of a test conversation before the concierge runs
scope_guard = ScopeGuard(
    FoundryChatClient(
        project_endpoint=endpoint,
        model=secret_manager.get_secret(config["scope_guard"]["model_secret"]),
        credential=credential,
    ),
    config,
)
evaluate_service = EvaluateService(
    judge_client,
    config,
    max_answer_words=evaluation_config["max_answer_words"]["concierge"],
    scope_guard=scope_guard,
)
# Plays the traveller in the test cases with a "user" field and answers the subagents' questions there;
# in the scripted cases its ask_user tools skip every question
user_simulator = UserSimulator(
    FoundryChatClient(
        project_endpoint=endpoint,
        model=secret_manager.get_secret(config["user_simulator"]["model_secret"]),
        credential=credential,
    ),
    config,
)


# Detects answers that the model refused or the content filter cut off
blocked_reply_monitor = BlockedReplyMonitor()
# Records every subagent call (task and response), so each call can be scored with the subagent's evaluators
call_recorder = ConciergeCallRecorder()


def create_agents(usage_middleware) -> FoundryAgent:
    """Wrap the pre-deployed Foundry agents: the concierge with its four subagents as tools (agent as a tool).

    The same wiring as execution/concierge_agent.py, except: the subagents get the simulated user's
    ask_user instead of the console one (it answers in the simulated test cases and skips the questions in
    the scripted ones), usage_middleware and call_recorder are added, and the concierge has no
    UserMemoryProvider (the evaluation has no user profile).
    """
    # 1. Subagents. agent_name must match agent_name in agents/concierge/*_subagent_config.yaml and the
    # tool names declared in agents/concierge/concierge_agent_deployment.py.
    # usage_middleware records the tokens of every agent, call_recorder the subagent calls. call_recorder
    # comes after add_todays_date_to_task, so it records the task with the date line the subagent saw.
    subagent_middleware = [
        add_todays_date_to_task,
        usage_middleware,
        call_recorder,
        blocked_reply_monitor,
        log_agent_replies,
        log_tool_calls,
    ]

    rag_data_subagent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="ragDataSubagent",
        description="Travel guide knowledge base: attractions, restaurants, events, transportation and practical tips.",
        credential=credential,
        tools=[user_simulator.create_ask_user_tool("travel guide")],
        middleware=subagent_middleware,
    )
    web_data_subagent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="webDataSubagent",
        description="Web search: country information, entry rules such as visas, and current travel information.",
        credential=credential,
        tools=[user_simulator.create_ask_user_tool("web search")],
        middleware=subagent_middleware,
    )
    weather_subagent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="weatherSubagent",
        description="Daily weather forecast for a location and an optional date range (next 15 days).",
        credential=credential,
        tools=[get_forecast_weather, user_simulator.create_ask_user_tool("weather")],
        middleware=subagent_middleware,
    )
    hotel_subagent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="hotelSubagent",
        description="Hotel search on Google Maps: picks the hotels that best match the user's wishes.",
        credential=credential,
        tools=[search_hotels_google_maps, user_simulator.create_ask_user_tool("hotel")],
        middleware=subagent_middleware,
    )
    subagents = [rag_data_subagent, web_data_subagent, weather_subagent, hotel_subagent]

    # 2. The concierge: as_tool() turns each subagent into a function tool named after the agent, with one
    # string argument "task", matching the tools declared in its server-side definition.
    concierge_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="conciergeAgent",
        credential=credential,
        tools=[subagent.as_tool() for subagent in subagents],
        middleware=[usage_middleware, blocked_reply_monitor, log_delegations],
    )

    return concierge_agent


def print_subagent_calls(record) -> None:
    """Print how often each subagent was called and which tools it used (ask_user = a question to the user)."""
    calls = record.get("subagent_calls")
    if calls is None:
        return
    if not calls:
        print("[EVAL] Subagents called: none")
        return
    for agent_name, activity in calls.items():
        print(f"[EVAL] Subagent {agent_name}: {activity['calls']} call(s), tools: {activity['tool_calls'] or 'none'}")


def print_user_simulation(record) -> None:
    """Print how a simulated conversation ended and what the simulated user cost (not counted for the concierge)."""
    simulation = record.get("user_simulation")
    if simulation is None:
        return
    tokens = simulation["token_usage"]
    print(
        f"[EVAL] Simulated user: {simulation['end']} after {simulation['user_messages']} message(s) "
        f"(limit {simulation['max_turns']}), {simulation['questions_answered']} question(s) answered, "
        f"{tokens['total_token_count']} tokens in {tokens['calls']} calls, {simulation['duration_seconds']} s"
    )


# Microsoft Agent Framework is built entirely on async programming.
# agent.run() is an async method, so you need to run it in an async context.
async def run_evaluation(concierge_agent, test_cases, case_records):
    """Run and evaluate every test conversation; the records are appended to case_records."""
    # Once per process: covers the concierge, the subagents and their tool calls in every test case
    await enable_tracing(concierge_agent, config, "concierge_evaluation")
    repetitions = evaluation_config["repetitions"]

    for repetition in range(1, repetitions + 1):
        for test_case in test_cases:
            print(f"\n[EVAL] Test case '{test_case['id']}' (run {repetition}/{repetitions})")
            print(f"You: {test_case['query']}")
            print("\n[CONCIERGE] Working on it...")

            # Every test case is a new conversation (session) with the concierge
            record = await evaluate_service.evaluate_concierge_test_case(
                concierge_agent, call_recorder, test_case, WORKFLOW_NAME, user_simulator=user_simulator
            )
            record["repetition"] = repetition
            case_records.append(record)
            evaluate_service.print_case_result(record)
            print_subagent_calls(record)
            print_user_simulation(record)


def main() -> int:
    try:
        test_cases = evaluate_service.load_concierge_test_cases()
        concierge_agent = create_agents(evaluate_service.usage_middleware)
    except Exception as e:
        print(f"\n❌ Execution failed: {str(e)}")
        return 1

    case_records = []
    try:
        asyncio.run(run_evaluation(concierge_agent, test_cases, case_records))
    except KeyboardInterrupt:
        print("\nInterrupted. Saving the finished test cases.")
    except Exception as e:
        print(f"\n❌ Execution failed: {str(e)}")

    if not case_records:
        return 1

    # The report is saved even after an interruption: every finished run has already cost tokens
    report = evaluate_service.create_report(WORKFLOW_NAME, case_records)
    evaluate_service.print_summary(report["summary"])
    # The simulated conversations, with the simulated user's tokens (not part of the concierge's tokens)
    simulation_summary = evaluate_service.summarize_concierge_simulation(case_records)
    if simulation_summary is not None:
        report["summary"]["user_simulation"] = simulation_summary
        print(
            f"Simulated conversations: {simulation_summary['conversations']}, goal reached: "
            f"{simulation_summary['goal_reached']}, ends: {simulation_summary['ends']}, average user messages: "
            f"{simulation_summary['average_user_messages']}, simulated user's tokens: "
            f"{simulation_summary['tokens'].get('total_token_count', 0)}"
        )
    report_path = evaluate_service.save_report(report)
    print(f"\n✨ Evaluation report saved to: {report_path}")
    return 0


if __name__ == "__main__":
    main()
