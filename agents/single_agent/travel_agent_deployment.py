import os
import sys
import yaml
from pathlib import Path

# This script lives in agents/single_agent/; add the project root so the classes package can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, FunctionTool, WebSearchTool, WebSearchApproximateLocation
from azure.core.exceptions import HttpResponseError

from classes.foundry_iq_services import FoundryIQService
from classes.rai_policies_services import RaiPolicyManager
from classes.secret_manager_services import SecretManager

# =============================================================================
# CONFIGURATION
# =============================================================================

key_vault_url = os.getenv("KEY_VAULT_URL")

# =============================================================================
# AUTHENTICATION
# =============================================================================

credential = DefaultAzureCredential()
secret_client = SecretClient(vault_url=key_vault_url, credential=credential)
secret_manager = SecretManager(secret_client)

# Secret name = setting name with '-' instead of '_' (see resource_deployment.bicep)
project_endpoint = secret_manager.get_secret("PROJECT-ENDPOINT")
# The single agent does the work of the whole team, so it uses the same model as the manager
llm_model_deployment_name = secret_manager.get_secret("LLM-MODEL-DEPLOYMENT-NAME")
# Foundry IQ knowledge base
search_endpoint = secret_manager.get_secret("SEARCH-ENDPOINT")
knowledge_base_name = secret_manager.get_secret("KNOWLEDGE-BASE-NAME")
knowledge_base_connection_name = secret_manager.get_secret("KNOWLEDGE-BASE-CONNECTION-NAME")

subscription_id = secret_manager.get_secret("AZURE-SUBSCRIPTION-ID")
resource_group_name = secret_manager.get_secret("AZURE-RESOURCE-GROUP")
account_name = secret_manager.get_secret("AZURE-COGNITIVE-ACCOUNT-NAME")

# Guardrails (RAI policy) shared by all agents
config_path = PROJECT_ROOT / "config.yaml"
with open(config_path, "r", encoding="utf-8") as file:
    config = yaml.safe_load(file)

# Name and system prompt of this agent
agent_config_path = Path(__file__).resolve().parent / "travel_agent_config.yaml"
with open(agent_config_path, "r", encoding="utf-8") as file:
    agent_config = yaml.safe_load(file)

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

    # The single agent has the tools of all four specialist agents

    # Parameters must match get_forecast_weather in classes/weather_services.py
    forecast_tool = FunctionTool(
        name="get_forecast_weather",
        description="Get the daily weather forecast for a given location and optional date range.",
        parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name. The location for which to get weather information.",
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

    # Parameters must match search_hotels_google_maps in classes/google_services.py
    hotel_search_tool = FunctionTool(
        name="search_hotels_google_maps",
        description="Search Google Maps for hotels in a given city, optionally matching the user's preferences. "
        "Returns up to 20 hotels with structured data: name, type, address, rating, number of reviews, "
        "price level, summary, guest reviews, amenities, website and Google Maps link. "
        "It has no room prices and no availability for travel dates.",
        parameters={
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "The city in which to search for hotels.",
                    },
                    "preferences": {
                        "type": "string",
                        "description": "What the user is looking for, as a few keywords in the user's language, "
                        "e.g. 'quiet, spa, near the Old Town' (optional).",
                    },
                    "language": {
                        "type": "string",
                        "description": "ISO 639-1 language code of the conversation, e.g. 'pl' or 'en'. "
                        "The results are returned in this language.",
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
                tools=[forecast_tool, hotel_search_tool, web_search_tool, knowledge_base_tool],
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
        print("❌ Missing secret: SEARCH-ENDPOINT (required for the knowledge base tool)")
        return 1

    try:
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
                agent_config,
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
