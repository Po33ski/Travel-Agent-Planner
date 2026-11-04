"""Tracing of the agents to Application Insights (OpenTelemetry), shared by the scripts that run them."""

from contextlib import contextmanager, nullcontext
from urllib.parse import urlencode, urlsplit

from agent_framework.observability import create_resource
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind, Status, StatusCode

try:
    # Switches off the requests and urllib3 instrumentations for the calls made inside the block
    from opentelemetry.instrumentation.utils import suppress_http_instrumentation
except ImportError:
    # No HTTP instrumentation is installed, so there is nothing to switch off
    suppress_http_instrumentation = nullcontext

REDACTED = "REDACTED"


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


@contextmanager
def http_span_without_secrets(method: str, url: str, params: dict, secret_params: set[str]):
    """HTTP client span for a call that sends a secret as a query parameter, such as `?key=<API key>`.

    The Azure Monitor distro traces every requests / urllib3 call with its full URL and redacts only a fixed
    list of parameters (AWS and Google signatures), so an API key in the query string would be stored in
    plain text. Inside this block those two instrumentations are switched off, and this span records the
    URL with the secret parameters replaced by REDACTED. It records the type of an exception, never its
    message: a urllib3 connection error quotes the URL with its query string. Set the status code with
    span.set_attribute("http.response.status_code", ...). Without tracing the tracer does nothing.
    """
    safe_params = {name: REDACTED if name in secret_params else value for name, value in params.items()}
    host = urlsplit(url).hostname
    with trace.get_tracer(__name__).start_as_current_span(
        f"{method} {host}",
        kind=SpanKind.CLIENT,
        # The attributes the Azure Monitor exporter reads for an HTTP dependency (AppDependencies, type HTTP)
        attributes={
            "http.request.method": method,
            "url.full": f"{url}?{urlencode(safe_params)}",
            "server.address": host,
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        with suppress_http_instrumentation():
            try:
                yield span
            except Exception as error:
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(Status(StatusCode.ERROR))
                raise
