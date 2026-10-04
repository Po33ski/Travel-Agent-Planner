import os
import asyncio
import logging
from azure.identity import DefaultAzureCredential
from agent_framework.foundry import FoundryAgent
from agent_framework import FunctionInvocationContext
from classes.weather_services import get_forecast_weather, get_current_weather
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

def create_agent():
    # Pass the local implementation into tools so FoundryAgent handles execution callbacks
    agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name=agent_name,
        credential=credential,
        tools=[get_forecast_weather, search_for_hotels],
        middleware=[log_tool_calls],
    )
    return agent


# def extract_url_citations(result) -> list[tuple[str, str]]:
#     citations = {}
#     for message in getattr(result, "messages", None) or []:
#         for content in getattr(message, "contents", None) or []:
#             for annotation in getattr(content, "annotations", None) or []:
#                 url = getattr(annotation, "url", None)
#                 if url:
#                     citations[url] = getattr(annotation, "title", None) or url
#     return [(title, url) for url, title in citations.items()]


async def run_agent(agent):
    print(
        "Interactive agent mode started.\n"
    )
    session = await agent.create_conversation()

    while True:
        try:
            user_message_text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_message_text or user_message_text.lower() in ("exit", "quit"):
            break

        try:
            print("\n[AGENT] Executing pipeline...")

            # When the server agent requests a function call, FoundryAgent runs the local
            # weather / hotel tool and sends the result back; MCP and web search run server-side.
            result = await agent.run(user_message_text, session=session)

            final_text = getattr(result, "text", str(result))
            print("\n" + "=" * 50)
            print("ASSISTANT REPLY:")
            print("=" * 50)
            print(final_text)

            speech_result = text_to_speech(final_text)
            print("\n" + "=" * 50)
            print("SPEECH SYNTHESIS RESULT:")
            print(f"\n[SPEECH] {str(speech_result)}")
            print("=" * 50)


        except Exception as e:
            print(f"❌ ERROR during workflow execution: {e}")
            # The server-side conversation may now hold a function call without output, so it can't continue
            session = await agent.create_conversation()
            print("↻ Started a new conversation.")


def main() -> int:
    try:
        agent = create_agent()
        asyncio.run(run_agent(agent))
        return 0
    except Exception as e:
        print(f"\n❌ Execution failed: {str(e)}")
        return 1


if __name__ == "__main__":
    main()
