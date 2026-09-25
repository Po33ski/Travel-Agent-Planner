//https://github.com/microsoft-foundry/foundry-samples/blob/main/infrastructure/infrastructure-setup-bicep/00-basic/main.bicep

param projectPrefix string
param aiFoundryName string = projectPrefix
param aiProjectName string = '${aiFoundryName}-proj'
param llmModelDeploymentName string = '${projectPrefix}-llm-deploy'
param embeddingModelDeploymentName string = '${projectPrefix}-embedding-deploy'
param aiSearchName string = '${projectPrefix}-aisearch'
// Storage account names: 3-24 lowercase letters and digits, globally unique
param storageName string = take(toLower(replace('${projectPrefix}st', '-', '')), 24)
param blobContainerName string = 'travel-guide'
// Must match the knowledge base created by rag_setup.py
param knowledgeBaseName string = 'travel-guide-kb'
param knowledgeBaseConnectionName string = '${knowledgeBaseName}-mcp'
// Object ID of the user who runs rag_setup.py: az ad signed-in-user show --query id -o tsv
param userPrincipalId string
param location string = resourceGroup().location

var knowledgeBaseApiVersion = '2026-08-01-preview'

// Built-in role definition IDs
var roles = {
  storageBlobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
  searchIndexDataReader: '1407120a-92aa-4202-b7e9-c0e197c71c8f'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
}

/*
  An AI Foundry resources is a variant of a CognitiveServices/account resource type that is specifically designed
   to host AI workloads and provide a seamless experience for deploying and managing AI models and assets.
*/
resource aiFoundry 'Microsoft.CognitiveServices/accounts@2026-05-01' = {
  name: aiFoundryName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  kind: 'AIServices'
  properties: {
    // required to work in AI Foundry
    allowProjectManagement: true

    // Defines developer API endpoint subdomain
    customSubDomainName: aiFoundryName

    disableLocalAuth: false
  }
}

/*
  An AI Project is a logical container for assets such as models, deployments, etc. within the AI Foundry.
  It is required to create an AI Project before deploying any models or other assets.
*/
resource aiProject 'Microsoft.CognitiveServices/accounts/projects@2026-05-01' = {
  name: aiProjectName
  parent: aiFoundry
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

/*
  Deploying a model to the AI Foundry is done through a deployment resource.
  In this example, we are deploying the 'gpt-5-mini' model,
  which is a variant of GPT-5 optimized for lower latency and
  cost while still providing strong performance for many use cases.
*/
resource llmModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2026-05-01'= {
  parent: aiFoundry
  name: llmModelDeploymentName
  dependsOn: [
    aiProject
  ]
  sku : {
    capacity: 50 // Rate limit in thousands of tokens per minute (50 = 50K TPM).
    name: 'GlobalStandard'
  }
  properties: {
    model:{
      name: 'gpt-5-mini'
      format: 'OpenAI'
      version: '2025-08-07'
    }
  }
}


// https://ai.azure.com/catalog/models/text-embedding-3-small
resource embeddingModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2026-05-01'= {
  parent: aiFoundry
  name: embeddingModelDeploymentName
  dependsOn: [
    llmModelDeployment  // Explicitly wait for LLM to finish first
  ]
  sku : {
    capacity: 50 // 50K TPM, enough for the indexer to vectorise documents without constant throttling
    name: 'GlobalStandard'
  }
  properties: {
    model:{
      name: 'text-embedding-3-small' // 1536 dimensions, good for semantic search and embedding use cases
      format: 'OpenAI'
      version: '1'
    }
  }
}

// ------------------------------------------------------------------ Azure AI Search
resource searchService 'Microsoft.Search/searchServices@2025-05-01' = {
  name: aiSearchName
  location: location
  sku: { name: 'free' } // no fixed cost; services connect with keys, so no managed identity is needed
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'Default'
    publicNetworkAccess: 'enabled'
    semanticSearch: 'free' // semantic ranker is used by agentic retrieval; free plan returns errors instead of charges
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge' // Entra ID for the user, API keys for the services
      }
    }
  }
}

// ------------------------------------------------------------------ Storage (blob knowledge source)
resource storage 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true // Search indexer reads the container with the account key
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' = {
  parent: storage
  name: 'default'
}

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  parent: blobService
  name: blobContainerName
  properties: { publicAccess: 'None' }
}

// ------------------------------------------------------------------ project connection to the knowledge base (MCP)
// Stores only the URL and key, so it can be created before the knowledge base exists.
var knowledgeBaseMcpEndpoint = 'https://${searchService.name}.search.windows.net/knowledgebases/${knowledgeBaseName}/mcp?api-version=${knowledgeBaseApiVersion}'

// Key-based MCP connection: the agent sends the Search query key (read-only) in the api-key header.
// The key is read from the search service during deployment, so it never appears in code or outputs.
resource kbConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-10-01-preview' = {
  parent: aiProject
  name: knowledgeBaseConnectionName
  properties: {
    category: 'RemoteTool'
    authType: 'CustomKeys'
    target: knowledgeBaseMcpEndpoint
    isSharedToAll: true
    credentials: {
      keys: {
        'api-key': searchService.listQueryKeys().value[0].key
      }
    }
    metadata: { type: 'generic_mcp' }
  }
}

// ------------------------------------------------------------------ role assignments: service → service
// Search reads documents from the blob container
// roles asigned to the search service's managed identity, allowing it to read blob data from the storage account. 
// ONLY IN THE CASE OF USE MANAGED IDENTITY AUTHENTICATION. If you are using shared key or SAS token authentication, this role assignment is not necessary.
// resource searchToStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
//   scope: storage
//   name: guid(storage.id, searchService.id, roles.storageBlobDataReader)
//   properties: {
//     principalId: searchService.identity.principalId
//     principalType: 'ServicePrincipal'
//     roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataReader)
//   }
// }

// // Search calls the embedding and chat models (ingestion, query planning)
// resource searchToFoundry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
//   scope: aiFoundry
//   name: guid(aiFoundry.id, searchService.id, roles.cognitiveServicesUser)
//   properties: {
//     principalId: searchService.identity.principalId
//     principalType: 'ServicePrincipal'
//     roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesUser)
//   }
// }

// // Agent (project managed identity) queries the knowledge base over MCP
// resource projectToSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
//   scope: searchService
//   name: guid(searchService.id, aiProject.id, roles.searchIndexDataReader)
//   properties: {
//     principalId: aiProject.identity.principalId
//     principalType: 'ServicePrincipal'
//     roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataReader)
//   }
// }

// ------------------------------------------------------------------ role assignments: user → service (rag_setup.py)
// The user's own calls use Entra ID (DefaultAzureCredential); Owner has no data-plane rights, so these are needed.
// Upload files to the container
resource userToStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, userPrincipalId, roles.storageBlobDataContributor)
  properties: {
    principalId: userPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataContributor)
  }
}

// Create knowledge sources and knowledge bases
resource userToSearchService 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: searchService
  name: guid(searchService.id, userPrincipalId, roles.searchServiceContributor)
  properties: {
    principalId: userPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchServiceContributor)
  }
}

// Read and write index content (knowledge source status, test queries)
resource userToSearchData 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: searchService
  name: guid(searchService.id, userPrincipalId, roles.searchIndexDataContributor)
  properties: {
    principalId: userPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.searchIndexDataContributor)
  }
}

// ------------------------------------------------------------------ outputs (values for rag_setup.py / launch.json)
output projectEndpoint string = 'https://${aiFoundry.properties.customSubDomainName}.services.ai.azure.com/api/projects/${aiProject.name}'
output aoaiEndpoint string = 'https://${aiFoundry.properties.customSubDomainName}.openai.azure.com'
output llmModelDeploymentName string = llmModelDeployment.name
output embeddingModelDeploymentName string = embeddingModelDeployment.name
output searchEndpoint string = 'https://${searchService.name}.search.windows.net'
output storageAccountUrl string = storage.properties.primaryEndpoints.blob
// Names for the key commands in scripts/01_resource_deployment.sh (keys are never output)
output storageAccountName string = storage.name
output aiFoundryName string = aiFoundry.name
output blobContainerName string = container.name
output knowledgeBaseName string = knowledgeBaseName
output knowledgeBaseConnectionName string = kbConnection.name
