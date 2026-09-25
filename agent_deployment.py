import os
import sys
import yaml
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, FunctionTool, WebSearchTool, WebSearchApproximateLocation
from azure.core.exceptions import HttpResponseError

from classes.foundry_iq_services import FoundryIQService

# =============================================================================
# CONFIGURATION
# =============================================================================

project_endpoint = os.getenv("PROJECT_ENDPOINT")
llm_model_deployment_name = os.getenv("LLM_MODEL_DEPLOYMENT_NAME")
# Foundry IQ knowledge base (values from the resource_deployment.bicep outputs)
search_endpoint = os.getenv("SEARCH_ENDPOINT")
knowledge_base_name = os.getenv("KNOWLEDGE_BASE_NAME", "travel-guide-kb")
knowledge_base_connection_name = os.getenv("KNOWLEDGE_BASE_CONNECTION_NAME", "travel-guide-kb-mcp")

config_path = Path("config.yaml")
with open(config_path, "r") as file:
    config = yaml.safe_load(file)

# =============================================================================
# AGENT FUNCTIONS
# =============================================================================


def create_or_update_agent(
    project_client: AIProjectClient,
    config: dict,
    llm_model_deployment_name: str,
    search_endpoint: str,
    knowledge_base_name: str,
    knowledge_base_connection_name: str,
) -> object:
    """
    Create or update a server-side agent in the Azure AI Foundry project.

    Args:
        project_client: Authenticated AIProjectClient instance
        config: Agent configuration dictionary from YAML
        llm_model_deployment_name: Model deployment used by the agent
        search_endpoint: Azure AI Search endpoint hosting the knowledge base
        knowledge_base_name: Foundry IQ knowledge base name
        knowledge_base_connection_name: RemoteTool project connection to the knowledge base

    Returns:
        The created or updated Agent object
    """
    agent_name = config.get("agent_name")
    system_prompt = config.get("system_prompt")

    print(f"🔄 Processing agent: {agent_name}")

    forecast_tool = FunctionTool(
        name="get_forecast_weather",
        description="Get weather forecast for a given location and date frame.",
        parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The location for which to get weather information.",
                    },
            },
            "required": ["location"],
        }
    )

    current_weather_tool = FunctionTool(
        name="get_current_weather",
        description="Get current weather for a given location.",
        parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The location for which to get current weather information.",
                    },
                },
                "required": ["location"],
        }
    )

    web_search_tool = WebSearchTool(
        user_location=WebSearchApproximateLocation(
            country="PL", city="Warsaw", region="Mazowieckie"
        ),
        search_context_size="medium",
        external_web_access=False,
    )

    knowledge_base_tool = FoundryIQService.create_mcp_tool(
        search_endpoint=search_endpoint,
        knowledge_base_name=knowledge_base_name,
        project_connection_name=knowledge_base_connection_name,
        server_description="Travel guide to 182 popular cities in Europe, Asia and the Americas: "
        "attractions, restaurants, events, transport and practical travel tips.",
    )

    try:
        print(f"   ✨ Creating or updating agent: {agent_name}")
        agent = project_client.agents.create_version(
            agent_name=agent_name,
            definition=PromptAgentDefinition(
                model=llm_model_deployment_name,
                instructions=system_prompt,
                tools=[forecast_tool, current_weather_tool, web_search_tool, knowledge_base_tool],
            ),
        )
        print(f"   ✅ Agent created or updated successfully (ID: {agent.id})")

        return agent

    except HttpResponseError as e:
        print(f"   ❌ Azure REST API Error: {e.message}")
        raise
    except Exception as e:
        print(f"   ❌ Unexpected error: {str(e)}")
        raise


# =============================================================================
# MAIN SCRIPT
# =============================================================================


def main() -> int:
    print("🚀 Starting agent deployment to Azure AI Foundry")
    print("=" * 60)

    if not search_endpoint:
        print("❌ Missing environment variable: SEARCH_ENDPOINT (required for the knowledge base tool)")
        return 1

    try:
        # Initialize client via context manager
        credential = DefaultAzureCredential()

        with AIProjectClient(
            endpoint=project_endpoint, credential=credential
        ) as project_client:

            print(f"Connected to Azure AI Foundry project at: {project_endpoint}")
            agent = create_or_update_agent(
                project_client,
                config,
                llm_model_deployment_name,
                search_endpoint,
                knowledge_base_name,
                knowledge_base_connection_name,
            )

        print("\n✨ Agent deployment completed successfully!")
        return 0

    except Exception as e:
        print(f"\n❌ Deployment failed: {str(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
