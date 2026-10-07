import os
import sys
import yaml
from pathlib import Path

# This script lives in agents/; add the project root so the classes package can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.core.exceptions import HttpResponseError

from classes.rai_policies_services import RaiPolicyManager

# =============================================================================
# CONFIGURATION
# =============================================================================

project_endpoint = os.getenv("PROJECT_ENDPOINT")
llm_model_deployment_name = os.getenv("LLM_MODEL_DEPLOYMENT_NAME")

subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
resource_group_name = os.getenv("AZURE_RESOURCE_GROUP")
account_name = os.getenv("AZURE_COGNITIVE_ACCOUNT_NAME")

# Guardrails (RAI policy) shared by all agents
config_path = PROJECT_ROOT / "config.yaml"
with open(config_path, "r", encoding="utf-8") as file:
    config = yaml.safe_load(file)

# Name and system prompt of this agent
agent_config_path = Path(__file__).resolve().parent / "manager_agent_config.yaml"
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

    try:
        print(f"   ✨ Creating or updating agent: {agent_name}")
        # The manager only plans and delegates, so it has no tools
        agent = project_client.agents.create_version(
            agent_name=agent_name,
            definition=PromptAgentDefinition(
                model=llm_model_deployment_name,
                instructions=system_prompt,
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
