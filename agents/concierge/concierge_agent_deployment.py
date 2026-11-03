import os
import sys
import yaml
from pathlib import Path

# This script lives in agents/concierge/; add the project root so the classes package can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
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
# The concierge leads the conversation and plans the work, so it uses the same model as the Magentic manager
llm_model_deployment_name = secret_manager.get_secret("LLM-MODEL-DEPLOYMENT-NAME")

subscription_id = secret_manager.get_secret("AZURE-SUBSCRIPTION-ID")
resource_group_name = secret_manager.get_secret("AZURE-RESOURCE-GROUP")
account_name = secret_manager.get_secret("AZURE-COGNITIVE-ACCOUNT-NAME")

# Guardrails (RAI policy) shared by all agents
config_path = PROJECT_ROOT / "config.yaml"
with open(config_path, "r", encoding="utf-8") as file:
    config = yaml.safe_load(file)

# Name and system prompt of this agent
agent_config_path = Path(__file__).resolve().parent / "concierge_agent_config.yaml"
with open(agent_config_path, "r", encoding="utf-8") as file:
    agent_config = yaml.safe_load(file)

# =============================================================================
# AGENT FUNCTIONS
# =============================================================================


def create_subagent_tool(name: str, description: str) -> FunctionTool:
    """
    Declare one subagent as a function tool of the concierge.

    The subagent runs on the client: execution/concierge_agent.py passes subagent.as_tool() to the
    concierge's FoundryAgent, which runs the subagent when the service requests the call.
    as_tool() names the tool after the agent and gives it one string argument "task", so the name
    must match agent_name in agents/concierge/<subagent>_config.yaml and the argument must stay "task".
    """
    return FunctionTool(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "One self-contained task for the specialist: the city, the dates in "
                    "YYYY-MM-DD format when known, the user's language and everything the user said "
                    "that matters for this topic. The specialist does not see the conversation.",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    )


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

    # The concierge has no data tools of its own: its tools are the four subagents
    subagent_tools = [
        create_subagent_tool(
            "ragDataSubagent",
            "Travel guide specialist: attractions, restaurants, events, transportation and practical tips "
            "for popular cities from the travel guide knowledge base. First choice for destination "
            "information. It may ask the user what they are interested in.",
        ),
        create_subagent_tool(
            "webDataSubagent",
            "Web search specialist: general country information and entry rules such as visas, and "
            "destination information the knowledge base did not return (e.g. current events). It may ask "
            "the user about their citizenship or the length of the stay.",
        ),
        create_subagent_tool(
            "weatherSubagent",
            "Weather specialist: daily weather forecast for a location and an optional date range, only "
            "for the next 15 days. It may ask the user for the travel dates or the exact place.",
        ),
        create_subagent_tool(
            "hotelSubagent",
            "Hotel specialist: searches Google Maps and picks the 2 or 3 hotels that best match the user's "
            "wishes (ratings, reviews, amenities, links). It may ask the user about their wishes and shows "
            "its choice to the user for approval. It has no room prices and no availability.",
        ),
    ]

    try:
        print(f"   ✨ Creating or updating agent: {agent_name}")
        agent = project_client.agents.create_version(
            agent_name=agent_name,
            definition=PromptAgentDefinition(
                model=llm_model_deployment_name,
                instructions=system_prompt,
                tools=subagent_tools,
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
