import os
import sys
import asyncio
import logging
from pathlib import Path

# This script lives in execution/; add the project root so the classes and utils packages can be imported,
# and work from the root, because the relative paths (config.yaml) are relative to it
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from azure.identity import DefaultAzureCredential
from azure.core.credentials import AzureKeyCredential
from agent_framework.foundry import FoundryAgent, FoundryChatClient
from agent_framework.exceptions import ChatClientContentFilterException
from azure.keyvault.secrets import SecretClient
from azure.cosmos import CosmosClient
from azure.ai.textanalytics import TextAnalyticsClient
import yaml
from classes.weather_services import get_forecast_weather
from classes.google_services import search_hotels_google_maps
from classes.human_review_services import create_ask_user_tool
from classes.secret_manager_services import SecretManager
from classes.cosmos_memory_services import CosmosMemory
from classes.azure_nlp_services import AzureNLPService
from classes.travel_history_provider import TravelHistoryProvider
from classes.scope_guard_services import ScopeGuard
from utils.agent_middleware import (
    BlockedReplyMonitor,
    add_todays_date_to_task,
    log_agent_replies,
    log_delegations,
    log_tool_calls,
)
from utils.observability import enable_tracing
from utils.utils import add_todays_date, ask_user_email, ask_user_name

# WARNING shows tool-loop problems such as "Maximum consecutive function call errors reached"
logging.getLogger("agent_framework").setLevel(logging.WARNING)

# The whole chat is saved as one multi-turn fine-tuning example in the user's conversation_history
# in Cosmos DB. Short stand-in for the concierge's full system prompt, different from the Magentic
# chat's one, so the two kinds of examples can be told apart.
FINE_TUNING_SYSTEM_PROMPT = (
    "You are a travel concierge. You plan trips with the user step by step using a travel guide, "
    "web search, weather forecasts and hotel search, you suggest the next steps, and you never invent facts."
)

# Azure AI Language reads at most 5,120 characters per document in language detection and key phrase
# extraction, and 125,000 in summarization
LANGUAGE_DOCUMENT_CHARS = 5000
SUMMARY_DOCUMENT_CHARS = 120000

# =============================================================================
# CONFIGURATION
# =============================================================================

key_vault_url = os.getenv("KEY_VAULT_URL")
# The user name is asked for at start-up; the role comes from launch.json for now
user_role = os.getenv("USER_ROLE")
with open("config.yaml", "r") as file:
    config = yaml.safe_load(file)
memory_config = config["memory"]

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
# Recognises user preferences (for now the language) and summarises the chat for the travel history
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

cosmos_memory = CosmosMemory(
    cosmos_client=cosmos_db_client,
    database_client=database_client,
    container_client=container_client,
    config=config,
)

# Detects answers that the model refused or the content filter cut off
blocked_reply_monitor = BlockedReplyMonitor()

# A small model checks every message before the concierge runs; the concierge never sees an out-of-scope message
scope_guard = ScopeGuard(
    FoundryChatClient(
        project_endpoint=endpoint,
        model=secret_manager.get_secret(config["scope_guard"]["model_secret"]),
        credential=credential,
    ),
    config,
)


def create_agents(travel_history: TravelHistoryProvider) -> FoundryAgent:
    """Wrap the pre-deployed Foundry agents: the concierge with its four subagents as tools (agent as a tool)."""
    # 1. Subagents. agent_name must match agent_name in agents/concierge/*_subagent_config.yaml and the
    # tool names declared in agents/concierge/concierge_agent_deployment.py.
    # Each subagent has its data tool and ask_user, the only way it talks to the user; its system
    # prompt limits the questions to its topic. The knowledge base (MCP) and web search run server-side,
    # so ragDataSubagent and webDataSubagent get only ask_user here.
    # A subagent sees only the task the concierge wrote, never the chat, so drop_foreign_tool_contents
    # (needed in the Magentic group chat) is not needed; add_todays_date_to_task gives it the date line.
    subagent_middleware = [add_todays_date_to_task, blocked_reply_monitor, log_agent_replies, log_tool_calls]

    rag_data_subagent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="ragDataSubagent",
        description="Travel guide knowledge base: attractions, restaurants, events, transportation and practical tips.",
        credential=credential,
        tools=[create_ask_user_tool("travel guide")],
        middleware=subagent_middleware,
    )
    web_data_subagent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="webDataSubagent",
        description="Web search: country information, entry rules such as visas, and current travel information.",
        credential=credential,
        tools=[create_ask_user_tool("web search")],
        middleware=subagent_middleware,
    )
    weather_subagent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="weatherSubagent",
        description="Daily weather forecast for a location and an optional date range (next 15 days).",
        credential=credential,
        tools=[get_forecast_weather, create_ask_user_tool("weather")],
        middleware=subagent_middleware,
    )
    hotel_subagent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="hotelSubagent",
        description="Hotel search on Google Maps: picks the hotels that best match the user's wishes.",
        credential=credential,
        tools=[search_hotels_google_maps, create_ask_user_tool("hotel")],
        middleware=subagent_middleware,
    )
    subagents = [rag_data_subagent, web_data_subagent, weather_subagent, hotel_subagent]

    # 2. The concierge. as_tool() turns each subagent into a function tool named after the agent, with one
    # string argument "task"; the concierge's server-side definition declares the same four tools, and
    # FoundryAgent runs the subagent when the service requests the call. Without propagate_session every
    # call is a new, separate conversation of the subagent.
    # Only the concierge gets the user's travel history (once per session: the session keeps it).
    concierge_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="conciergeAgent",
        credential=credential,
        tools=[subagent.as_tool() for subagent in subagents],
        middleware=[blocked_reply_monitor, log_delegations],
        context_providers=[travel_history],
    )

    return concierge_agent


def print_reply(text: str) -> None:
    print("\n" + "=" * 50)
    print("CONCIERGE:")
    print("=" * 50)
    print(text)
    print("=" * 50)


def save_conversation(user_id: str, turns: list[tuple[str, str]]) -> None:
    """Save the chat as one multi-turn fine-tuning example (system message, then the user's messages and
    the concierge's replies in turn) and the preferences Azure AI Language recognises in the user's messages.
    """
    user_text = "\n".join(user_message for user_message, _ in turns)
    preferences = nlp_service.extract_preferences(user_text[:LANGUAGE_DOCUMENT_CHARS])

    messages = [{"role": "system", "content": FINE_TUNING_SYSTEM_PROMPT}]
    for user_message, reply in turns:
        messages.append({"role": "user", "content": user_message})
        messages.append({"role": "assistant", "content": reply})
    cosmos_memory.add_conversation(user_id, {"messages": messages}, preferences)


def propose_trip_memory(user_id: str, turns: list[tuple[str, str]]) -> None:
    """Summarise the chat and extract keywords from the user's messages; save both only when the user accepts.

    The summary covers the whole chat, the keywords only the user's messages, so they describe what the
    user asked for and not every place the concierge suggested.
    """
    user_text = "\n".join(user_message for user_message, _ in turns)[:LANGUAGE_DOCUMENT_CHARS]
    chat_text = "\n\n".join(f"{user_message}\n\n{reply}" for user_message, reply in turns)[:SUMMARY_DOCUMENT_CHARS]

    language = nlp_service.detect_language(user_text)
    summary = nlp_service.summarize(chat_text, language, memory_config["summary_sentence_count"])
    keywords = nlp_service.extract_key_phrases(user_text, language)[: memory_config["max_keywords_per_answer"]]
    if not summary and not keywords:
        return

    print("\n" + "=" * 50)
    print("TRAVEL HISTORY:")
    print(f"Summary: {summary or '(none)'}")
    print(f"Keywords: {', '.join(keywords) or '(none)'}")
    print("=" * 50)
    try:
        answer = input("Save them to your profile for your next trips? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in ("y", "yes", "t", "tak"):
        print("[LONG-TERM] Not saved.")
        return

    cosmos_memory.add_trip_memory(user_id, summary, keywords)


# Microsoft Agent Framework is built entirely on async programming: agent.run() is an async method.
async def run_chat(concierge_agent: FoundryAgent) -> list[tuple[str, str]]:
    """Run the console chat with the concierge until the user quits.

    Returns the turns the concierge answered, as (user message, reply), for the history in Cosmos DB.
    """
    # Once per process: covers the concierge, the subagents and their tool calls
    await enable_tracing(concierge_agent, config, "concierge_chat")
    print("Concierge chat started. Type 'exit' or 'quit' to stop.\n")

    # One session for the whole chat: the service stores the conversation and the session points to it,
    # so the concierge remembers the earlier turns, its subagent calls and their results. The session is
    # updated only after a complete turn, so a turn that fails leaves it at the previous one.
    session = concierge_agent.create_session()
    turns: list[tuple[str, str]] = []
    last_reply: str | None = None

    while True:
        try:
            user_message_text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting the chat.")
            break

        if not user_message_text:
            continue
        if user_message_text.lower() in ("exit", "quit"):
            print("Exiting the chat.")
            break

        try:
            blocked_reply_monitor.reset()

            # The scope guard decides first. It gets the end of the concierge's previous reply, so a short
            # answer to it ("yes", "three days") counts as part of the travel conversation.
            scope = await scope_guard.check(user_message_text, previous_reply=last_reply)
            scope.print_line()
            if not scope.in_scope:
                # The concierge never sees the refused message, so it stays out of the conversation
                print_reply(scope_guard.refusal)
                continue

            print("\n[CONCIERGE] Working on it...")
            # The agents do not know the current date, so it is put in front of the message.
            # The saved conversation keeps the message as the user wrote it.
            response = await concierge_agent.run(add_todays_date(user_message_text), session=session)
            reply = response.text
            if not reply:
                print("⚠️ The concierge returned no answer. Please try again.")
                continue

            print_reply(reply)
            last_reply = reply

            # A refused or filtered reply is not a valid fine-tuning example
            if blocked_reply_monitor.is_last_reply_blocked(concierge_agent.name):
                print("🛡️ The reply was refused or blocked (see the [GUARDRAIL] lines above).")
                print("It will not be saved to the conversation history.")
                continue

            turns.append((user_message_text, reply))

        except ChatClientContentFilterException as e:
            # The content filter blocked the input of the scope guard, the concierge or one of the subagents
            checks = getattr(e, "content_filter_result", None) or {}
            triggered = [name for name, check in checks.items() if check.filtered]
            reason = f" ({', '.join(triggered)})" if triggered else ""
            print(f"🛡️ Your request was blocked by the content safety policy{reason}.")
            print("Please rephrase your message and try again.")

        except Exception as e:
            print(f"❌ ERROR during the concierge run: {e}")

    return turns


def main() -> int:
    # The user has to give a name before the agents start
    user_name = ask_user_name()
    if user_name is None:
        print("\nExiting.")
        return 1

    try:
        # Known name: the stored user id; first visit: a new id and profile
        user_id = cosmos_memory.get_or_create_user(user_name, user_role)

        # The e-mail address is only stored in the profile; the agents never see it
        stored_email = (cosmos_memory.get_user_profile(user_id) or {}).get("email_address")
        email = ask_user_email(stored_email)
        if email is None:
            print("\nExiting.")
            return 1
        if email != stored_email:
            cosmos_memory.update_preference(user_id, "email_address", email)

        travel_history = TravelHistoryProvider(cosmos_memory, user_id, once_per_session=True)
        concierge_agent = create_agents(travel_history)
        turns = asyncio.run(run_chat(concierge_agent))

        # The chat is saved once, at the end: a single turn of it is rarely a complete request
        if turns:
            save_conversation(user_id, turns)
            propose_trip_memory(user_id, turns)
        return 0
    except Exception as e:
        print(f"\n❌ Execution failed: {str(e)}")
        return 1


if __name__ == "__main__":
    main()
