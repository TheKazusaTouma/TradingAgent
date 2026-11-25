# Runtime prompt inventory — incomplete

The reviewed TradingAgents source constructs prompts in the agent modules, including:

| Agent | Source entry point | Status |
|---|---|---|
| Research Manager | `tradingagents/agents/managers/research_manager.py` | Existing prompt construction; not externalized by this draft. |
| Trader | `tradingagents/agents/trader/trader.py` | Existing prompt construction; not externalized by this draft. |
| Portfolio Manager | `tradingagents/agents/managers/portfolio_manager.py` | Existing prompt construction; not externalized by this draft. |

`prompt_v1.0.yaml` records a **candidate coding-agent startup prompt**, not an application runtime prompt. Its version number does not mean that previous analysis runs used it. The team must inspect the full set of runtime prompts and the course's detailed requirement sheet, then decide whether runtime prompt versioning and evidence require implementation. No runtime prompt versioning or integration is claimed by this draft.
