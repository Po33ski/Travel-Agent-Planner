import os
import sys
import yaml
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, FunctionTool, WebSearchTool, WebSearchApproximateLocation
from azure.core.exceptions import HttpResponseError

from classes.foundry_iq_services import FoundryIQService
# from rai_policies import create_or_update_rai_policy
from classes.rai_policies_services import RaiPolicyManager

# =============================================================================
# CONFIGURATION
# =============================================================================

project_endpoint = os.getenv("PROJECT_ENDPOINT")
llm_model_deployment_name = os.getenv("LLM_MODEL_DEPLOYMENT_NAME")
# Foundry IQ knowledge base (values from the resource_deployment.bicep outputs)
search_endpoint = os.getenv("SEARCH_ENDPOINT")
knowledge_base_name = os.getenv("KNOWLEDGE_BASE_NAME", "travel-guide-kb")
knowledge_base_connection_name = os.getenv("KNOWLEDGE_BASE_CONNECTION_NAME", "travel-guide-kb-mcp")

subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
resource_group_name = os.getenv("AZURE_RESOURCE_GROUP")
account_name = os.getenv("AZURE_COGNITIVE_ACCOUNT_NAME")

config_path = Path("config.yaml")
with open(config_path, "r", encoding="utf-8") as file:
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
    rai_config: object,
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
        rai_config: RAI configuration object
    Returns:
        The created or updated Agent object
    """
    agent_name = config.get("agent_name")
    system_prompt = config.get("system_prompt")

    print(f"🔄 Processing agent: {agent_name}")

    # Parameters must match get_forecast_weather in classes/weather_services.py
    forecast_tool = FunctionTool(
        name="get_forecast_weather",
        description="Get the daily weather forecast for a given location and optional date range.",
        parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The location for which to get weather information.",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "First day in YYYY-MM-DD format. Omit both dates for the next 15 days.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Last day in YYYY-MM-DD format. Use only with start_date.",
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

    # Parameters must match search_for_hotels in classes/hotel_services.py
    hotel_search_tool = FunctionTool(
        name="search_for_hotels",
        description="Search booking sites (booking.com, hotels.com, tripadvisor.com) for hotels in a given city "
        "and optional date range. Returns raw search results with prices in a single target currency.",
        parameters={
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "The city in which to search for hotels.",
                    },
                    "check_in": {
                        "type": "string",
                        "description": "Check-in date in YYYY-MM-DD format (optional).",
                    },
                    "check_out": {
                        "type": "string",
                        "description": "Check-out date in YYYY-MM-DD format (optional). Use only with check_in.",
                    },
                    "language": {
                        "type": "string",
                        "description": "ISO 639-1 language code of the conversation, e.g. 'pl' or 'en'. "
                        "'pl' returns prices in PLN, any other language in USD.",
                    },
                },
                "required": ["city"],
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
                tools=[forecast_tool, current_weather_tool, hotel_search_tool, web_search_tool, knowledge_base_tool],
                rai_config=rai_config,
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

        # Create or update the RAI policy and get its rai_config to attach to the agent
        rai_manager = RaiPolicyManager(
            subscription_id=subscription_id,
            resource_group_name=resource_group_name,
            account_name=account_name,
            credential=credential,
        )
        rai_config = rai_manager.create_or_update_rai_policy(
            config
        )
        
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
                rai_config
            )

        print("\n✨ Agent deployment completed successfully!")
        return 0

    except Exception as e:
        print(f"\n❌ Deployment failed: {str(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
