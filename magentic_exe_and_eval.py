import os
import asyncio
import logging
from azure.identity import DefaultAzureCredential
from agent_framework.foundry import FoundryAgent, FoundryChatClient
from agent_framework.orchestrations import MagenticBuilder
from azure.keyvault.secrets import SecretClient
import yaml
from classes.weather_services import get_forecast_weather
from classes.google_services import search_hotels_google_maps
from classes.secret_manager_services import SecretManager
from classes.evaluate_services import EvaluateService
from utils.agent_middleware import (
    BlockedReplyMonitor,
    drop_foreign_tool_contents,
    log_agent_replies,
    log_manager_decisions,
    log_tool_calls,
)

# WARNING shows tool-loop problems such as "Maximum consecutive function call errors reached"
logging.getLogger("agent_framework").setLevel(logging.WARNING)

# Name of the evaluated workflow: used in the report file name and in the Foundry evaluation names
WORKFLOW_NAME = "travelPlannerWorkflow"

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
    max_answer_words=evaluation_config["max_answer_words"]["magentic"],
)


# Detects answers that the model refused or the content filter cut off
blocked_reply_monitor = BlockedReplyMonitor()


def create_agents(usage_middleware):
    # 1. Wrap the pre-deployed Foundry project agents.
    # agent_name must match agent_name in agents/*_config.yaml and the Team section of the manager prompt.
    # usage_middleware records the token usage of every agent, the manager included.
    # drop_foreign_tool_contents: the agents get each other's text answers, not the raw tool calls.
    manager_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="managerAgent",
        credential=credential,
        middleware=[drop_foreign_tool_contents, usage_middleware, blocked_reply_monitor, log_manager_decisions],
    )

    # The manager picks the next agent from these descriptions
    rag_data_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="ragDataAgent",
        description="Retrieves attractions, restaurants, events, transportation and practical tips "
        "from the travel guide knowledge base. First choice for destination information.",
        credential=credential,
        middleware=[drop_foreign_tool_contents, usage_middleware, blocked_reply_monitor, log_agent_replies],
    )
    web_data_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="webDataAgent",
        description="Searches the web for general country information, e.g. visa requirements, and for "
        "travel information missing from the knowledge base, e.g. current events.",
        credential=credential,
        middleware=[drop_foreign_tool_contents, usage_middleware, blocked_reply_monitor, log_agent_replies],
    )
    # Function tools run locally, so the weather and hotel agents need their Python implementations
    weather_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="weatherAgent",
        description="Returns the daily weather forecast for a location and an optional date range.",
        credential=credential,
        tools=[get_forecast_weather],
        middleware=[drop_foreign_tool_contents, usage_middleware, blocked_reply_monitor, log_agent_replies, log_tool_calls],
    )
    hotel_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="hotelAgent",
        description="Searches Google Maps for hotels in a city and picks the ones that best match the user's "
        "preferences: ratings, reviews, amenities and links. It has no room prices.",
        credential=credential,
        tools=[search_hotels_google_maps],
        middleware=[drop_foreign_tool_contents, usage_middleware, blocked_reply_monitor, log_agent_replies, log_tool_calls],
    )

    return manager_agent, [rag_data_agent, web_data_agent, weather_agent, hotel_agent]


def create_workflow(manager_agent, participants):
    # 2. Build the workflow
    workflow = MagenticBuilder(
        manager_agent=manager_agent,
        participants=participants,
        # One round = one manager check plus one agent turn. When the limit is hit the workflow
        # terminates WITHOUT a final answer, so leave room for all four agents and a retry.
        max_round_count=20,
        max_stall_count=3,  # Consecutive rounds without progress before the manager resets and replans
        # A reset clears the shared conversation and starts again from the first agent. After the
        # second reset the workflow terminates instead of repeating the same rounds up to the round limit.
        max_reset_count=2,
    ).build()

    return workflow


# Microsoft Agent Frame is built entirely on async programming.
# workflow.run() is an async method, so you need to run it in an async context.
async def run_evaluation(manager_agent, participants, test_cases, case_records):
    """Run and evaluate every test case; the records are appended to case_records."""
    repetitions = evaluation_config["repetitions"]

    for repetition in range(1, repetitions + 1):
        for test_case in test_cases:
            print(f"\n[EVAL] Test case '{test_case['id']}' (run {repetition}/{repetitions})")
            print(f"You: {test_case['query']}")
            print("\n[WORKFLOW] Executing pipeline...")

            # A Magentic workflow handles exactly one task and then refuses further messages,
            # so a new one is built for every test case
            workflow = create_workflow(manager_agent, participants)

            record = await evaluate_service.evaluate_test_case(workflow, test_case, WORKFLOW_NAME)
            record["repetition"] = repetition
            case_records.append(record)
            evaluate_service.print_case_result(record)


def main() -> int:
    try:
        test_cases = evaluate_service.load_test_cases()
        manager_agent, participants = create_agents(evaluate_service.usage_middleware)
    except Exception as e:
        print(f"\n❌ Execution failed: {str(e)}")
        return 1

    case_records = []
    try:
        asyncio.run(run_evaluation(manager_agent, participants, test_cases, case_records))
    except KeyboardInterrupt:
        print("\nInterrupted. Saving the finished test cases.")
    except Exception as e:
        print(f"\n❌ Execution failed: {str(e)}")

    if not case_records:
        return 1

    # The report is saved even after an interruption: every finished run has already cost tokens
    report = evaluate_service.create_report(WORKFLOW_NAME, case_records)
    evaluate_service.print_summary(report["summary"])
    report_path = evaluate_service.save_report(report)
    print(f"\n✨ Evaluation report saved to: {report_path}")
    return 0


if __name__ == "__main__":
    main()
