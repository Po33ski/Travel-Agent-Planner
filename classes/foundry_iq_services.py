
"""
FoundryIQService – builds a Foundry IQ knowledge base on top of Azure Blob Storage.
 
Flow:
    1. upload_blob()                    – upload a local file to the blob container
    2. create_blob_knowledge_source()   – Azure AI Search auto-creates data source, skillset,
                                          index and indexer from the container
    3. wait_for_ingestion()             – wait until the first indexing run has finished
    4. create_knowledge_base()          – knowledge base that references the knowledge source(s)
    5. create_context_provider()        – context provider for Microsoft Agent Framework
 
Requirements (preview APIs, 2026-08-01-preview):
    pip install azure-identity azure-storage-blob
    pip install --pre azure-search-documents            # tested against 12.1.0b2
    pip install --pre agent-framework-azure-ai-search   # tested against 1.0.0b260910
 
RBAC:
    - Search service managed identity: "Storage Blob Data Reader" on the storage account
      and "Cognitive Services User" on the Azure OpenAI / Foundry resource.
    - Identity running this code: "Storage Blob Data Contributor" on the container and
      "Search Service Contributor" + "Search Index Data Reader" on the search service.
"""
 
from __future__ import annotations
 
import logging
import time
from pathlib import Path
 
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    AzureBlobKnowledgeSource,
    AzureBlobKnowledgeSourceParameters,
    AzureOpenAIVectorizerParameters,
    KnowledgeBase,
    KnowledgeBaseAzureOpenAIModel,
    KnowledgeSourceContentExtractionMode,
    KnowledgeSourceReference,
)
from azure.search.documents.knowledgebases.models import (
    KnowledgeRetrievalAutoReasoningEffort,
    KnowledgeRetrievalOutputMode,
    KnowledgeSourceAzureOpenAIVectorizer,
    KnowledgeSourceIngestionParameters,
)
from azure.storage.blob import ContainerClient, ContentSettings
from agent_framework_azure_ai_search import AzureAISearchContextProvider
logger = logging.getLogger(__name__)
 
_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
 
 
class FoundryIQService:
    """Creates and wires up a Foundry IQ knowledge base backed by a blob container.
 
    The clients and the shared model configuration are passed once in the constructor.
    Resource names (knowledge source, knowledge base) are method arguments, so one
    service instance can manage several knowledge sources / knowledge bases.
    """
 
    def __init__(
        self,
        container_client: ContainerClient,
        index_client: SearchIndexClient,
        *,
        storage_connection: str,
        aoai_endpoint: str,
        chat_deployment: str,
        chat_model: str,
        embedding_deployment: str,
        embedding_model: str,
    ) -> None:
        """
        Args:
            container_client: Blob container that holds the source documents.
            index_client: Azure AI Search index client (manages knowledge sources/bases).
            storage_connection: Connection string used by Azure AI Search to read the
                container. Prefer managed identity:
                "ResourceId=/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<account>"
            aoai_endpoint: Azure OpenAI / Foundry resource URL, e.g. https://<name>.openai.azure.com
            chat_deployment / chat_model: chat model used for ingestion and query planning.
            embedding_deployment / embedding_model: embedding model used for vectorisation.
        """
        self._container = container_client
        self._index_client = index_client
        self._storage_connection = storage_connection
        self._chat = AzureOpenAIVectorizerParameters(
            resource_url=aoai_endpoint,
            deployment_name=chat_deployment,
            model_name=chat_model,
        )
        self._embedding = AzureOpenAIVectorizerParameters(
            resource_url=aoai_endpoint,
            deployment_name=embedding_deployment,
            model_name=embedding_model,
        )
 
    # ------------------------------------------------------------------ 1. upload
    def upload_blob(self, file_path: str | Path, blob_name: str | None = None, overwrite: bool = True) -> str:
        """Upload a local file to the container. Returns the blob name."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        name = blob_name or path.name
        content_type = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        with path.open("rb") as data:
            self._container.upload_blob(
                name=name,
                data=data,
                overwrite=overwrite,
                content_settings=ContentSettings(content_type=content_type),
            )
        logger.info("Uploaded %s to container '%s' as '%s'", path, self._container.container_name, name)
        return name
 
    # ------------------------------------------------------------------ 2. knowledge source
    def create_blob_knowledge_source(
        self,
        knowledge_source_name: str,
        description: str,
        folder_path: str | None = None,
    ) -> str:
        """Create (or update) a blob knowledge source over the container.
 
        Azure AI Search generates '<name>-datasource', '<name>-skillset', '<name>-index'
        and '<name>-indexer' automatically and starts the first indexing run.
        Returns the knowledge source name.
        """
        knowledge_source = AzureBlobKnowledgeSource(
            name=knowledge_source_name,
            description=description,
            azure_blob_parameters=AzureBlobKnowledgeSourceParameters(
                connection_string=self._storage_connection,
                container_name=self._container.container_name,
                folder_path=folder_path,
                ingestion_parameters=KnowledgeSourceIngestionParameters(
                    chat_completion_model=KnowledgeBaseAzureOpenAIModel(azure_open_ai_parameters=self._chat),
                    embedding_model=KnowledgeSourceAzureOpenAIVectorizer(azure_open_ai_parameters=self._embedding),
                    content_extraction_mode=KnowledgeSourceContentExtractionMode.MINIMAL,
                    disable_image_verbalization=True,  # text-only documents
                ),
            ),
        )
        self._index_client.create_or_update_knowledge_source(knowledge_source)
        logger.info("Knowledge source '%s' created or updated", knowledge_source_name)
        return knowledge_source_name
 
    # ------------------------------------------------------------------ 3. wait
    def wait_for_ingestion(
        self,
        knowledge_source_name: str,
        timeout_seconds: int = 900,
        poll_seconds: int = 15,
    ) -> None:
        """Block until the knowledge source finished its (first) synchronisation.
 
        Raises TimeoutError if it does not finish in time and RuntimeError if
        any document failed to index.
        """
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = self._index_client.get_knowledge_source_status(knowledge_source_name)
            last = status.last_synchronization_state
            running = status.current_synchronization_state
            if last is not None and running is None:
                if last.items_updates_failed:
                    raise RuntimeError(
                        f"Ingestion of '{knowledge_source_name}' finished with "
                        f"{last.items_updates_failed} failed item(s)"
                    )
                logger.info(
                    "Ingestion of '%s' finished: %s item(s) processed",
                    knowledge_source_name, last.items_updates_processed,
                )
                return
            logger.info("Waiting for '%s' (status: %s)...", knowledge_source_name, status.synchronization_status)
            time.sleep(poll_seconds)
        raise TimeoutError(f"Ingestion of '{knowledge_source_name}' did not finish within {timeout_seconds}s")
 
    # ------------------------------------------------------------------ 4. knowledge base
    def create_knowledge_base(
        self,
        knowledge_base_name: str,
        knowledge_source_names: list[str],
        description: str | None = None,
        retrieval_instructions: str | None = None,
        output_mode: KnowledgeRetrievalOutputMode = KnowledgeRetrievalOutputMode.EXTRACTIVE_DATA,
    ) -> str:
        """Create (or update) a knowledge base referencing one or more knowledge sources.
 
        EXTRACTIVE_DATA returns raw chunks (the agent writes the answer);
        ANSWER_SYNTHESIS returns an answer generated by Azure AI Search.
        Returns the knowledge base name.
        """
        knowledge_base = KnowledgeBase(
            name=knowledge_base_name,
            description=description,
            retrieval_instructions=retrieval_instructions,
            knowledge_sources=[KnowledgeSourceReference(name=n) for n in knowledge_source_names],
            models=[KnowledgeBaseAzureOpenAIModel(azure_open_ai_parameters=self._chat)],
            output_mode=output_mode,
            retrieval_reasoning_effort=KnowledgeRetrievalAutoReasoningEffort(),
        )
        self._index_client.create_or_update_knowledge_base(knowledge_base)
        logger.info("Knowledge base '%s' created or updated", knowledge_base_name)
        return knowledge_base_name
 
    # ------------------------------------------------------------------ 5. agent wiring
    @staticmethod
    def create_context_provider(knowledge_base_name: str, search_endpoint: str, credential):
        """Return an Agent Framework context provider for the knowledge base.
 
        `credential` should be an async credential (azure.identity.aio.DefaultAzureCredential).
        Use it with: Agent(..., context_providers=[provider]) and `async with provider:`.
        """
 
        return AzureAISearchContextProvider(
            endpoint=search_endpoint,
            knowledge_base_name=knowledge_base_name,
            credential=credential,
            mode="agentic",
        )
 
    # ------------------------------------------------------------------ convenience
    def setup(
        self,
        file_path: str | Path,
        knowledge_source_name: str,
        knowledge_base_name: str,
        description: str,
    ) -> str:
        """Run steps 1–4 in order. Returns the knowledge base name."""
        self.upload_blob(file_path)
        self.create_blob_knowledge_source(knowledge_source_name, description)
        self.wait_for_ingestion(knowledge_source_name)
        return self.create_knowledge_base(knowledge_base_name, [knowledge_source_name], description=description)