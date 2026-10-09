import os
import asyncio
import logging
from azure.identity import DefaultAzureCredential
from agent_framework.foundry import FoundryAgent
from agent_framework.orchestrations import MagenticBuilder
from agent_framework import FunctionInvocationContext
from agent_framework.exceptions import ChatClientContentFilterException
from classes.weather_services import get_forecast_weather
from classes.hotel_services import search_for_hotels
from classes.speech_services import text_to_speech


# WARNING shows tool-loop problems such as "Maximum consecutive function call errors reached"
logging.getLogger("agent_framework").setLevel(logging.WARNING)

endpoint = os.getenv("PROJECT_ENDPOINT")
agent_name = os.getenv("AGENT_NAME")
resolved_key = os.getenv("SPEECH_KEY")
resolved_region = os.getenv("SPEECH_REGION")

credential = DefaultAzureCredential()
# =============================================================================
# AGENT INITIALIZATION
# =============================================================================

async def log_tool_calls(context: FunctionInvocationContext, call_next):
    print(f"[TOOL] -> {context.function.name}({context.arguments})")
    await call_next()
    print(f"[TOOL] <- {context.function.name}: {str(context.result)[:200]}")


def create_agents():
    # 1. Wrap the pre-deployed Foundry project agents.
    # agent_name must match agent_name in agents/*_config.yaml and the Team section of the manager prompt.
    manager_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="managerAgent",
        credential=credential,
    )

    # The manager picks the next agent from these descriptions
    rag_data_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="ragDataAgent",
        description="Retrieves attractions, restaurants, events, transportation and practical tips "
        "from the travel guide knowledge base. First choice for destination information.",
        credential=credential,
    )
    web_data_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="webDataAgent",
        description="Searches the web for travel information missing from the knowledge base, "
        "e.g. visa requirements or current events. Fallback only.",
        credential=credential,
    )
    # Function tools run locally, so the weather and hotel agents need their Python implementations
    weather_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="weatherAgent",
        description="Returns the daily weather forecast for a location and an optional date range.",
        credential=credential,
        tools=[get_forecast_weather],
        middleware=[log_tool_calls],
    )
    hotel_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="hotelAgent",
        description="Searches booking sites for hotels in a city: prices, ratings and booking links.",
        credential=credential,
        tools=[search_for_hotels],
        middleware=[log_tool_calls],
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

    # 3. Convert the workflow to an agent
    workflow_agent = workflow.as_agent(name="travelPlannerWorkflow")

    return workflow_agent


# Microsoft Agent Frame is built entirely on async programming.
# agent.run() is an async method, so you need to run it in an async context.
async def run_workflow(manager_agent, participants):
    print("Interactive workflow-agent mode started. Type 'exit' or 'quit' to stop.\n")

    # Create a server-side session to keep conversation history across turns
    # session = await agent.create_conversation()
    # print(f"Started session ID: {session}")

    while True:
        try:
            user_message_text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting interactive mode.")
            break

        if not user_message_text:
            continue
        if user_message_text.lower() in ("exit", "quit"):
            print("Exiting interactive mode.")
            break

        try:
            print("\n[WORKFLOW AGENT] Executing pipeline...")

            # A Magentic workflow handles exactly one task and then refuses further messages,
            # so a new one is built for every user message
            agent = create_workflow(manager_agent, participants)

            # Run the workflow-agent directly using standard agent execution
            result = await agent.run(user_message_text)

            # The result object from an agent run typically contains a text property
            final_text = getattr(result, "text", str(result))

            print("\n" + "=" * 50)
            print("ASSISTANT REPLY:")
            print("=" * 50)
            print(final_text)
            print("=" * 50)

            speech_result = text_to_speech(final_text)
            print("\n" + "=" * 50)
            print("SPEECH SYNTHESIS RESULT:")
            print(f"\n[SPEECH] {str(speech_result)}")
            print("=" * 50)

        except ChatClientContentFilterException as e:
            # The RAI policy blocked the input of the manager or of one of the specialists
            checks = getattr(e, "content_filter_result", None) or {}
            triggered = [name for name, check in checks.items() if check.filtered]
            reason = f" ({', '.join(triggered)})" if triggered else ""
            print(f"🛡️ Your request was blocked by the content safety policy{reason}.")
            print("Please rephrase your message and try again.")

        except Exception as e:
            print(f"❌ ERROR during workflow execution: {e}")


def main() -> int:
    try:
        manager_agent, participants = create_agents()
        asyncio.run(run_workflow(manager_agent, participants))
        return 0
    except Exception as e:
        print(f"\n❌ Execution failed: {str(e)}")
        return 1


if __name__ == "__main__":
    main()
