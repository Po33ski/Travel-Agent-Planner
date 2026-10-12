import os
import asyncio
import logging
from azure.identity import DefaultAzureCredential
from agent_framework.foundry import FoundryAgent, FoundryChatClient
from agent_framework.orchestrations import MagenticBuilder
from agent_framework import FunctionInvocationContext
from azure.keyvault.secrets import SecretClient
import yaml
from classes.weather_services import get_forecast_weather
from classes.hotel_services import search_for_hotels
from classes.secret_manager_services import SecretManager
from classes.evaluate_services import EvaluateService

# WARNING shows tool-loop problems such as "Maximum consecutive function call errors reached"
logging.getLogger("agent_framework").setLevel(logging.WARNING)

# Name of the evaluated workflow: used in the report file name and in the Foundry evaluation names.
# Give every workflow its own name to compare their reports.
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
evaluate_service = EvaluateService(judge_client, config)


async def log_tool_calls(context: FunctionInvocationContext, call_next):
    print(f"[TOOL] -> {context.function.name}({context.arguments})")
    await call_next()
    print(f"[TOOL] <- {context.function.name}: {str(context.result)[:200]}")


def create_agents(usage_middleware):
    # 1. Wrap the pre-deployed Foundry project agents.
    # agent_name must match agent_name in agents/*_config.yaml and the Team section of the manager prompt.
    # usage_middleware records the token usage of every agent, the manager included.
    manager_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="managerAgent",
        credential=credential,
        middleware=[usage_middleware],
    )

    # The manager picks the next agent from these descriptions
    rag_data_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="ragDataAgent",
        description="Retrieves attractions, restaurants, events, transportation and practical tips "
        "from the travel guide knowledge base. First choice for destination information.",
        credential=credential,
        middleware=[usage_middleware],
    )
    web_data_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="webDataAgent",
        description="Searches the web for travel information missing from the knowledge base, "
        "e.g. visa requirements or current events. Fallback only.",
        credential=credential,
        middleware=[usage_middleware],
    )
    # Function tools run locally, so the weather and hotel agents need their Python implementations
    weather_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="weatherAgent",
        description="Returns the daily weather forecast for a location and an optional date range.",
        credential=credential,
        tools=[get_forecast_weather],
        middleware=[usage_middleware, log_tool_calls],
    )
    hotel_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="hotelAgent",
        description="Searches booking sites for hotels in a city: prices, ratings and booking links.",
        credential=credential,
        tools=[search_for_hotels],
        middleware=[usage_middleware, log_tool_calls],
    )

    return manager_agent, [rag_data_agent, web_data_agent, weather_agent, hotel_agent]


def create_workflow(manager_agent, participants):
    # 2. Build the workflow
    workflow = MagenticBuilder(
        manager_agent=manager_agent,
        participants=participants,
        # One round = one manager check plus one agent turn. When the limit is hit the workflow
        # terminates WITHOUT a final answer, so leave room for all four agents and a retry.
        max_round_count=10,
        max_stall_count=3,  # Consecutive rounds without progress before the manager resets and replans
    ).build()

    return workflow


def load_test_cases(path):
    """Read the test prompts (see inputs/evaluation_prompts.yaml for the format)."""
    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)["test_cases"]


def print_case_result(record):
    """Print the outcome of one test case."""
    if "error" in record:
        print(f"❌ Workflow run failed: {record['error']}")
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
    for check in record["rule_checks"]:
        print(f"[EVAL] {'✅' if check['passed'] else '❌'} {check['name']}: {check['details']}")

    final_answer_evaluation = record["final_answer_evaluation"]
    print(
        f"[EVAL] Final answer (LLM judge): {final_answer_evaluation['status']}, "
        f"{final_answer_evaluation.get('passed', 0)} passed, {final_answer_evaluation.get('failed', 0)} failed"
    )


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
            print_case_result(record)


def main() -> int:
    try:
        test_cases = load_test_cases(evaluation_config["test_cases_path"])
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
    report_path = evaluate_service.save_report(report, evaluation_config["output_dir"])
    print(f"\n✨ Evaluation report saved to: {report_path}")
    return 0


if __name__ == "__main__":
    main()
