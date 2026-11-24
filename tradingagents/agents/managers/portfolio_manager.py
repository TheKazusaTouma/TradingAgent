"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call. Its rating is the run's
``final_rating``, and the decision is rendered to markdown as
``final_trade_decision`` for the memory log, CLI display and saved reports.
When a provider does not expose structured output, the agent falls back to
free-text generation and the rating is read from that text.
"""

from __future__ import annotations

from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
)
from tradingagents.agents.contracts import (
    PORTFOLIO_MANAGER,
    StrictPortfolioDecision,
    active_mode,
    bind_strict,
    build_repair_prompt,
    contract_state_update,
    require_identity,
    run_agent_contract,
    validate_input,
)
from tradingagents.agents.rating import parse_rating
from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS, bind_structured, invoke_structured


def create_portfolio_manager(llm, memory_reader=None):
    """Build a manager that can refresh reviewed memory before composing its prompt."""
    def portfolio_manager_node(state) -> dict:
        mode = active_mode()
        if mode is None:
            structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")
        else:
            # Input contract first: both saved plans are revalidated before any state
            # indexing or prompt binding, so a stale plan costs zero model calls (FR-7,
            # INV-10). The identity is read by this node itself (review item C).
            validate_input(PORTFOLIO_MANAGER, state, mode)
            require_identity(PORTFOLIO_MANAGER, state, mode)
            structured_llm = bind_strict(llm, PORTFOLIO_MANAGER)
        instrument_context = get_instrument_context_from_state(state)
        portfolio_context = get_portfolio_context_from_state(state)

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        read_note = ""
        if memory_reader is not None:
            past_context, read_note = memory_reader(
                state["company_of_interest"], state["trade_date"]
            )
        else:
            past_context = state.get("past_context", "")
        memory_note = " ".join(
            part for part in (state.get("memory_note", ""), read_note) if part
        )
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

{portfolio_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Ground every conclusion in specific evidence from the analysts. The risk debate always contains conflicting stances; deciding which is stronger is the job, so conflict alone is not a reason to Hold. Commit to the stronger case, sized by how decisively it wins. Choose Hold only when the evidence is still balanced after that weighing, or too thin to support a call; do not force a direction to appear decisive. Weigh the analysts on their merits, independent of speaking order.

## Output

Write these sections, in this order, starting with the rating on its own line:

- **Rating**: exactly one of Buy / Overweight / Hold / Underweight / Sell
- **Executive Summary**: the call and how to act on it
- **Investment Thesis**: the evidence that decided it, and what would change it

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""

        # The typed rating is the decision; the rendered text only carries it.
        # Read back from text, a rating the thesis quotes could replace it.
        if mode is None:
            decision = invoke_structured(structured_llm, prompt, "Portfolio Manager")
            if decision is not None:
                final_trade_decision = render_pm_decision(decision)
                final_rating = decision.rating.value
            else:
                final_trade_decision = llm.invoke(prompt).content
                final_rating = parse_rating(final_trade_decision)
        else:
            # The typed rating is the decision; the rendered text only carries it. An
            # unrecovered output raises before any decision reaches state (FR-8, INV-2).
            invocation = run_agent_contract(
                agent=PORTFOLIO_MANAGER, state=state, structured_llm=structured_llm,
                plain_llm=llm, prompt=prompt,
                repair_prompt=lambda issues: build_repair_prompt(
                    prompt, issues,
                    "the rating on its own line exactly as Buy / Overweight / Hold / "
                    "Underweight / Sell, plus non-empty executive summary and investment thesis",
                ),
            )
            final_trade_decision = invocation.rendered
            # ``run_agent_contract`` validates the payload against the strict PM schema, so
            # this holds; the check states the invariant instead of merely asserting it.
            decision_payload = invocation.payload
            if not isinstance(decision_payload, StrictPortfolioDecision):
                raise TypeError("a Portfolio Manager payload must be a validated StrictPortfolioDecision")
            final_rating = decision_payload.rating.value

        new_risk_debate_state = {
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        update = {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
            "final_rating": final_rating,
            "past_context": past_context,
            "memory_note": memory_note,
        }
        if mode is not None:
            update["contract_records"] = contract_state_update(state.get("contract_records"))
        return update
    return portfolio_manager_node
