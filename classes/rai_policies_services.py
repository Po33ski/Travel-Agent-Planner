import os
import sys
import yaml
from pathlib import Path

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

        Returns the full ARM Resource ID of the created policy.
        """

        policy_name = config["guardrails"]["rai_policy_name"]

        print(f"🛡️ Defining and deploying RAI Policy: '{policy_name}'...")

        mgmt_client = CognitiveServicesManagementClient(
            credential=self.credential, subscription_id=self.subscription_id
        )

        content_filters = []

        # Add Jailbreak Protection
        content_filters.append(
            RaiPolicyContentFilter(
                name="Jailbreak",
                source=RaiPolicyContentSource.PROMPT,
                blocking=config["guardrails"]["controls"]["jailbreak"]["action"] == "Block",
                enabled=config["guardrails"]["controls"]["jailbreak"]["enabled"],
            )
        )

        # Add Indirect Prompt Injection
        content_filters.append(
            RaiPolicyContentFilter(
                # name="Indirect Prompt Injection", # Not supported
                name="Indirect Attack",
                # name="Prompt Shield", # Not supported
                source=RaiPolicyContentSource.PROMPT,
                blocking=config["guardrails"]["controls"]["indirect_prompt_injections"][
                    "action"
                ]
                == "Block",
                enabled=config["guardrails"]["controls"]["indirect_prompt_injections"][
                    "enabled"
                ],
            ),
        )

        # Add Content Harms filters (Hate, Sexual, Self-Harm, Violence) based on the configuration
        content_filters.extend(
            [
                RaiPolicyContentFilter(
                    name="Hate",
                    source=RaiPolicyContentSource.PROMPT,
                    severity_threshold={
                        "High": ContentLevel.LOW,
                        "Medium": ContentLevel.MEDIUM,
                        "Low": ContentLevel.HIGH,
                    }.get(config["guardrails"]["controls"]["content_harms"]["hate"]),
                    blocking=config["guardrails"]["controls"]["content_harms"]["action"]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["content_harms"]["enabled"],
                ),
                RaiPolicyContentFilter(
                    name="Hate",
                    source=RaiPolicyContentSource.COMPLETION,
                    severity_threshold={
                        "High": ContentLevel.LOW,
                        "Medium": ContentLevel.MEDIUM,
                        "Low": ContentLevel.HIGH,
                    }.get(config["guardrails"]["controls"]["content_harms"]["hate"]),
                    blocking=config["guardrails"]["controls"]["content_harms"]["action"]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["content_harms"]["enabled"],
                ),
                RaiPolicyContentFilter(
                    name="Sexual",
                    source=RaiPolicyContentSource.PROMPT,
                    severity_threshold={
                        "High": ContentLevel.LOW,
                        "Medium": ContentLevel.MEDIUM,
                        "Low": ContentLevel.HIGH,
                    }.get(config["guardrails"]["controls"]["content_harms"]["sexual"]),
                    blocking=config["guardrails"]["controls"]["content_harms"]["action"]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["content_harms"]["enabled"],
                ),
                RaiPolicyContentFilter(
                    name="Sexual",
                    source=RaiPolicyContentSource.COMPLETION,
                    severity_threshold={
                        "High": ContentLevel.LOW,
                        "Medium": ContentLevel.MEDIUM,
                        "Low": ContentLevel.HIGH,
                    }.get(config["guardrails"]["controls"]["content_harms"]["sexual"]),
                    blocking=config["guardrails"]["controls"]["content_harms"]["action"]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["content_harms"]["enabled"],
                ),
                RaiPolicyContentFilter(
                    name="Selfharm",
                    source=RaiPolicyContentSource.PROMPT,
                    severity_threshold={
                        "High": ContentLevel.LOW,
                        "Medium": ContentLevel.MEDIUM,
                        "Low": ContentLevel.HIGH,
                    }.get(config["guardrails"]["controls"]["content_harms"]["self_harm"]),
                    blocking=config["guardrails"]["controls"]["content_harms"]["action"]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["content_harms"]["enabled"],
                ),
                RaiPolicyContentFilter(
                    name="Selfharm",
                    source=RaiPolicyContentSource.COMPLETION,
                    severity_threshold={
                        "High": ContentLevel.LOW,
                        "Medium": ContentLevel.MEDIUM,
                        "Low": ContentLevel.HIGH,
                    }.get(config["guardrails"]["controls"]["content_harms"]["self_harm"]),
                    blocking=config["guardrails"]["controls"]["content_harms"]["action"]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["content_harms"]["enabled"],
                ),
                RaiPolicyContentFilter(
                    name="Violence",
                    source=RaiPolicyContentSource.PROMPT,
                    severity_threshold={
                        "High": ContentLevel.LOW,
                        "Medium": ContentLevel.MEDIUM,
                        "Low": ContentLevel.HIGH,
                    }.get(config["guardrails"]["controls"]["content_harms"]["violence"]),
                    blocking=config["guardrails"]["controls"]["content_harms"]["action"]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["content_harms"]["enabled"],
                ),
                RaiPolicyContentFilter(
                    name="Violence",
                    source=RaiPolicyContentSource.COMPLETION,
                    severity_threshold={
                        "High": ContentLevel.LOW,
                        "Medium": ContentLevel.MEDIUM,
                        "Low": ContentLevel.HIGH,
                    }.get(config["guardrails"]["controls"]["content_harms"]["violence"]),
                    blocking=config["guardrails"]["controls"]["content_harms"]["action"]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["content_harms"]["enabled"],
                ),
            ]
        )

        # Add Profanity Filter (Prompt & Completion)
        content_filters.extend(
            [
                RaiPolicyContentFilter(
                    name="Profanity",
                    source=RaiPolicyContentSource.PROMPT,
                    blocking=config["guardrails"]["controls"]["profanity"]["action"]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["profanity"]["enabled"],
                ),
                RaiPolicyContentFilter(
                    name="Profanity",
                    source=RaiPolicyContentSource.COMPLETION,
                    blocking=config["guardrails"]["controls"]["profanity"]["action"]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["profanity"]["enabled"],
                ),
            ]
        )

        # Add protected material filters for both text and code
        content_filters.extend(
            [
                RaiPolicyContentFilter(
                    name="Protected Material Text",
                    source=RaiPolicyContentSource.PROMPT,
                    blocking=config["guardrails"]["controls"]["protected_materials"][
                        "action"
                    ]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["protected_materials"]["text"],
                ),
                RaiPolicyContentFilter(
                    name="Protected Material Text",
                    source=RaiPolicyContentSource.COMPLETION,
                    blocking=config["guardrails"]["controls"]["protected_materials"][
                        "action"
                    ]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["protected_materials"]["text"],
                ),
                RaiPolicyContentFilter(
                    name="Protected Material Code",
                    source=RaiPolicyContentSource.PROMPT,
                    blocking=config["guardrails"]["controls"]["protected_materials"][
                        "action"
                    ]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["protected_materials"]["code"],
                ),
                RaiPolicyContentFilter(
                    name="Protected Material Code",
                    source=RaiPolicyContentSource.COMPLETION,
                    blocking=config["guardrails"]["controls"]["protected_materials"][
                        "action"
                    ]
                    == "Block",
                    enabled=config["guardrails"]["controls"]["protected_materials"]["code"],
                ),
            ]
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
        deployed_policy = mgmt_client.rai_policies.create_or_update(
            resource_group_name=self.resource_group_name,
            account_name=self.account_name,
            rai_policy_name=policy_name,
            rai_policy=rai_policy,
        )

        print(
            f"✅ RAI Policy deployed successfully. Resource ID:\n   {deployed_policy.id}\n"
        )

        rai_config = RaiConfig(rai_policy_name=deployed_policy.id)

        return rai_config


