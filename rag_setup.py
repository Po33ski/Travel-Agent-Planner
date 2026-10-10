import os
import sys
import logging

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.search.documents.indexes import SearchIndexClient
from azure.storage.blob import ContainerClient

from classes.foundry_iq_services import FoundryIQService
from classes.secret_manager_services import SecretManager

# =============================================================================
# CONFIGURATION
# =============================================================================

key_vault_url = os.getenv("KEY_VAULT_URL")
# Local settings, not stored in Key Vault
knowledge_source_name = os.getenv("KNOWLEDGE_SOURCE_NAME", "travel-guide-ks")
source_file_path = os.getenv("SOURCE_FILE_PATH")

# =============================================================================
# AUTHENTICATION
# =============================================================================

# Our own calls (Key Vault, upload, creating the knowledge source/base) use Entra ID (az login)
credential = DefaultAzureCredential()
secret_client = SecretClient(vault_url=key_vault_url, credential=credential)
secret_manager = SecretManager(secret_client)

# Secret name = setting name with '-' instead of '_' (see resource_deployment.bicep)
search_endpoint = secret_manager.get_secret("SEARCH-ENDPOINT")
storage_account_url = secret_manager.get_secret("STORAGE-ACCOUNT-URL")
blob_container_name = secret_manager.get_secret("BLOB-CONTAINER-NAME")
aoai_endpoint = secret_manager.get_secret("AOAI-ENDPOINT")
llm_model_deployment_name = secret_manager.get_secret("LLM-MINI-MODEL-DEPLOYMENT-NAME")
embedding_model_deployment_name = secret_manager.get_secret("EMBEDDING-MODEL-DEPLOYMENT-NAME")
knowledge_base_name = secret_manager.get_secret("KNOWLEDGE-BASE-NAME")
# Keys Azure AI Search uses to reach Storage and the models
storage_connection_string = secret_manager.get_secret("STORAGE-CONNECTION-STRING")
aoai_api_key = secret_manager.get_secret("AOAI-API-KEY")

# Model names must match the deployments in resource_deployment.bicep
LLM_MODEL_NAME = "gpt-5-mini"
EMBEDDING_MODEL_NAME = "text-embedding-3-small"

KNOWLEDGE_DESCRIPTION = "Travel guide to 182 popular cities in Europe, Asia and the Americas."

REQUIRED_SETTINGS = {
    "SEARCH_ENDPOINT": search_endpoint,
    "STORAGE_ACCOUNT_URL": storage_account_url,
    "AOAI_ENDPOINT": aoai_endpoint,
    "LLM_MINI_MODEL_DEPLOYMENT_NAME": llm_model_deployment_name,
    "EMBEDDING_MODEL_DEPLOYMENT_NAME": embedding_model_deployment_name,
    "SOURCE_FILE_PATH": source_file_path,
    "STORAGE_CONNECTION_STRING": storage_connection_string,
    "AOAI_API_KEY": aoai_api_key,
}

# =============================================================================
# MAIN SCRIPT
# =============================================================================


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    # Silence per-request HTTP logging from the Azure SDK
    logging.getLogger("azure").setLevel(logging.WARNING)

    print("🚀 Starting Foundry IQ knowledge base setup")
    print("=" * 60)

    missing = [name for name, value in REQUIRED_SETTINGS.items() if not value]
    if missing:
        print(f"❌ Missing settings: {', '.join(missing)}")
        return 1

    try:
        service = FoundryIQService(
            container_client=ContainerClient(
                account_url=storage_account_url,
                container_name=blob_container_name,
                credential=credential,
            ),
            index_client=SearchIndexClient(endpoint=search_endpoint, credential=credential),
            # Azure AI Search calls Storage and the models itself, later and in the background, with keys
            storage_connection=storage_connection_string,
            aoai_endpoint=aoai_endpoint,
            chat_deployment=llm_model_deployment_name,
            chat_model=LLM_MODEL_NAME,
            embedding_deployment=embedding_model_deployment_name,
            embedding_model=EMBEDDING_MODEL_NAME,
            aoai_api_key=aoai_api_key,
        )

        kb_name = service.setup(
            file_path=source_file_path,
            knowledge_source_name=knowledge_source_name,
            knowledge_base_name=knowledge_base_name,
            description=KNOWLEDGE_DESCRIPTION,
        )

        print(f"\n✨ Knowledge base ready: {kb_name}")
        print(f"   MCP endpoint: {FoundryIQService.get_mcp_endpoint(search_endpoint, kb_name)}")
        return 0

    except Exception as e:
        print(f"\n❌ Knowledge base setup failed: {str(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
