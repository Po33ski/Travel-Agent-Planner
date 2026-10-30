import os
import sys
import time
import logging
from pathlib import Path
from urllib.parse import unquote, urlparse

# This script lives in rag/; add the project root so the classes package can be imported,
# and work from the root, because SOURCE_FOLDER_PATH (inputs/travel_guide) is relative to it
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient, SearchIndexerClient
from azure.search.documents.indexes.models import IndexerExecutionResult, IndexerExecutionStatus
from azure.storage.blob import ContainerClient

from classes.foundry_iq_services import FoundryIQService
from classes.secret_manager_services import SecretManager

# =============================================================================
# CONFIGURATION
# =============================================================================

key_vault_url = os.getenv("KEY_VAULT_URL")
# Local settings, not stored in Key Vault
knowledge_source_name = os.getenv("KNOWLEDGE_SOURCE_NAME", "travel-guide-ks")
# Folder with the guide split into one file per city: every file in it is uploaded
source_folder_path = os.getenv("SOURCE_FOLDER_PATH")
SOURCE_FILE_PATTERN = "*.md"

# How long to wait for the indexer run that ingests the uploaded files
INGESTION_TIMEOUT_SECONDS = 1800
INGESTION_POLL_SECONDS = 15
# Indexer warnings that mean content was lost: the run counts as failed
BLOCKING_WARNINGS = ("Truncated",)

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
    "SOURCE_FOLDER_PATH": source_folder_path,
    "STORAGE_CONNECTION_STRING": storage_connection_string,
    "AOAI_API_KEY": aoai_api_key,
}

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def delete_stale_blobs(container_client: ContainerClient, keep: set[str]) -> list[str]:
    """Delete the blobs that are not in `keep` (files removed or renamed in the source folder)."""
    stale = [blob.name for blob in container_client.list_blobs() if blob.name not in keep]
    for name in stale:
        container_client.delete_blob(name)
    return stale


def wait_for_indexer_run(
    indexer_client: SearchIndexerClient,
    indexer_name: str,
    previous_start,
) -> IndexerExecutionResult:
    """Wait until an indexer run that started after `previous_start` has finished, and return it.

    Polls the indexer, not the knowledge source: the knowledge source status still shows
    the previous synchronisation until the new run starts, so it can report "finished" too early.
    """
    deadline = time.monotonic() + INGESTION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        last = indexer_client.get_indexer_status(indexer_name).last_result
        is_new = last is not None and (previous_start is None or last.start_time > previous_start)
        if is_new and last.status != IndexerExecutionStatus.IN_PROGRESS:
            return last
        print(f"   ⏳ Waiting for indexer '{indexer_name}' ({last.status if is_new else 'not started'})...")
        time.sleep(INGESTION_POLL_SECONDS)
    raise TimeoutError(f"Indexer '{indexer_name}' did not finish within {INGESTION_TIMEOUT_SECONDS}s")


def report_indexer_run(run: IndexerExecutionResult) -> bool:
    """Print the outcome, errors and warnings of an indexer run. Returns False if content was lost."""
    print(f"   Status: {run.status}, items processed: {run.item_count}, failed: {run.failed_item_count}")
    ok = run.status == IndexerExecutionStatus.SUCCESS and not run.failed_item_count
    if run.error_message:
        print(f"   ❌ {run.error_message}")
    for error in run.errors or []:
        print(f"   ❌ {error.key}: {error.error_message}")
        ok = False
    for warning in run.warnings or []:
        blocking = any(text in warning.message for text in BLOCKING_WARNINGS)
        print(f"   {'❌' if blocking else '⚠️'} {warning.key or 'indexer'}: {warning.message}")
        ok = ok and not blocking
    return ok


def delete_stale_chunks(search_client: SearchClient, keep: set[str]) -> int:
    """Delete index chunks whose source blob is not in `keep`.

    The generated data source has no deletion detection, so chunks of a deleted blob
    stay in the index until they are removed here.
    """
    stale = []
    for doc in search_client.search("*", select=["uid", "blob_url"], top=100000):
        # blob_url: https://<account>.blob.core.windows.net/<container>/<blob name>
        blob_name = unquote(urlparse(doc["blob_url"]).path).split("/", 2)[2]
        if blob_name not in keep:
            stale.append({"uid": doc["uid"]})
    if stale:
        search_client.delete_documents(documents=stale)
    return len(stale)


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

    source_files = sorted(Path(source_folder_path).glob(SOURCE_FILE_PATTERN))
    if not source_files:
        print(f"❌ No {SOURCE_FILE_PATTERN} files in {source_folder_path}")
        return 1

    try:
        container_client = ContainerClient(
            account_url=storage_account_url,
            container_name=blob_container_name,
            credential=credential,
        )
        index_client = SearchIndexClient(endpoint=search_endpoint, credential=credential)
        indexer_client = SearchIndexerClient(endpoint=search_endpoint, credential=credential)
        service = FoundryIQService(
            container_client=container_client,
            index_client=index_client,
            # Azure AI Search calls Storage and the models itself, later and in the background, with keys
            storage_connection=storage_connection_string,
            aoai_endpoint=aoai_endpoint,
            chat_deployment=llm_model_deployment_name,
            chat_model=LLM_MODEL_NAME,
            embedding_deployment=embedding_model_deployment_name,
            embedding_model=EMBEDDING_MODEL_NAME,
            aoai_api_key=aoai_api_key,
        )

        # 1. Upload every file of the source folder, one blob per file
        print(f"\n📤 Uploading {len(source_files)} file(s) from {source_folder_path}")
        uploaded = {service.upload_blob(path) for path in source_files}
        stale_blobs = delete_stale_blobs(container_client, uploaded)
        print(f"   Uploaded {len(uploaded)} blob(s), deleted {len(stale_blobs)} stale blob(s): {stale_blobs or 'none'}")

        # 2. Knowledge source (creates data source, skillset, index and indexer on the first run)
        service.create_blob_knowledge_source(knowledge_source_name, KNOWLEDGE_DESCRIPTION)
        # Names of the generated resources (a mapping: datasource, indexer, skillset, index)
        created = index_client.get_knowledge_source(knowledge_source_name).azure_blob_parameters.created_resources
        indexer_name, index_name = created["indexer"], created["index"]

        # 3. Run the indexer now instead of waiting for its daily schedule, and wait for that run
        print(f"\n🔄 Indexing into '{index_name}'")
        last = indexer_client.get_indexer_status(indexer_name).last_result
        previous_start = last.start_time if last else None
        try:
            indexer_client.run_indexer(indexer_name)
        except HttpResponseError as e:
            if e.status_code != 409:  # 409: a run is already in progress (the first one starts by itself)
                raise
        run = wait_for_indexer_run(indexer_client, indexer_name, previous_start)
        if not report_indexer_run(run):
            print("\n❌ Indexing finished with errors or lost content; the knowledge base was not updated")
            return 1

        # 4. Remove chunks of blobs that no longer exist
        search_client = SearchClient(endpoint=search_endpoint, index_name=index_name, credential=credential)
        print(f"   Deleted {delete_stale_chunks(search_client, uploaded)} stale chunk(s)")

        # 5. Knowledge base
        kb_name = service.create_knowledge_base(
            knowledge_base_name, [knowledge_source_name], description=KNOWLEDGE_DESCRIPTION
        )

        print(f"\n✨ Knowledge base ready: {kb_name}")
        print(f"   MCP endpoint: {FoundryIQService.get_mcp_endpoint(search_endpoint, kb_name)}")
        return 0

    except Exception as e:
        print(f"\n❌ Knowledge base setup failed: {str(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
