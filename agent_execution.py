import os
import asyncio
import logging
from azure.identity import DefaultAzureCredential
from azure.core.credentials import AzureKeyCredential
from agent_framework.foundry import FoundryAgent
from agent_framework.orchestrations import MagenticBuilder
from agent_framework.exceptions import ChatClientContentFilterException
from azure.keyvault.secrets import SecretClient
from azure.cosmos import CosmosClient
from azure.ai.textanalytics import TextAnalyticsClient
import yaml
from classes.weather_services import get_forecast_weather
from classes.google_services import search_hotels_google_maps
from classes.speech_services import text_to_speech
from classes.secret_manager_services import SecretManager
from classes.cosmos_memory_services import CosmosMemory
from classes.azure_nlp_services import AzureNLPService
from utils.agent_middleware import (
    BlockedReplyMonitor,
    drop_foreign_tool_contents,
    log_agent_replies,
    log_manager_decisions,
    log_tool_calls,
)
from utils.utils import add_todays_date

# WARNING shows tool-loop problems such as "Maximum consecutive function call errors reached"
logging.getLogger("agent_framework").setLevel(logging.WARNING)

agent_name = os.getenv("AGENT_NAME")

# User requests and final answers are collected as fine-tuning examples in the user's
# conversation_history in Cosmos DB.
# Short stand-in for the agents' full system prompts: a fine-tuned model should learn
# the detailed rules from the examples instead of reading them in every prompt
FINE_TUNING_SYSTEM_PROMPT = (
    "You are a travel assistant. You plan trips and answer travel questions using a travel guide, "
    "web search, weather forecasts and hotel search, and you never invent facts."
)

# =============================================================================
# AGENT INITIALIZATION
# =============================================================================
# =============================================================================
# CONFIGURATION
# =============================================================================

key_vault_url = os.getenv("KEY_VAULT_URL")
region = os.getenv("REGION")
# The user name is asked for at start-up; the role comes from launch.json for now
user_role = os.getenv("USER_ROLE")
with open("config.yaml", "r") as file:
    config = yaml.safe_load(file)
# =============================================================================
# AUTHENTICATION
# =============================================================================

credential = DefaultAzureCredential()
secret_client = SecretClient(vault_url=key_vault_url, credential=credential)
secret_manager = SecretManager(secret_client)

language_client = TextAnalyticsClient(
    endpoint=secret_manager.get_secret("LANGUAGE-ENDPOINT"),
    credential=AzureKeyCredential(secret_manager.get_secret("LANGUAGE-KEY")),
)
# Recognises user preferences (for now the language) in the user's messages
nlp_service = AzureNLPService(language_client, config)
cosmos_db_client = CosmosClient(
    url=secret_manager.get_secret("COSMOSDB-ENDPOINT"),
    credential=secret_manager.get_secret("COSMOSDB-PRIMARY-KEY"),
)
database_client = cosmos_db_client.get_database_client(
    secret_manager.get_secret("COSMOSDB-DATABASE-NAME")
)
if database_client.read():
    print("Successfully connected to Cosmos DB database")
container_client = database_client.get_container_client(
    secret_manager.get_secret("COSMOSDB-CONTAINER-NAME")
)
if container_client.read():
    print("Successfully connected to Cosmos DB container")
# Secret name = setting name with '-' instead of '_' (see resource_deployment.bicep)
endpoint = secret_manager.get_secret("PROJECT-ENDPOINT")

# Instantiate CosmosMemory class
cosmos_memory = CosmosMemory(
    cosmos_client=cosmos_db_client,
    database_client=database_client,
    container_client=container_client,
    config=config,
)


# Detects answers that the model refused or the content filter cut off
blocked_reply_monitor = BlockedReplyMonitor()


def create_agents():
    # 1. Wrap the pre-deployed Foundry project agents.
    # agent_name must match agent_name in agents/*_config.yaml and the Team section of the manager prompt.
    # drop_foreign_tool_contents: the agents get each other's text answers, not the raw tool calls.
    manager_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="managerAgent",
        credential=credential,
        middleware=[drop_foreign_tool_contents, blocked_reply_monitor, log_manager_decisions],
    )

    # The manager picks the next agent from these descriptions
    rag_data_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="ragDataAgent",
        description="Retrieves attractions, restaurants, events, transportation and practical tips "
        "from the travel guide knowledge base. First choice for destination information.",
        credential=credential,
        middleware=[drop_foreign_tool_contents, blocked_reply_monitor, log_agent_replies],
    )
    web_data_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="webDataAgent",
        description="Searches the web for general country information, e.g. visa requirements, and for "
        "travel information missing from the knowledge base, e.g. current events.",
        credential=credential,
        middleware=[drop_foreign_tool_contents, blocked_reply_monitor, log_agent_replies],
    )
    # Function tools run locally, so the weather and hotel agents need their Python implementations
    weather_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="weatherAgent",
        description="Returns the daily weather forecast for a location and an optional date range.",
        credential=credential,
        tools=[get_forecast_weather],
        middleware=[drop_foreign_tool_contents, blocked_reply_monitor, log_agent_replies, log_tool_calls],
    )
    hotel_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="hotelAgent",
        description="Searches Google Maps for hotels in a city and picks the ones that best match the user's "
        "preferences: ratings, reviews, amenities and links. It has no room prices.",
        credential=credential,
        tools=[search_hotels_google_maps],
        middleware=[drop_foreign_tool_contents, blocked_reply_monitor, log_agent_replies, log_tool_calls],
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

    # 3. Convert the workflow to an agent
    workflow_agent = workflow.as_agent(name="travelPlannerWorkflow")

    return workflow_agent


def save_response_and_preferences(user_id: str, user_text: str, assistant_text: str) -> None:
    """Append one user request and the workflow's final answer to the user's conversation_history
    and save the preferences Azure AI Language recognises in the request (for now the language).

    Each record uses the chat fine-tuning format: a system, a user and an assistant message.
    """
    preferences = nlp_service.extract_preferences(user_text)

    record = {
        "messages": [
            {"role": "system", "content": FINE_TUNING_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
    }
    cosmos_memory.add_conversation(user_id, record, preferences)


def ask_user_name() -> str | None:
    """Ask for the user name until a non-empty one is given; None when the user quits."""
    while True:
        try:
            user_name = input("User name: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if user_name:
            return user_name
        print("The user name is required.")


# Microsoft Agent Frame is built entirely on async programming.
# agent.run() is an async method, so you need to run it in an async context.
async def run_workflow(manager_agent, participants, user_id: str):
    print("Interactive workflow-agent mode started. Type 'exit' or 'quit' to stop.\n")

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
            blocked_reply_monitor.reset()
            # The agents do not know the current date, so it is put in front of the message.
            # The conversation history below keeps the message as the user wrote it.
            result = await agent.run(add_todays_date(user_message_text))

            # The result object from an agent run typically contains a text property
            final_text = getattr(result, "text", str(result))

            print("\n" + "=" * 50)
            print("ASSISTANT REPLY:")
            print("=" * 50)
            print(final_text)
            print("=" * 50)

            # The manager writes the final answer. A refused or filtered answer is not a valid
            # fine-tuning example and is not read out.
            if blocked_reply_monitor.is_last_reply_blocked(manager_agent.name):
                print("🛡️ The final answer was refused or blocked (see the [GUARDRAIL] lines above).")
                print("It was not saved to the conversation history and not read out.")
                continue

            save_response_and_preferences(user_id, user_message_text, final_text)

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
    # The user has to give a name before the agents start
    user_name = ask_user_name()
    if user_name is None:
        print("\nExiting.")
        return 1

    try:
        # Known name: the stored user id; first visit: a new id and profile
        user_id = cosmos_memory.get_or_create_user(user_name, user_role)
        manager_agent, participants = create_agents()
        asyncio.run(run_workflow(manager_agent, participants, user_id))
        return 0
    except Exception as e:
        print(f"\n❌ Execution failed: {str(e)}")
        return 1


if __name__ == "__main__":
    main()
