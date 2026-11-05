import os
import sys
import asyncio
import logging
from datetime import date
from pathlib import Path

# This script lives in execution/; add the project root so the classes and utils packages can be imported,
# and work from the root, because the relative paths (config.yaml) are relative to it
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from azure.identity import DefaultAzureCredential
from agent_framework.foundry import FoundryAgent, FoundryChatClient
from agent_framework.exceptions import ChatClientContentFilterException
from azure.keyvault.secrets import SecretClient
from azure.cosmos import CosmosClient
import yaml
from classes.weather_services import get_forecast_weather
from classes.google_services import search_hotels_google_maps
from classes.human_review_services import create_ask_user_tool
from classes.secret_manager_services import SecretManager
from classes.user_sign_in_services import sign_in_user
from classes.user_memory_services import UserMemoryProvider, UserMemoryStore, format_preferences, format_trip
from classes.memory_extraction_services import ChatTurn, MemoryExtractor
from classes.scope_guard_services import ScopeGuard
from utils.agent_middleware import (
    BlockedReplyMonitor,
    add_todays_date_to_task,
    log_agent_replies,
    log_delegations,
    log_tool_calls,
)
from utils.observability import enable_tracing
from utils.utils import add_todays_date

# WARNING shows tool-loop problems such as "Maximum consecutive function call errors reached"
logging.getLogger("agent_framework").setLevel(logging.WARNING)

# The whole chat is saved as one multi-turn fine-tuning example in the fine-tuning container of Cosmos DB.
# Short stand-in for the concierge's full system prompt, different from the Magentic chat's one, so the
# two kinds of examples can be told apart.
FINE_TUNING_SYSTEM_PROMPT = (
    "You are a travel concierge. You plan trips with the user step by step using a travel guide, "
    "web search, weather forecasts and hotel search, you suggest the next steps, and you never invent facts."
)

# Typed at the "You:" prompt instead of a message. The chat runs them itself: no model call, and the
# concierge never sees them.
COMMANDS_HELP = (
    "Commands: /memory shows what is saved about you · /forget <number> deletes a saved trip · "
    "/forget profile deletes your preferences · /forget all deletes everything · exit ends the chat"
)
YES_ANSWERS = ("y", "yes", "t", "tak")

# =============================================================================
# CONFIGURATION
# =============================================================================

key_vault_url = os.getenv("KEY_VAULT_URL")
# Stored in the user's profile; comes from launch.json for now
user_role = os.getenv("USER_ROLE")
with open("config.yaml", "r") as file:
    config = yaml.safe_load(file)

# =============================================================================
# AUTHENTICATION
# =============================================================================

credential = DefaultAzureCredential()
secret_client = SecretClient(vault_url=key_vault_url, credential=credential)
secret_manager = SecretManager(secret_client)

# Cosmos DB with Entra ID instead of the account key: the template gives the developer (az login) the
# data-plane role "Cosmos DB Built-in Data Contributor". The Magentic chat still uses COSMOSDB-PRIMARY-KEY.
cosmos_db_client = CosmosClient(url=secret_manager.get_secret("COSMOSDB-ENDPOINT"), credential=credential)
database_client = cosmos_db_client.get_database_client(secret_manager.get_secret("COSMOSDB-DATABASE-NAME"))
memory_container = database_client.get_container_client(secret_manager.get_secret("COSMOSDB-MEMORY-CONTAINER-NAME"))
fine_tuning_container = database_client.get_container_client(
    secret_manager.get_secret("COSMOSDB-FINETUNING-CONTAINER-NAME")
)
if memory_container.read() and fine_tuning_container.read():
    print("Successfully connected to Cosmos DB (memory and fine-tuning containers)")
memory_store = UserMemoryStore(memory_container, fine_tuning_container, config)

# Secret name = setting name with '-' instead of '_' (see resource_deployment.bicep)
endpoint = secret_manager.get_secret("PROJECT-ENDPOINT")

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

# A small model proposes at the end of the chat what to remember for the next trips
memory_extractor = MemoryExtractor(
    FoundryChatClient(
        project_endpoint=endpoint,
        model=secret_manager.get_secret(config["concierge_memory"]["model_secret"]),
        credential=credential,
    ),
    config,
)

# The subagents' questions and the user's answers (filled by ask_user). They are not in the concierge's
# replies, but hold wishes worth remembering (hotel style, citizenship, interests).
specialist_answers: list[dict] = []


def create_agents(memory_provider: UserMemoryProvider) -> FoundryAgent:
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
        tools=[create_ask_user_tool("travel guide", specialist_answers)],
        middleware=subagent_middleware,
    )
    web_data_subagent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="webDataSubagent",
        description="Web search: country information, entry rules such as visas, and current travel information.",
        credential=credential,
        tools=[create_ask_user_tool("web search", specialist_answers)],
        middleware=subagent_middleware,
    )
    weather_subagent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="weatherSubagent",
        description="Daily weather forecast for a location and an optional date range (next 15 days).",
        credential=credential,
        tools=[get_forecast_weather, create_ask_user_tool("weather", specialist_answers)],
        middleware=subagent_middleware,
    )
    hotel_subagent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="hotelSubagent",
        description="Hotel search on Google Maps: picks the hotels that best match the user's wishes.",
        credential=credential,
        tools=[search_hotels_google_maps, create_ask_user_tool("hotel", specialist_answers)],
        middleware=subagent_middleware,
    )
    subagents = [rag_data_subagent, web_data_subagent, weather_subagent, hotel_subagent]

    # 2. The concierge. as_tool() turns each subagent into a function tool named after the agent, with one
    # string argument "task"; the concierge's server-side definition declares the same four tools, and
    # FoundryAgent runs the subagent when the service requests the call. Without propagate_session every
    # call is a new, separate conversation of the subagent.
    # Only the concierge gets the user's long-term memory (once per session: the session keeps it).
    concierge_agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name="conciergeAgent",
        credential=credential,
        tools=[subagent.as_tool() for subagent in subagents],
        middleware=[blocked_reply_monitor, log_delegations],
        context_providers=[memory_provider],
    )

    return concierge_agent


def print_block(title: str, text: str) -> None:
    print("\n" + "=" * 50)
    print(f"{title}:")
    print("=" * 50)
    print(text)
    print("=" * 50)


def confirm(question: str) -> bool:
    try:
        return input(question).strip().lower() in YES_ANSWERS
    except (EOFError, KeyboardInterrupt):
        return False


def handle_command(command: str, user_id: str, memory_provider: UserMemoryProvider, session) -> bool:
    """Run a /memory or /forget command typed in the chat.

    Returns True after /forget all: the turns of this chat so far are then not saved either.
    """
    parts = command.split()
    name = parts[0].lower()
    argument = parts[1].lower() if len(parts) == 2 else ""

    if name == "/memory" and len(parts) == 1:
        print_block("SAVED ABOUT YOU", memory_store.describe_for_user(user_id))
        return False
    if name != "/forget" or not argument:
        print(COMMANDS_HELP)
        return False

    if argument == "all":
        if not confirm("Delete everything saved about you (profile, trips and the chats saved for fine-tuning)? [y/N]: "):
            print("[LONG-TERM] Nothing deleted.")
            return False
        deleted = memory_store.forget_all(user_id)
        print(f"[LONG-TERM] Deleted {deleted} item(s). This chat so far will not be saved either.")
    elif argument == "profile":
        memory_store.clear_preferences(user_id)
        print("[LONG-TERM] Your preferences are deleted; the saved trips stay (/forget all deletes them too).")
    elif argument.isdigit():
        trips = memory_store.list_trips(user_id)
        number = int(argument)
        if not 1 <= number <= len(trips):
            print(f"[LONG-TERM] There is no trip {number}. /memory shows the numbers.")
            return False
        trip = trips[number - 1]
        memory_store.delete_trip(user_id, trip["id"])
        print(f"[LONG-TERM] Deleted trip {number} ({trip.get('city')}).")
    else:
        print(COMMANDS_HELP)
        return False

    memory_provider.refresh()
    if memory_provider.note_was_sent(session):
        # The note is part of the conversation that the service stores; it cannot be taken out of it
        print(
            "[LONG-TERM] The concierge got your memory with your first message and keeps it until this chat ends;"
            " the change applies from your next chat."
        )
    return argument == "all"


def save_conversation(user_id: str, turns: list[ChatTurn]) -> None:
    """Save the chat as one multi-turn fine-tuning example (system message, then the user's messages and the
    concierge's replies in turn) in the fine-tuning container. The agents never read it.
    """
    messages = [{"role": "system", "content": FINE_TUNING_SYSTEM_PROMPT}]
    for turn in turns:
        messages.append({"role": "user", "content": turn.user_message})
        messages.append({"role": "assistant", "content": turn.reply})
    try:
        memory_store.add_fine_tuning_record(user_id, messages)
        print(f"[LONG-TERM] Saved the chat ({len(turns)} turn(s)) for fine-tuning")
    except Exception as error:
        print(f"[LONG-TERM] Error saving the chat: {error}")


async def propose_memory(user_id: str, turns: list[ChatTurn], conversation_id: str | None) -> None:
    """Let the small model propose profile updates and trips from the chat; save them only when the user accepts."""
    print("\n[LONG-TERM] Looking for what to remember from this chat...")
    try:
        current_preferences = memory_store.get_preferences(user_id)
    except Exception as error:
        print(f"[LONG-TERM] Error reading your profile, nothing is saved: {error}")
        return
    proposal = await memory_extractor.extract(turns, current_preferences)
    if proposal is None:
        return
    if proposal.is_empty():
        print("[LONG-TERM] Nothing new to remember.")
        return

    updates = proposal.profile_updates.model_dump()
    lines = [f"Profile (new or changed): {format_preferences(updates) or '(nothing new)'}"]
    if proposal.trips:
        lines.append("Trips:")
        for trip in proposal.trips:
            lines.extend(format_trip(trip.model_dump(), date.today()))
    print_block("TO REMEMBER FOR YOUR NEXT TRIPS", "\n".join(lines))
    if not confirm("Save them to your profile for your next trips? [y/N]: "):
        print("[LONG-TERM] Not saved.")
        return

    try:
        memory_store.update_preferences(user_id, updates)
        saved = memory_store.add_trips(user_id, [trip.model_dump() for trip in proposal.trips], conversation_id)
        print(f"[LONG-TERM] Saved your preferences and {saved} trip(s).")
    except Exception as error:
        print(f"[LONG-TERM] Error saving the memory: {error}")


# Microsoft Agent Framework is built entirely on async programming: agent.run() is an async method.
async def run_chat(
    concierge_agent: FoundryAgent, user_id: str, memory_provider: UserMemoryProvider
) -> tuple[list[ChatTurn], str | None]:
    """Run the console chat with the concierge until the user quits.

    Returns the turns the concierge answered, for the fine-tuning record and the memory, and the id of the
    service-side conversation (stored with every saved trip, to show where it comes from).
    """
    # Once per process: covers the concierge, the subagents and their tool calls
    await enable_tracing(concierge_agent, config, "concierge_chat")
    print("Concierge chat started. Type 'exit' or 'quit' to stop.")
    print(COMMANDS_HELP + "\n")

    # One session for the whole chat: the service stores the conversation and the session points to it,
    # so the concierge remembers the earlier turns, its subagent calls and their results. The session is
    # updated only after a complete turn, so a turn that fails leaves it at the previous one.
    session = concierge_agent.create_session()
    turns: list[ChatTurn] = []
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
        if user_message_text.startswith("/"):
            try:
                if handle_command(user_message_text, user_id, memory_provider, session):
                    turns.clear()
            except Exception as e:
                print(f"❌ ERROR in the command: {e}")
            continue

        try:
            blocked_reply_monitor.reset()

            # The scope guard decides first. It gets the end of the concierge's previous reply, so a short
            # answer to it ("yes", "three days") counts as part of the travel conversation.
            scope = await scope_guard.check(user_message_text, previous_reply=last_reply)
            scope.print_line()
            if not scope.in_scope:
                # The concierge never sees the refused message, so it stays out of the conversation
                print_block("CONCIERGE", scope_guard.refusal)
                continue

            print("\n[CONCIERGE] Working on it...")
            questions_before = len(specialist_answers)
            # The agents do not know the current date, so it is put in front of the message.
            # The saved conversation keeps the message as the user wrote it.
            response = await concierge_agent.run(add_todays_date(user_message_text), session=session)
            reply = response.text
            if not reply:
                print("⚠️ The concierge returned no answer. Please try again.")
                continue

            print_block("CONCIERGE", reply)
            last_reply = reply

            # A refused or filtered reply is neither a valid fine-tuning example nor something to remember
            if blocked_reply_monitor.is_last_reply_blocked(concierge_agent.name):
                print("🛡️ The reply was refused or blocked (see the [GUARDRAIL] lines above).")
                print("It will not be saved to the conversation history.")
                continue

            turns.append(ChatTurn(user_message_text, reply, specialist_answers[questions_before:]))

        except ChatClientContentFilterException as e:
            # The content filter blocked the input of the scope guard, the concierge or one of the subagents
            checks = getattr(e, "content_filter_result", None) or {}
            triggered = [name for name, check in checks.items() if check.filtered]
            reason = f" ({', '.join(triggered)})" if triggered else ""
            print(f"🛡️ Your request was blocked by the content safety policy{reason}.")
            print("Please rephrase your message and try again.")

        except Exception as e:
            print(f"❌ ERROR during the concierge run: {e}")

    return turns, session.service_session_id


async def run_session(concierge_agent: FoundryAgent, user_id: str, memory_provider: UserMemoryProvider) -> None:
    """The chat and, when it has answered turns, the saving at the end (one event loop for both)."""
    turns, conversation_id = await run_chat(concierge_agent, user_id, memory_provider)
    # The chat is saved once, at the end: a single turn of it is rarely a complete request
    if turns:
        save_conversation(user_id, turns)
        await propose_memory(user_id, turns, conversation_id)


def main() -> int:
    # The user signs in (or signs up) in the external tenant before the agents start. The account's object ID (oid)
    # is the key of the user's memory; nobody can reach another user's memory by typing a name.
    user = sign_in_user(secret_manager.get_secret("ENTRA-APP-CLIENT-ID"), config["sign_in"])
    if user is None:
        print("\nExiting.")
        return 1
    name = user.name or user.username or "traveller"
    print(f"[SIGN-IN] Signed in as {user.username or name} (user id: {user.user_id})")

    try:
        _, is_new = memory_store.sign_in(user, user_role)
        if is_new:
            print(f"[LONG-TERM] Welcome, {name}! A new profile was created for you.")
        else:
            print(f"[LONG-TERM] Welcome back, {name}!")

        memory_provider = UserMemoryProvider(memory_store, user.user_id)
        concierge_agent = create_agents(memory_provider)
        asyncio.run(run_session(concierge_agent, user.user_id, memory_provider))
        return 0
    except Exception as e:
        print(f"\n❌ Execution failed: {str(e)}")
        return 1


if __name__ == "__main__":
    main()
