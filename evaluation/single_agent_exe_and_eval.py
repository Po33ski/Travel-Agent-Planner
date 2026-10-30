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
from classes.evaluate_services import EvaluateService
from utils.agent_middleware import BlockedReplyMonitor, log_tool_calls
from utils.observability import enable_tracing

# WARNING shows tool-loop problems such as "Maximum consecutive function call errors reached"
logging.getLogger("agent_framework").setLevel(logging.WARNING)

# Must match agent_name in agents/single_agent/travel_agent_config.yaml.
# Also used in the report file name and in the Foundry evaluation names.
AGENT_NAME = "travelAgent"

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
evaluate_service = EvaluateService(
    judge_client,
    config,
    max_answer_words=evaluation_config["max_answer_words"]["single_agent"],
)


# Detects answers that the model refused or the content filter cut off
blocked_reply_monitor = BlockedReplyMonitor()


def create_agent(usage_middleware):
    # Wrap the pre-deployed Foundry project agent. One agent has all four tools:
    # the weather and hotel function tools run locally, MCP and web search run server-side.
    # usage_middleware records the token usage of the agent.
    agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name=AGENT_NAME,
        credential=credential,
        tools=[get_forecast_weather, search_hotels_google_maps],
        middleware=[usage_middleware, blocked_reply_monitor, log_tool_calls],
    )
    return agent


# Microsoft Agent Frame is built entirely on async programming.
# agent.run() is an async method, so you need to run it in an async context.
async def run_evaluation(agent, test_cases, case_records):
    """Run and evaluate every test case; the records are appended to case_records."""
    # Once per process: covers the agent and its tool calls in every test case
    await enable_tracing(agent, config, "single_agent_evaluation")
    repetitions = evaluation_config["repetitions"]

    for repetition in range(1, repetitions + 1):
        for test_case in test_cases:
            print(f"\n[EVAL] Test case '{test_case['id']}' (run {repetition}/{repetitions})")
            print(f"You: {test_case['query']}")
            print("\n[AGENT] Executing pipeline...")

            record = await evaluate_service.evaluate_agent_test_case(agent, test_case)
            record["repetition"] = repetition
            case_records.append(record)
            evaluate_service.print_case_result(record)


def main() -> int:
    try:
        test_cases = evaluate_service.load_test_cases()
        agent = create_agent(evaluate_service.usage_middleware)
    except Exception as e:
        print(f"\n❌ Execution failed: {str(e)}")
        return 1

    case_records = []
    try:
        asyncio.run(run_evaluation(agent, test_cases, case_records))
    except KeyboardInterrupt:
        print("\nInterrupted. Saving the finished test cases.")
    except Exception as e:
        print(f"\n❌ Execution failed: {str(e)}")

    if not case_records:
        return 1

    # The report is saved even after an interruption: every finished run has already cost tokens
    report = evaluate_service.create_report(AGENT_NAME, case_records)
    evaluate_service.print_summary(report["summary"])
    report_path = evaluate_service.save_report(report)
    print(f"\n✨ Evaluation report saved to: {report_path}")
    return 0


if __name__ == "__main__":
    main()
