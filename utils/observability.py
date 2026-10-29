"""Tracing of the agents to Application Insights (OpenTelemetry), shared by the scripts that run them."""

from agent_framework.observability import create_resource
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider


async def enable_tracing(agent, config: dict, script: str) -> bool:
    """Send the traces of this process to Application Insights. Returns True when tracing is on.

    FoundryAgent.configure_azure_monitor() reads the connection string from the Application Insights
    connection of the Foundry project (resource_deployment.bicep) and sets up OpenTelemetry for the whole
    process, so one call on any FoundryAgent covers every agent, tool call and workflow step.
    `script` picks the service name from observability.service_names in config.yaml, which keeps the chat
    apart from the evaluation runs. Tracing never stops a run: when it can't be set up, the run goes on without it.
    """
    settings = config["observability"]
    if not settings["enabled"]:
        print("[TRACING] Off (observability.enabled in config.yaml).")
        return False

    service_name = settings["service_names"][script]
    try:
        await agent.configure_azure_monitor(
            enable_sensitive_data=settings["enable_sensitive_data"],
            resource=create_resource(service_name=service_name),
        )
    except Exception as error:
        # For example azure-monitor-opentelemetry is not installed, or the connection can't be read
        print(f"[TRACING] Not enabled: {error}")
        return False

    # Without a project connection configure_azure_monitor() only logs a warning and sets nothing up
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        print("[TRACING] Not enabled: the Foundry project has no Application Insights connection.")
        return False

    print(f"[TRACING] Traces go to Application Insights as '{service_name}'.")
    return True
