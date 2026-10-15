# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`README.md` is the detailed reference: infrastructure, every agent's behaviour rules, tools, the evaluation and clean-up. Keep it in sync when behaviour changes. This file covers what you need to work on the code.

`CHANGELOG.md` is the project log: why things changed, the decisions taken and what is still open. Before you start, read its latest entries. After a change to behaviour, configuration, infrastructure or the evaluation, add an entry at the top in the format given there.

## What this is

A travel-planning assistant on **Azure AI Foundry Agent Service**, built in two variants so they can be compared:

- **Multi-agent (Magentic)**: `managerAgent` (`gpt-5.6-luna`, no tools) plus four specialists on `gpt-5-mini`, each with exactly one tool: `ragDataAgent` (Foundry IQ MCP `knowledge_base_retrieve`), `webDataAgent` (`web_search`), `weatherAgent` (`get_forecast_weather`) and `hotelAgent` (`search_hotels_google_maps`). This is the variant the chat uses.
- **Single agent**: `travelAgent` in `agents/single_agent/`, with all four tools in one prompt. Only the evaluation uses it.

## Commands

The venv is `tutorials\` (git-ignored). The shell commands in `scripts/*.sh` use PowerShell backtick line continuation, despite the `.sh` extension.

```powershell
tutorials\Scripts\activate
pip install -r requirements.txt

python rag_setup.py                                  # upload the guide, build the knowledge source and knowledge base (needs SOURCE_FILE_PATH)
python agents/<name>_agent_deployment.py             # create or update the RAI policy and a new agent version
python agents/single_agent/travel_agent_deployment.py
python agent_execution.py                            # interactive chat (Magentic team)
python magentic_exe_and_eval.py                      # evaluate the team, report goes to outputs/
python single_agent_exe_and_eval.py                  # evaluate travelAgent
```

- Run the agent scripts **by path**. `python -m agents.…` breaks because the `openai-agents` package is also imported as `agents`. The scripts in `agents/` add the project root to `sys.path` themselves.
- There is no test suite, linter or build. The "tests" are the evaluation scripts. They call real models and the Foundry judge, so each case costs money. To run a subset, cut down `inputs/evaluation_prompts.yaml` or set `evaluation.evaluate_agents: false` in `config.yaml`.
- Every script needs only `KEY_VAULT_URL` (set in the git-ignored `.vscode/launch.json`) and `az login`. All other settings and keys are Key Vault secrets. A secret name is the setting name with `-` instead of `_` (for example `PROJECT-ENDPOINT`). The Python code never reads `.env`; `.env` holds only the two third-party keys passed to the Bicep deployment.

## Architecture

- **Two halves of every agent.** The server-side definition (system prompt, tool schema, RAI policy) is created by `agents/<agent>_deployment.py` from `agents/<agent>_config.yaml` plus the `guardrails` section of `config.yaml`. The client side (`FoundryAgent` objects, Magentic workflow, middleware, local tool functions) lives in the run scripts. Changing a prompt or a tool schema means running the deployment script again, because the deployment creates a new agent version.
- **Orchestration runs on the client.** `MagenticBuilder` (Microsoft Agent Framework) calls each Foundry agent through `FoundryAgent`. The agents never call each other. A new workflow is built for every user message, so there is no memory between messages.
- **Function tools run locally.** `get_forecast_weather` and `search_hotels_google_maps` are `@tool` functions in `classes/weather_services.py` and `classes/google_services.py`. They are passed as `tools=[...]` to the `FoundryAgent`, which runs them when the server requests the call. MCP and web search run server-side. Tool functions return `{"error": ...}` instead of raising exceptions, so the agent can react to the error.
- **Middleware** (`utils/agent_middleware.py`) is attached to every agent. `drop_foreign_tool_contents` is required: without it, Magentic forwards other agents' tool calls and results, and Foundry rejects them with "No tool call found for function call output". `BlockedReplyMonitor` detects refusals and output-side content-filter cuts, which arrive without an error. `TokenUsageMiddleware` (`classes/evaluate_services.py`) is the only place that sees the manager's token usage.
- **`add_todays_date`** (`utils/utils.py`) puts today's date in front of every task. The agents need it to resolve relative dates and the 15-day forecast window.
- **Classes** in `classes/` are thin service wrappers. Each takes its secrets through `SecretManager` (Key Vault with an in-memory cache).

## Things defined in more than one place (keep in sync)

- **Team wiring** (`FoundryAgent` names, descriptions, tools and middleware, and the Magentic limits) is duplicated in `agent_execution.py` and `magentic_exe_and_eval.py`. The single-agent wiring is in `single_agent_exe_and_eval.py`.
- **Agent names**: `agent_name` in `agents/*_config.yaml`, the `FoundryAgent` names in the run scripts, the *Team* section of the manager's prompt, `expected_agents` / `unexpected_agents` in `inputs/evaluation_prompts.yaml`, and the keys of `evaluation.agent_evaluators` in `config.yaml` (a missing agent is silently not evaluated).
- **Function tool schemas**: the JSON `FunctionTool` schemas in `agents/weather_agent_deployment.py`, `agents/hotel_agent_deployment.py` and `agents/single_agent/travel_agent_deployment.py` must match the parameter names of the Python `@tool` functions.
- **Answer word limit**: the manager and single-agent system prompts, and `evaluation.max_answer_words` in `config.yaml`.
- **Infrastructure names**: `knowledgeBaseName` in `resource_deployment.bicep` must match the knowledge base that `rag_setup.py` creates. Key Vault secret names are defined in the Bicep template and read by the scripts.

`get_current_weather` is legacy: no agent uses it.
