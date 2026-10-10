import os
import sys
import yaml
from pathlib import Path

# This script lives in agents/; add the project root so the classes package can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, FunctionTool
from azure.core.exceptions import HttpResponseError

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
# Specialist agents use the smaller model; only the manager uses LLM-MODEL-DEPLOYMENT-NAME
llm_model_deployment_name = secret_manager.get_secret("LLM-MINI-MODEL-DEPLOYMENT-NAME")

subscription_id = secret_manager.get_secret("AZURE-SUBSCRIPTION-ID")
resource_group_name = secret_manager.get_secret("AZURE-RESOURCE-GROUP")
account_name = secret_manager.get_secret("AZURE-COGNITIVE-ACCOUNT-NAME")

# Guardrails (RAI policy) shared by all agents
config_path = PROJECT_ROOT / "config.yaml"
with open(config_path, "r", encoding="utf-8") as file:
    config = yaml.safe_load(file)

# Name and system prompt of this agent
agent_config_path = Path(__file__).resolve().parent / "hotel_agent_config.yaml"
with open(agent_config_path, "r", encoding="utf-8") as file:
    agent_config = yaml.safe_load(file)

# =============================================================================
# AGENT FUNCTIONS
# =============================================================================


def create_or_update_agent(
    project_client: AIProjectClient,
    config: dict,
    llm_model_deployment_name: str,
    rai_config: object,
) -> object:
    """
    Create or update a server-side agent in the Azure AI Foundry project.

    Args:
        project_client: Authenticated AIProjectClient instance
        config: Agent configuration dictionary from YAML
        llm_model_deployment_name: Model deployment used by the agent
        rai_config: RAI configuration object
    Returns:
        The created or updated Agent object
    """
    agent_name = config.get("agent_name")
    system_prompt = config.get("system_prompt")

    print(f"🔄 Processing agent: {agent_name}")

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

    try:
        print(f"   ✨ Creating or updating agent: {agent_name}")
        agent = project_client.agents.create_version(
            agent_name=agent_name,
            definition=PromptAgentDefinition(
                model=llm_model_deployment_name,
                instructions=system_prompt,
                tools=[hotel_search_tool],
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
                rai_config
            )

        print("\n✨ Agent deployment completed successfully!")
        return 0

    except Exception as e:
        print(f"\n❌ Deployment failed: {str(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
