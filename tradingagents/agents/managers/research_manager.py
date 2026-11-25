"""Research Manager: turns the bull/bear debate into a structured investment plan for the trader.

Two paths share one prompt and one renderer:

- mode ``off`` keeps the baseline binding and the baseline helper, byte-for-byte (INV-7);
- an enhanced mode validates the actual state before any model call, binds the **strict**
  schema, and raises ``AgentContractError`` on an unrecovered output (ADR-001 sections
  2.3-2.6). The record of what happened is kept for the run's contract summary.
"""

from __future__ import annotations

from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.contracts import (
    RESEARCH_MANAGER,
    active_mode,
    bind_strict,
    build_repair_prompt,
    contract_state_update,
    run_agent_contract,
    validate_input,
)
from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_research_manager(llm):
    def research_manager_node(state) -> dict:
        mode = active_mode()
        if mode is None:
            structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")
        else:
            # Input contract first: an unreadable state raises at INPUT with zero model calls
            # and no state indexing below (FR-7, INV-3).
            validate_input(RESEARCH_MANAGER, state, mode)
            structured_llm = bind_strict(llm, RESEARCH_MANAGER)

        instrument_context = get_instrument_context_from_state(state)
        history = state["investment_debate_state"].get("history", "")

        investment_debate_state = state["investment_debate_state"]

        prompt = f"""As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position

The debate always contains conflicting arguments; deciding which side is stronger is the job, so conflict alone is not a reason to Hold. Commit to the side with the stronger case, sized by how decisively it wins. Choose Hold only when the evidence is still balanced after that weighing, or too thin to support a call; do not manufacture a direction to appear decisive. Weigh the bull and bear cases on their merits, independent of which side spoke first or last.

---

**Debate History:**
{history}

## Output

Write these sections, in this order, starting with the recommendation on its own line:

- **Recommendation**: exactly one of Buy / Overweight / Hold / Underweight / Sell
- **Rationale**: which arguments decided it
- **Strategic Actions**: concrete steps for the trader, sized against a standard allocation

{NO_EXTERNAL_TOOLS}""" + get_language_instruction()

        if mode is None:
            investment_plan = invoke_structured_or_freetext(
                structured_llm,
                llm,
                prompt,
                render_research_plan,
                "Research Manager",
            )
        else:
            # Invalid input costs zero model calls; an unrecovered output raises the typed
            # abort before anything is written to state (FR-1, FR-8).
            invocation = run_agent_contract(
                agent=RESEARCH_MANAGER, state=state, structured_llm=structured_llm,
                plain_llm=llm, prompt=prompt,
                repair_prompt=lambda issues: build_repair_prompt(
                    prompt, issues,
                    "recommendation on its own line exactly as one of Buy / Overweight / "
                    "Hold / Underweight / Sell, plus non-empty rationale and strategic actions",
                ),
            )
            investment_plan = invocation.rendered

        new_investment_debate_state = {
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        update = {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }
        if mode is not None:
            # The saved record lets a resumed run show what this agent did (review E).
            update["contract_records"] = contract_state_update(state.get("contract_records"))
        return update

    return research_manager_node
