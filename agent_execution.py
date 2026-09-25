import os
import asyncio
import logging
from pathlib import Path
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from agent_framework.foundry import FoundryAgent
from classes.weather_services import get_forecast_weather, get_current_weather

logging.getLogger("agent_framework").setLevel(logging.ERROR)

endpoint = os.getenv("PROJECT_ENDPOINT")
agent_name = os.getenv("AGENT_NAME")

credential = DefaultAzureCredential()
project_client = AIProjectClient(endpoint=endpoint, credential=credential)
# =============================================================================
# AGENT INITIALIZATION
# =============================================================================


def create_agent():
    # Pass the local implementation into tools so FoundryAgent handles execution callbacks
    agent = FoundryAgent(
        project_endpoint=endpoint,
        agent_name=agent_name,
        credential=credential,
        tools=[get_forecast_weather, get_current_weather],
    )
    return agent

def extract_url_citations(result) -> list[tuple[str, str]]:
    citations = {}
    for message in getattr(result, "messages", None) or []:
        for content in getattr(message, "contents", None) or []:
            for annotation in getattr(content, "annotations", None) or []:
                url = getattr(annotation, "url", None)
                if url:
                    citations[url] = getattr(annotation, "title", None) or url
    return [(title, url) for url, title in citations.items()]


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

            # When the server agent requests tool execution, FoundryAgent handles
            # running `analyze_document_with_intelligence` locally and passing results back.
            result = await agent.run(user_message_text, session=session)

            final_text = getattr(result, "text", str(result))
            print("\n" + "=" * 50)
            print("ASSISTANT REPLY:")
            print("=" * 50)
            print(final_text)
            citations = extract_url_citations(result)
            if citations:
                print("\nSOURCES (knowledge base / web search):")
                for title, url in citations:
                    print(f"- {title}: {url}")
            print("=" * 50)

        except Exception as e:
            print(f"❌ ERROR during workflow execution: {e}")


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
