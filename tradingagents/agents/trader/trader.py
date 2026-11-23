"""Trader: turns the Research Manager's investment plan into a concrete transaction proposal."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
)
from tradingagents.agents.contracts import (
    TRADER,
    active_mode,
    bind_strict,
    build_repair_prompt,
    contract_state_update,
    require_identity,
    run_agent_contract,
    validate_input,
)
from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_trader(llm):
    def trader_node(state):
        mode = active_mode()
        if mode is None:
            structured_llm = bind_structured(llm, TraderProposal, "Trader")
        else:
            # Input contract first: an unreadable state raises at INPUT with zero model calls
            # and no state indexing below (FR-7, INV-3). The identity is read by this node
            # itself, so it is checked here with the rest of the input (review item C).
            validate_input(TRADER, state, mode)
            require_identity(TRADER, state, mode)
            structured_llm = bind_strict(llm, TRADER)
        company_name = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)
        investment_plan = state["investment_plan"]
        # The research plan digests the debate but loses exact price structure;
        # give the Trader the technical market report so entry/stop levels are
        # grounded in real ATR / support-resistance / current price (#1167). The
        # report is empty when the user did not select the market analyst, so
        # only offer it (and the grounding instruction) when it has content.
        # The key is optional (ADR-001 section 3): a state without it, or with ``None``
        # or blank, simply has no report and must not surface as a ``KeyError``. A
        # non-text value is still refused by the input contract before this line runs.
        market_report = (state.get("market_report") or "").strip()
        portfolio_context = get_portfolio_context_from_state(state)

        if market_report:
            grounding = (
                "Ground concrete price levels (entry, stop-loss, position sizing) in the technical "
                "market report's price structure -- current price, support/resistance, ATR, and "
                "volatility -- and use the research plan for direction and strategy. "
            )
            report_section = f"Technical Market Report:\n{market_report}\n\n"
        else:
            grounding = ""
            report_section = ""

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a trading agent analyzing market data to make investment decisions. "
                    "Based on your analysis, provide a specific recommendation to buy, sell, or hold. "
                    + grounding
                    # Entry/stop are numeric price fields. Asking for concrete
                    # levels invites a percentage ("15%"), which is not a price
                    # and fails the structured parse (#1288).
                    + "State entry price and stop-loss as absolute price levels in the "
                    "instrument's quote currency (for example 189.5), never a percentage "
                    "or a range; convert a percentage distance to the price level it "
                    "implies, or omit the field if you cannot state a number. "
                    + NO_EXTERNAL_TOOLS
                    + get_language_instruction()
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Here is the research team's investment plan for {company_name}. "
                    f"{instrument_context}\n\n"
                    f"{report_section}"
                    f"{portfolio_context}\n\n"
                    f"Proposed Investment Plan:\n{investment_plan}\n\n"
                    "Make an informed, strategic trading decision.\n\n"
                    "## Output\n\n"
                    "Write these sections, in this order, starting with the action "
                    "on its own line:\n\n"
                    "- **Action**: exactly one of Buy / Hold / Sell. A research "
                    "recommendation of Overweight is a Buy and Underweight is a Sell, "
                    "sized by how strong the case is; conflict alone is not a Hold.\n"
                    "- **Reasoning**: why, against the plan and the price structure\n"
                    "- **Entry Price**, **Stop Loss**, **Position Sizing**: when you can state them"
                ),
            },
        ]

        if mode is None:
            trader_plan = invoke_structured_or_freetext(
                structured_llm,
                llm,
                messages,
                render_trader_proposal,
                "Trader",
            )
        else:
            invocation = run_agent_contract(
                agent=TRADER, state=state, structured_llm=structured_llm, plain_llm=llm,
                prompt=messages,
                repair_prompt=lambda issues: build_repair_prompt(
                    messages, issues,
                    "the action on its own line exactly as Buy / Hold / Sell, plus non-empty reasoning",
                ),
            )
            trader_plan = invocation.rendered

        # A node's state update mixes a message list with string payloads (and, in an
        # enhanced mode, a mapping of contract records), so it is annotated as such rather
        # than letting the first value's type narrow the whole dict.
        update: dict[str, Any] = {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
        }
        if mode is not None:
            update["contract_records"] = contract_state_update(state.get("contract_records"))
        return update

    return trader_node
