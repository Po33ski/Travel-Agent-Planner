import os
import asyncio
import logging
from pathlib import Path
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from agent_framework.foundry import FoundryAgent
from classes.azure_ai_services import analyze_document_with_intelligence

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
        tools=[analyze_document_with_intelligence],
    )
    return agent


async def run_agent(agent):
    print(
        "Interactive workflow-agent mode started with Document Intelligence Tool support. Type 'exit' to stop.\n"
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
