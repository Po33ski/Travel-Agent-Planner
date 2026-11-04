from azure.identity import DefaultAzureCredential
from azure.ai.projects.models import RaiConfig


from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from azure.mgmt.cognitiveservices.models import (
    RaiPolicy,
    RaiPolicyProperties,
    RaiPolicyContentFilter,
    ContentLevel,
    RaiPolicyContentSource,
    RaiPolicyMode,
)

# config.yaml severity value -> severityThreshold sent to Azure (see README, "Severity levels for content harms")
SEVERITY_THRESHOLDS = {
    "High": ContentLevel.LOW,
    "Medium": ContentLevel.MEDIUM,
    "Low": ContentLevel.HIGH,
}

# Content harm filter name in the policy -> its key under content_harms in config.yaml
CONTENT_HARMS = {
    "Hate": "hate",
    "Sexual": "sexual",
    "Selfharm": "self_harm",
    "Violence": "violence",
}

BOTH_SOURCES = (RaiPolicyContentSource.PROMPT, RaiPolicyContentSource.COMPLETION)


class RaiPolicyManager:
    def __init__(self, subscription_id: str, resource_group_name: str, account_name: str, credential: DefaultAzureCredential):
        self.subscription_id = subscription_id
        self.resource_group_name = resource_group_name
        self.account_name = account_name
        self.credential = credential
        self.mgmt_client = CognitiveServicesManagementClient(
            credential=self.credential, subscription_id=self.subscription_id
        )

    def create_or_update_rai_policy(
        self,
        config: dict,
    ) -> RaiConfig:
        """Creates or updates a custom RAI Policy programmatically using the Azure Management SDK.

        Returns a RaiConfig that refers to the policy by its full ARM resource ID, ready to attach to an agent.
        Raises ValueError when a content harm severity in config.yaml is not Low, Medium or High.
        """

        policy_name = config["guardrails"]["rai_policy_name"]
        controls = config["guardrails"]["controls"]

        print(f"🛡️ Defining and deploying RAI Policy: '{policy_name}'...")

        def content_filter(name, control, source, severity=None, enabled=None):
            return RaiPolicyContentFilter(
                name=name,
                source=source,
                severity_threshold=severity,
                blocking=control["action"] == "Block",
                enabled=control["enabled"] if enabled is None else enabled,
            )

        content_filters = [
            # Prompt Shields check the input only
            content_filter("Jailbreak", controls["jailbreak"], RaiPolicyContentSource.PROMPT),
            # "Indirect Prompt Injection" and "Prompt Shield" are not supported names
            content_filter("Indirect Attack", controls["indirect_prompt_injections"], RaiPolicyContentSource.PROMPT),
        ]

        # Content harms (Hate, Sexual, Self-Harm, Violence) on prompt and completion
        harms = controls["content_harms"]
        for name, key in CONTENT_HARMS.items():
            severity = SEVERITY_THRESHOLDS.get(harms[key])
            if severity is None:
                raise ValueError(
                    f"Invalid severity '{harms[key]}' for content_harms.{key} in config.yaml; "
                    f"use one of: {', '.join(SEVERITY_THRESHOLDS)}"
                )
            content_filters.extend(content_filter(name, harms, source, severity) for source in BOTH_SOURCES)

        # Profanity on prompt and completion
        content_filters.extend(
            content_filter("Profanity", controls["profanity"], source) for source in BOTH_SOURCES
        )

        # Protected material (text) on prompt and completion; protected_materials has "text" instead of "enabled"
        protected = controls["protected_materials"]
        content_filters.extend(
            content_filter("Protected Material Text", protected, source, enabled=protected["text"])
            for source in BOTH_SOURCES
        )

        # Create the RAI Policy object with the defined content filters
        rai_policy = RaiPolicy(
            properties=RaiPolicyProperties(
                mode=RaiPolicyMode.BLOCKING,
                base_policy_name="Microsoft.Default",
                content_filters=content_filters,
            )
        )

        # Deploy the policy resource via Azure Management REST endpoint
        deployed_policy = self.mgmt_client.rai_policies.create_or_update(
            resource_group_name=self.resource_group_name,
            account_name=self.account_name,
            rai_policy_name=policy_name,
            rai_policy=rai_policy,
        )

        print(
            f"✅ RAI Policy deployed successfully. Resource ID:\n   {deployed_policy.id}\n"
        )

        return RaiConfig(rai_policy_name=deployed_policy.id)
