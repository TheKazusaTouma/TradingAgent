"""Integration tests for the approved agent I/O increment (T-1, T-15..T-23, T-30).

These run the **real** agent nodes with scripted models and no network: the agents' own
contracts, the risk-debator guards, the graph/CLI fail-closed handling, the checkpoint
signature and the reports are all production code paths. A helper-level test in
``test_agent_contracts.py`` is not a substitute for these.

Every test states its ``agent_contract_mode`` explicitly and drives its own scripted
answers, so nothing depends on another mode or another test having run first.

Scripted answers are keyed by the **schema class** held in ``StrictResearchPlan`` /
``StrictTraderProposal`` / ``StrictPortfolioDecision``, or by the agent label. The model
looks answers up by label, so a script that is not consumed is a bug in the test, not a
silent default: ``AgentModel.served`` records what each lookup actually returned and the
tests assert on it (review item A).
"""

from __future__ import annotations

import copy
from pathlib import Path

import pandas as pd
import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field, ValidationError

from tradingagents.agents import context, contracts, schemas
from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.agents.contracts import (
    AgentContractError,
    StrictPortfolioDecision,
    StrictResearchPlan,
    StrictTraderProposal,
)
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator
from tradingagents.agents.trader.trader import create_trader
from tradingagents.dataflows import config as tradingagents_config, router
from tradingagents.dataflows.vendors.yahoo import market as yahoo_market, snapshot
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import checkpointer, trading_graph

pytestmark = pytest.mark.unit

TRADE_DATE = "2026-01-09"
ARGS = {"symbol": "NVDA", "ticker": "NVDA", "curr_date": TRADE_DATE, "start_date": "2026-01-02",
        "end_date": TRADE_DATE, "indicator": "rsi", "topic": "Fed rate cut", "freq": "quarterly"}

# ---------------------------------------------------------------------------
# Deterministic fixtures
# ---------------------------------------------------------------------------

RM_OK = StrictResearchPlan(recommendation=schemas.PortfolioRating.OVERWEIGHT,
                           rationale="Bulls led on cash flow.", strategic_actions="Scale in.")
TRADER_OK = StrictTraderProposal(action=schemas.TraderAction.BUY, reasoning="Plan supports adding.",
                                 entry_price=189.5, stop_loss=172.0, position_sizing="5%")
PM_OK = StrictPortfolioDecision(rating=schemas.PortfolioRating.OVERWEIGHT,
                                executive_summary="Add on strength.", investment_thesis="Beat.",
                                price_target=210.0, time_horizon="3-6 months")

# Semantic violations: the payload parses but a required narrative is blank. Under
# ``validate`` these abort on the first call with no repair and no recovery (review B).
RM_BAD = StrictResearchPlan(recommendation=schemas.PortfolioRating.OVERWEIGHT,
                            rationale="   ", strategic_actions="Scale in.")
TRADER_BAD = StrictTraderProposal(action=schemas.TraderAction.BUY, reasoning="  ")
PM_BAD = StrictPortfolioDecision(rating=schemas.PortfolioRating.OVERWEIGHT,
                                 executive_summary="", investment_thesis="Beat.")

# The baseline payloads: what a provider returns when ``off`` binds the baseline schema.
RM_BASE = schemas.ResearchPlan(recommendation=schemas.PortfolioRating.OVERWEIGHT,
                               rationale="Bulls led on cash flow.", strategic_actions="Scale in.")
TRADER_BASE = schemas.TraderProposal(action=schemas.TraderAction.BUY, reasoning="Plan supports adding.",
                                     entry_price=189.5, stop_loss=172.0, position_sizing="5%")
PM_BASE = schemas.PortfolioDecision(rating=schemas.PortfolioRating.OVERWEIGHT,
                                    executive_summary="Add on strength.", investment_thesis="Beat.",
                                    price_target=210.0, time_horizon="3-6 months")

SENTIMENT_OK = schemas.SentimentReport(overall_band=schemas.SentimentBand.NEUTRAL,
                                       overall_score=5.0, confidence="low", narrative="n")

# Plain-route (free-text) answers, exactly what a provider writes without a schema.
TEXT_RM = "**Recommendation**: Overweight\n\n**Rationale**: Bulls led.\n\n**Strategic Actions**: Scale in."
TEXT_TRADER = "**Action**: Buy\n\n**Reasoning**: Plan supports adding.\n\nFINAL TRANSACTION PROPOSAL: **BUY**"
TEXT_PM = "**Rating**: Overweight\n\n**Executive Summary**: Add.\n\n**Investment Thesis**: Beat."

STRICT_LABEL = {StrictResearchPlan: "Research Manager", StrictTraderProposal: "Trader",
                StrictPortfolioDecision: "Portfolio Manager",
                schemas.ResearchPlan: "Research Manager", schemas.TraderProposal: "Trader",
                schemas.PortfolioDecision: "Portfolio Manager",
                schemas.SentimentReport: "Sentiment Analyst"}

DEFAULTS = {StrictResearchPlan: RM_OK, StrictTraderProposal: TRADER_OK,
            StrictPortfolioDecision: PM_OK, schemas.ResearchPlan: RM_BASE,
            schemas.TraderProposal: TRADER_BASE, schemas.PortfolioDecision: PM_BASE}

# The opening line each agent's own prompt carries; the Trader's prompt is a list of dicts,
# so the marker has to match its system text.
PROMPT_MARKERS = (
    ("Research Manager", "As the Research Manager"),
    ("Trader", "trading agent analyzing market data"),
    ("Portfolio Manager", "As the Portfolio Manager"),
    ("Aggressive Analyst", "As the Aggressive Risk Analyst"),
    ("Conservative Analyst", "As the Conservative Risk Analyst"),
    ("Neutral Analyst", "As the Neutral Risk Analyst"),
)

TEXT_BY_LABEL = {"Research Manager": TEXT_RM, "Trader": TEXT_TRADER, "Portfolio Manager": TEXT_PM}


def label_of_messages(messages) -> str:
    """The agent a prompt belongs to, from its own opening line."""
    contents = []
    for message in messages:
        content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
        contents.append(content if isinstance(content, str) else str(content))
    text = "\n".join(contents)
    for label, marker in PROMPT_MARKERS:
        if marker in text:
            return label
    return "other"


class ScriptedAnswers:
    """The scripted answers for one test, looked up by schema class or agent label.

    ``ScriptedAnswers(StrictResearchPlan=[RM_BAD])`` registers the script under the agent
    label and the schema, so whichever key the model asks for, the script is found. A
    script list is consumed in order; once it is exhausted the model returns nothing rather
    than falling back to a valid default, so a scripted failure can never silently become a
    success.
    """

    def __init__(self, *pairs, **kwargs):
        self._scripts: dict[str, list] = {}
        for key, value in (*pairs, *kwargs.items()):
            if isinstance(key, type):
                label = STRICT_LABEL.get(key)
            else:
                label = str(key)
                # A keyword argument can only carry the schema's *name*.
                label = STRICT_LABEL.get(next((s for s, schema_label in STRICT_LABEL.items()
                                               if s.__name__ == label), None), label)
            if label is None:
                raise KeyError(f"no agent label registered for {key!r}")
            self._scripts[label] = value if isinstance(value, list) else [value]

    def pop(self, label: str):
        """The next scripted value for ``label``, ``EXHAUSTED`` when its script ran out."""
        script = self._scripts.get(label)
        if script is None:
            return NO_SCRIPT
        return script.pop(0) if script else EXHAUSTED

    def pending(self, label: str) -> int:
        return len(self._scripts.get(label, ()))


NO_SCRIPT = object()
EXHAUSTED = object()


class AgentModel(BaseChatModel):
    """The whole graph's model, scripted per agent, with a per-agent call log.

    ``structured`` selects whether the provider supports structured output. Every structured
    call for one agent is counted in ``calls``; ``served`` records what each answer actually
    was, which is how a test proves its bad fixture was delivered instead of a default.
    """

    structured: bool = False
    supports_tools: bool = False
    tools: tuple = ()
    answers: object = None
    calls: dict = Field(default_factory=dict)   # agent label -> structured call count
    plain_calls: dict = Field(default_factory=dict)  # agent label -> free-text call count
    order: list = Field(default_factory=list)   # agent labels in call order
    served: list = Field(default_factory=list)  # (label, "structured"|"plain", repr of answer)
    seen_prompts: list = Field(default_factory=list)  # prompts in call order

    @property
    def _llm_type(self) -> str:
        return "agent-scripted"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={
            "tools": tuple(tools), "supports_tools": True, "calls": self.calls,
            "plain_calls": self.plain_calls, "order": self.order, "served": self.served,
            "seen_prompts": self.seen_prompts, "answers": self.answers,
        })

    def with_structured_output(self, schema, **kwargs):
        if not self.structured:
            raise NotImplementedError
        return RunnableLambda(lambda prompt: self._structured(schema, prompt))

    def _scripted(self, label: str):
        if self.answers is None:
            return NO_SCRIPT
        return self.answers.pop(label)

    def _structured(self, schema, prompt):
        label = STRICT_LABEL.get(schema, getattr(schema, "__name__", "?"))
        self.calls[label] = self.calls.get(label, 0) + 1
        self.order.append(label)
        self.seen_prompts.append(prompt)
        if schema is schemas.SentimentReport:
            answer = SENTIMENT_OK
        else:
            answer = self._scripted(label)
            if answer is NO_SCRIPT:
                answer = DEFAULTS[schema]
            elif answer is EXHAUSTED:
                answer = None
        self.served.append((label, "structured", repr(answer)))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        label = label_of_messages(messages)
        self.plain_calls[label] = self.plain_calls.get(label, 0) + 1
        self.order.append(label)
        self.seen_prompts.append(messages)
        if self.supports_tools and not isinstance(messages[-1], ToolMessage):
            tool_calls = [{"name": t.name, "id": f"call_{i}",
                           "args": {k: v for k, v in ARGS.items()
                                    if k in t.tool_call_schema.model_json_schema()["properties"]}}
                          for i, t in enumerate(self.tools)]
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=tool_calls))])
        answer = self._scripted(label)
        if answer is NO_SCRIPT:
            answer = TEXT_BY_LABEL.get(label, "Report.")
        elif answer is EXHAUSTED:
            answer = ""
        self.served.append((label, "plain", repr(answer)[:200]))
        if isinstance(answer, Exception):
            raise answer
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=answer))])

    def served_for(self, label: str) -> list[tuple[str, str]]:
        return [(stage, value) for name, stage, value in self.served if name == label]


class _Client:
    def __init__(self, model):
        self.model = model

    def get_llm(self):
        return self.model


@pytest.fixture
def offline(monkeypatch):
    """Every vendor answers offline; the sentiment analyst's HTTP calls are stubbed."""
    for _method, vendors in router.VENDOR_METHODS.items():
        for vendor in vendors:
            monkeypatch.setitem(vendors, vendor, lambda *a, **k: "data")
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda *a, **k: pd.DataFrame({
        "Date": pd.bdate_range(end=TRADE_DATE, periods=60),
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1_000_000}))
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", lambda *a, **k: "no posts")
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *a, **k: "no posts")
    monkeypatch.setattr(yahoo_market.yf, "Ticker", lambda s: type("T", (), {"info": {"longName": "NVIDIA"}})())
    context._identity.cache_clear()


def _graph(tmp_path, monkeypatch, model, **config):
    """The production graph class with its provider clients replaced by ``model``.

    The run config is bound while the graph is built, because the checkpoint signature
    folds in the contract mode the graph starts with.
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
               memory_log_path=str(tmp_path / "log.md"), **config)
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    with tradingagents_config.run_config(cfg):
        return trading_graph.TradingAgentsGraph(config=cfg)


# ---------------------------------------------------------------------------
# A: the fixture itself -- a scripted bad answer is delivered, not defaulted
# ---------------------------------------------------------------------------

def test_scripted_answers_are_really_consumed(tmp_path, monkeypatch, offline):
    """Guard against the fixture bug: a scripted failure must reach the production code."""
    model = AgentModel(structured=True, answers=ScriptedAnswers(StrictResearchPlan=[RM_BAD]))
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate")

    with pytest.raises(AgentContractError) as caught:
        graph.propagate("NVDA", TRADE_DATE)

    assert caught.value.agent == "Research Manager"
    assert "blank" in {issue.code for issue in caught.value.issues}
    # The script found the label, was delivered, and is now spent -- so no default was used.
    assert any("rationale='   '" in value for _stage, value in model.served_for("Research Manager"))
    assert model.answers.pending("Research Manager") == 0


# ---------------------------------------------------------------------------
# T-1 / T-24 across modes -- valid fixtures agree
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,structured", [
    ("off", True), ("off", False), ("validate", True), ("validate", False),
    ("validate+repair", True), ("validate+repair", False),
], ids=["off-structured", "off-freetext", "validate-structured", "validate-freetext",
        "repair-structured", "repair-freetext"])
def test_t1_every_mode_reaches_the_same_decision(tmp_path, monkeypatch, offline, mode, structured):
    model = AgentModel(structured=structured)
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode=mode, checkpoint_enabled=True)

    state, signal = graph.propagate("NVDA", TRADE_DATE)

    assert signal == state["final_rating"] == "Overweight"
    assert "**Rating**: Overweight" in state["final_trade_decision"]
    records = graph.contract_records()
    assert (records == {}) if mode == "off" else ("Portfolio Manager" in records)


# ---------------------------------------------------------------------------
# B: validate aborts on a parsed semantic violation without repairing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["validate", "validate+repair"])
def test_validate_blank_narrative_aborts_with_one_call(tmp_path, monkeypatch, offline, mode):
    """A parsed payload with a blank required field is not a parse failure: validate stops."""
    model = AgentModel(structured=True, answers=ScriptedAnswers(StrictPortfolioDecision=[PM_BAD]))
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode=mode)
    with pytest.raises(AgentContractError) as caught:
        graph.propagate("NVDA", TRADE_DATE)
    calls = caught.value.record.calls
    assert calls.structured == 1
    if mode == "validate":
        assert calls.semantic_repair == 0 and calls.free_text_recovery == 0
        assert model.calls.get("Portfolio Manager") == 1
        assert model.plain_calls.get("Portfolio Manager", 0) == 0
    else:
        assert calls.semantic_repair == 1 and calls.free_text_recovery == 0
        assert calls.logical_total == 2


# ---------------------------------------------------------------------------
# T-15 / T-18 / T-19 -- a failed PM cannot publish, and the run keeps its audit
# ---------------------------------------------------------------------------

def test_t15_failed_pm_publishes_nothing(tmp_path, monkeypatch, offline):
    model = AgentModel(structured=True, answers=ScriptedAnswers(StrictPortfolioDecision=[PM_BAD]))
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate")
    recorded, cleared = [], []
    monkeypatch.setattr(graph, "record_decision", lambda *a, **k: recorded.append(a))
    monkeypatch.setattr(graph, "clear_checkpoint_on_success", lambda *a, **k: cleared.append(a))

    with pytest.raises(AgentContractError):
        graph.propagate("NVDA", TRADE_DATE)

    assert recorded == [] and cleared == []
    assert graph.memory_log.load_entries() == []
    assert model.calls.get("Portfolio Manager") == 1


def test_t18_failure_retains_checkpoint_and_writes_audit(tmp_path, monkeypatch, offline):
    model = AgentModel(structured=True, answers=ScriptedAnswers(StrictPortfolioDecision=[PM_BAD]))
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate", checkpoint_enabled=True)

    with pytest.raises(AgentContractError):
        graph.propagate("NVDA", TRADE_DATE)

    signature = graph._run_signature("stock")
    assert checkpointer.checkpoint_step(str(tmp_path / "cache"), "NVDA", TRADE_DATE, signature) is not None
    audits = list((tmp_path / "results" / "failed_runs").glob("NVDA*.md"))
    assert len(audits) == 1
    text = audits[0].read_text(encoding="utf-8")
    assert "failed" in text.lower()
    assert "Add on strength" not in text and "Beat." not in text


def test_t19_failed_run_has_a_contract_block_and_no_fabricated_proposal(tmp_path, monkeypatch, offline):
    model = AgentModel(structured=True, answers=ScriptedAnswers(StrictPortfolioDecision=[PM_BAD]))
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate")

    with pytest.raises(AgentContractError):
        graph.propagate("NVDA", TRADE_DATE)

    text = next((tmp_path / "results" / "failed_runs").glob("NVDA*.md")).read_text(encoding="utf-8")
    assert "## Contract Validation" in text
    assert "FINAL TRANSACTION PROPOSAL" not in text
    assert "**Rating**:" not in text


# ---------------------------------------------------------------------------
# T-16 / T-17 -- dependent LLMs never run after a failure
# ---------------------------------------------------------------------------

def test_t16_failed_research_manager_stops_every_dependent(tmp_path, monkeypatch, offline):
    model = AgentModel(structured=True, answers=ScriptedAnswers(StrictResearchPlan=[RM_BAD]))
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate")

    with pytest.raises(AgentContractError) as caught:
        graph.propagate("NVDA", TRADE_DATE)

    assert caught.value.agent == "Research Manager"
    for dependent in ("Trader", "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
                      "Portfolio Manager"):
        assert model.calls.get(dependent, 0) == 0, dependent
        assert model.plain_calls.get(dependent, 0) == 0, dependent


def test_t17_failed_trader_stops_the_risk_debate(tmp_path, monkeypatch, offline):
    model = AgentModel(structured=True, answers=ScriptedAnswers(StrictTraderProposal=[TRADER_BAD]))
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate")

    with pytest.raises(AgentContractError) as caught:
        graph.propagate("NVDA", TRADE_DATE)

    assert caught.value.agent == "Trader"
    for dependent in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager"):
        assert model.calls.get(dependent, 0) == 0, dependent
        assert model.plain_calls.get(dependent, 0) == 0, dependent


def test_t17_risk_guard_rejects_a_failure_marker(tmp_path, monkeypatch, offline):
    """Defence in depth: the debator guard rejects a plan the graph never validated."""
    model = AgentModel(structured=True)
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate")
    node = create_aggressive_debator(graph.quick_thinking_llm)
    with pytest.raises(AgentContractError) as caught:
        node({"trader_investment_plan": contracts.failure_marker("Trader", []),
              "company_of_interest": "NVDA",
              "risk_debate_state": {"history": "", "aggressive_history": "",
                                    "current_conservative_response": "", "current_neutral_response": ""},
              "market_report": "", "sentiment_report": "", "news_report": "",
              "fundamentals_report": ""})
    assert "failure_marker_input" in {issue.code for issue in caught.value.issues}
    assert model.calls.get("Aggressive Analyst", 0) == 0


# ---------------------------------------------------------------------------
# T-21 / T-22 / T-23 -- checkpoint signature and resume revalidation
# ---------------------------------------------------------------------------

def test_t21_signature_includes_mode_and_version(tmp_path, monkeypatch, offline):
    off = _graph(tmp_path, monkeypatch, AgentModel(), agent_contract_mode="off")
    enhanced = _graph(tmp_path / "v", monkeypatch, AgentModel(), agent_contract_mode="validate")
    assert off._run_signature("stock") != enhanced._run_signature("stock")
    assert "contract=off" in off._run_signature("stock")
    assert f"contract=validate@{contracts.AGENT_CONTRACT_VERSION}" in enhanced._run_signature("stock")


def test_t21_old_signature_without_a_version_never_resumes(tmp_path, monkeypatch, offline):
    graph = _graph(tmp_path, monkeypatch, AgentModel(), agent_contract_mode="validate",
                   checkpoint_enabled=True)
    legacy_signature = graph._run_signature("stock").replace(
        f"contract=validate@{contracts.AGENT_CONTRACT_VERSION}|", "")
    legacy_thread = checkpointer.thread_id("NVDA", TRADE_DATE, legacy_signature)

    # A genuinely stored checkpoint under the version-less thread id: a real save with the
    # production saver and a real graph, writing the state the legacy run would have left.
    with checkpointer.get_checkpointer(str(tmp_path / "cache"), "NVDA") as saver:
        compiled = graph.workflow.compile(checkpointer=saver)
        compiled.update_state({"configurable": {"thread_id": legacy_thread}},
                              {"investment_plan": "STALE PLAN", "messages": []})
    assert checkpointer.checkpoint_step(str(tmp_path / "cache"), "NVDA", TRADE_DATE,
                                        legacy_signature) is not None

    # The versioned signature is a different thread, so the stale plan cannot be resumed.
    assert checkpointer.checkpoint_step(str(tmp_path / "cache"), "NVDA", TRADE_DATE,
                                        graph._run_signature("stock")) is None
    state, _ = graph.propagate("NVDA", TRADE_DATE)
    assert "STALE PLAN" not in state.get("investment_plan", "")
    assert "STALE PLAN" not in state.get("trader_investment_plan", "")


def test_t23_three_modes_use_independent_fixtures(tmp_path, monkeypatch, offline):
    results = {}
    for mode in ("off", "validate", "validate+repair"):
        model = AgentModel(structured=True)
        state, signal = _graph(tmp_path / mode, monkeypatch, model, agent_contract_mode=mode) \
            .propagate("NVDA", TRADE_DATE)
        results[mode] = (signal, state["final_trade_decision"])
    assert len(set(results.values())) == 1


# ---------------------------------------------------------------------------
# C: the input contract runs before any node state access
# ---------------------------------------------------------------------------

TRADER_STATE_OK = {
    "investment_plan": "**Recommendation**: Overweight\n\n**Rationale**: r\n\n**Strategic Actions**: s",
    "company_of_interest": "NVDA",
}

PM_STATE_OK = {
    "investment_plan": TRADER_STATE_OK["investment_plan"],
    "trader_investment_plan": "**Action**: Buy\n\n**Reasoning**: r",
    "company_of_interest": "NVDA",
    "risk_debate_state": {"history": "Aggressive: add. Conservative: trim.",
                          "aggressive_history": "", "conservative_history": "",
                          "neutral_history": "", "latest_speaker": "",
                          "current_aggressive_response": "",
                          "current_conservative_response": "",
                          "current_neutral_response": "", "count": 2},
}


def _node_for(graph, agent):
    if agent == "Trader":
        return create_trader(graph.quick_thinking_llm)
    if agent == "Portfolio Manager":
        return create_portfolio_manager(graph.quick_thinking_llm)
    return create_research_manager(graph.quick_thinking_llm)


@pytest.mark.parametrize("agent,state", [
    ("Trader", {}),
    ("Trader", {"investment_plan": TRADER_STATE_OK["investment_plan"]}),  # plan present, no identity
    ("Portfolio Manager", {}),
    ("Portfolio Manager", {"investment_plan": PM_STATE_OK["investment_plan"]}),
    ("Trader", dict(TRADER_STATE_OK, investment_plan=contracts.failure_marker("Research Manager", []))),
])
def test_c_malformed_node_input_raises_input_error_with_no_invocation(
        tmp_path, monkeypatch, offline, agent, state):
    """A node handed an unreadable state raises at INPUT and never reaches its model."""
    model = AgentModel(structured=True)
    graph = _graph(tmp_path / agent.replace(" ", "_"), monkeypatch, model,
                   agent_contract_mode="validate")
    node = _node_for(graph, agent)
    with pytest.raises(AgentContractError) as caught:
        node(dict(state))
    assert caught.value.stage == contracts.Stage.INPUT
    assert caught.value.record.calls.logical_total == 0
    assert model.calls.get(agent, 0) == 0 and model.plain_calls.get(agent, 0) == 0


def test_c_optional_reports_may_be_absent_null_or_blank(tmp_path, monkeypatch, offline):
    """Optional context never blocks a node; only required inputs do (T-2, T-3)."""
    for missing in ("absent", "none", "blank"):
        model = AgentModel(structured=True)
        _graph(tmp_path / missing, monkeypatch, model, agent_contract_mode="validate")
        state = dict(TRADER_STATE_OK)
        if missing == "none":
            state.update(dict.fromkeys(("market_report", "sentiment_report", "news_report", "fundamentals_report")))
        elif missing == "blank":
            state.update(dict.fromkeys(("market_report", "sentiment_report", "news_report", "fundamentals_report"), "   "))
        contract = contracts.validate_trader_input(state)
        assert contract == [], (missing, contract)


def test_c_off_mode_still_raises_keyerror_and_makes_no_contract_claim(tmp_path, monkeypatch, offline):
    """Mode ``off`` keeps the baseline behaviour exactly: the enhanced path changes nothing."""
    model = AgentModel(structured=False)
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="off")
    node = create_trader(graph.quick_thinking_llm)
    with pytest.raises(KeyError):
        node({})
    assert model.calls.get("Trader", 0) == 0 and model.plain_calls.get("Trader", 0) == 0


def test_c_input_contract_names_every_nested_key_the_node_reads():
    """The validated keys are the keys the nodes index, not a subset of them."""
    rm = contracts.validate_research_manager_input(
        {"investment_debate_state": {"history": "", "count": 3}})
    assert [i.field for i in rm] == ["investment_debate_state.history"]

    pm = contracts.validate_portfolio_manager_input({"risk_debate_state": {"history": "h"}})
    assert {i.field for i in pm} == {"investment_plan", "trader_investment_plan"}


# ---------------------------------------------------------------------------
# D: saved upstream payloads are revalidated in full before an enhanced consumer
# ---------------------------------------------------------------------------

# Rendered by the baseline renderer, then damaged. Each is a plan the graph could have
# persisted before the contract existed, and each must now be refused at INPUT.
def _rendered_plan(**values):
    from tradingagents.agents.schemas import ResearchPlan, render_research_plan
    return render_research_plan(ResearchPlan(**values))


def _rendered_trader(**values):
    from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
    return render_trader_proposal(TraderProposal(**values))


STALE_PLAN_RATING_ONLY = "**Recommendation**: Buy"  # a rating with no reasoning
STALE_TRADER_BAD_PRICE = _rendered_trader(action=schemas.TraderAction.BUY, reasoning="Add.",
                                          entry_price=189.5, stop_loss=172.0).replace("189.5", "15%")
STALE_PLAN_BLANK_NARRATIVE = _rendered_plan(
    recommendation=schemas.PortfolioRating.OVERWEIGHT, rationale="   ", strategic_actions="s")
STALE_TRADER_BLANK_REASONING = _rendered_trader(
    action=schemas.TraderAction.BUY, reasoning="   ", entry_price=189.5, stop_loss=172.0)


@pytest.mark.parametrize("field,value,expected_field,expected_code", [
    # A rating with no reasoning is refused for the missing narrative, not accepted because
    # the label itself parses.
    ("investment_plan", STALE_PLAN_RATING_ONLY, "rationale", "missing_field"),
    ("investment_plan", STALE_PLAN_BLANK_NARRATIVE, "rationale", "blank"),
    # ADR-002 section 4: an unreadable *optional* numeric is an absence, not a refusal; the
    # bad-price row now asserts acceptance (see the test below). A blank required narrative
    # is still refused.
    ("trader_investment_plan", STALE_TRADER_BLANK_REASONING, "reasoning", "blank"),
])
def test_d_stale_saved_payload_is_refused_before_consumption(
        tmp_path, monkeypatch, offline, field, value, expected_field, expected_code):
    """Every required-field failure refuses the plan, not only an unknown label."""
    model = AgentModel(structured=True)
    graph = _graph(tmp_path / field.replace("_", ""), monkeypatch, model,
                   agent_contract_mode="validate")
    node = create_portfolio_manager(graph.quick_thinking_llm)
    with pytest.raises(AgentContractError) as caught:
        node(dict(PM_STATE_OK, **{field: value}))
    assert caught.value.stage == contracts.Stage.INPUT
    assert (expected_field, expected_code) in {(i.field, i.code) for i in caught.value.issues}
    assert caught.value.record.calls.logical_total == 0
    assert model.calls.get("Portfolio Manager", 0) == 0


def test_d_stale_saved_plan_with_an_unreadable_optional_price_is_accepted(
        tmp_path, monkeypatch, offline):
    """ADR-002 section 4 (NS-14): an unreadable optional level does not refuse the plan.

    The plan is revalidated for the Portfolio Manager's own input: its required action and
    reasoning are present, so the level is simply absent and the agent may run.
    """
    model = AgentModel(structured=True)
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate")
    node = create_portfolio_manager(graph.quick_thinking_llm)
    node(dict(PM_STATE_OK, trader_investment_plan=STALE_TRADER_BAD_PRICE))
    assert model.calls.get("Portfolio Manager", 0) == 1


def test_d_risk_debator_accepts_a_plan_with_an_unreadable_optional_price(
        tmp_path, monkeypatch, offline):
    """The guard revalidates in full; a bad optional level is absence, a blank one is refusal.

    ADR-002 section 4 supersedes the old expectation that the debator refused such a plan.
    """
    model = AgentModel(structured=True)
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate")
    node = create_aggressive_debator(graph.quick_thinking_llm)
    node(dict(PM_STATE_OK, trader_investment_plan=STALE_TRADER_BAD_PRICE,
              **dict.fromkeys(("market_report", "sentiment_report", "news_report", "fundamentals_report"), "")))
    assert model.calls.get("Aggressive Analyst", 0) + model.plain_calls.get("Aggressive Analyst", 0) == 1

    with pytest.raises(AgentContractError) as caught:
        node(dict(PM_STATE_OK, trader_investment_plan=STALE_TRADER_BLANK_REASONING,
                  **dict.fromkeys(("market_report", "sentiment_report", "news_report", "fundamentals_report"), "")))
    assert caught.value.stage == contracts.Stage.INPUT
    assert ("reasoning", "blank") in {(issue.field, issue.code) for issue in caught.value.issues}


def test_d_valid_saved_plans_resume_normally(tmp_path, monkeypatch, offline):
    """Control case: the same revalidation accepts the real rendered plans."""
    good_plan = _rendered_plan(recommendation=schemas.PortfolioRating.OVERWEIGHT,
                               rationale="Bulls led.", strategic_actions="Scale in.")
    good_trader = _rendered_trader(action=schemas.TraderAction.BUY, reasoning="Add.",
                                   entry_price=189.5, stop_loss=172.0)
    assert contracts.revalidate_saved_plan("Research Manager", good_plan, "investment_plan") is None
    assert contracts.revalidate_saved_plan("Trader", good_trader, "trader_investment_plan") is None
    # Only an absent optional value is skipped; a required plan is never skipped.
    assert contracts.revalidate_saved_plan("Research Manager", None,
                                           "investment_plan").code == "missing_required_input"


# ---------------------------------------------------------------------------
# F: a real parse exception raised inside invoke is recoverable; programming is not
# ---------------------------------------------------------------------------

def _run_pm(model, mode="validate", **config):
    state = dict(PM_STATE_OK, **dict.fromkeys((), ""))
    return contracts.invoke_with_contract(
        agent="Portfolio Manager", mode=mode, state=state,
        structured_llm=model.structured_llm, plain_llm=model.plain_llm,
        prompt="judge the debate",
        recovery_prompt=lambda issues: "judge the debate\n\nRepair these: "
                                       + ", ".join(f"{i.field}:{i.code}" for i in issues),
        sdk_retry_budget=config.get("sdk_retry_budget"))


class _RaisingModel:
    """A structured callable that raises, then answers with a real ``AIMessage``."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def invoke(self, prompt, **kwargs):
        self.calls += 1
        step = self.script.pop(0) if self.script else AssertionError("too many calls")
        if isinstance(step, BaseException):
            raise step
        return step


class _Harness:
    def __init__(self, structured_llm=None, plain_llm=None):
        self.structured_llm = structured_llm
        self.plain_llm = plain_llm


def test_f_validation_error_inside_invoke_is_recovered_by_one_valid_answer():
    """A Pydantic ``ValidationError`` raised by the provider is a parse failure, not a crash."""
    bad = ValidationError.from_exception_data("StrictPortfolioDecision", [
        {"type": "missing", "loc": ("executive_summary",), "input": {},
         "ctx": {"field_name": "executive_summary"}}])
    structured = _RaisingModel(bad, PM_OK)
    result = _run_pm(_Harness(structured_llm=structured), mode="validate+repair")
    assert structured.calls == 2
    assert result.record.calls.semantic_repair == 1
    assert result.record.calls.free_text_recovery == 0
    assert result.record.calls.logical_total == 2


def test_f_output_parser_exception_is_a_parse_failure_with_one_fallback():
    """``OutputParserException`` in ``validate`` spends the one compatibility fallback."""
    structured = _RaisingModel(OutputParserException("could not parse tool call"))
    plain = _RaisingModel(AIMessage(content=TEXT_PM))
    result = _run_pm(_Harness(structured, plain), mode="validate")
    assert structured.calls == 1 and plain.calls == 1
    assert result.record.calls.free_text_recovery == 1
    assert result.record.calls.semantic_repair == 0
    assert result.record.calls.logical_total == 2


def test_f_invalid_fallback_is_rejected_with_no_third_call():
    structured = _RaisingModel(OutputParserException("bad json"))
    plain = _RaisingModel(AIMessage(content="**Rating**: Buy-ish\n\n**Executive Summary**: s\n\n"
                                            "**Investment Thesis**: t"))
    with pytest.raises(AgentContractError) as caught:
        _run_pm(_Harness(structured, plain), mode="validate")
    assert structured.calls == 1 and plain.calls == 1
    assert caught.value.record.calls.logical_total == 2
    assert "rating" in {i.field for i in caught.value.issues}


@pytest.mark.parametrize("error", [ValueError("provider said no"), KeyError("missing")])
def test_f_arbitrary_value_or_key_error_inside_invoke_is_programming(error):
    """A bare ``ValueError``/``KeyError`` is a bug: surfaced, never repaired or replaced."""
    structured = _RaisingModel(error, PM_OK)
    plain = _RaisingModel(AIMessage(content=TEXT_PM))
    with pytest.raises(AgentContractError) as caught:
        _run_pm(_Harness(structured, plain), mode="validate+repair")
    assert caught.value.kind == contracts.FailureKind.PROGRAMMING
    assert structured.calls == 1 and plain.calls == 0
    assert caught.value.record.calls.semantic_repair == 0


def test_f_transport_error_is_never_repaired():
    error = type("APIConnectionError", (Exception,), {})("connection reset by peer")
    structured = _RaisingModel(error, PM_OK)
    with pytest.raises(AgentContractError) as caught:
        _run_pm(_Harness(structured_llm=structured), mode="validate+repair")
    assert caught.value.kind == contracts.FailureKind.TRANSPORT
    assert structured.calls == 1
    assert caught.value.record.calls.observed_http_attempts == "unknown"
    assert caught.value.record.calls.semantic_repair == 0


def test_f_repair_prompt_carries_the_original_prompt_and_field_issues():
    """The repair asks the same question again, plus the exact problems, nothing else."""
    captured = []
    bad = ValidationError.from_exception_data("StrictPortfolioDecision", [
        {"type": "missing", "loc": ("executive_summary",), "input": {},
         "ctx": {"field_name": "executive_summary"}}])

    class Model:
        def invoke(self, prompt, **kwargs):
            captured.append(prompt)
            if len(captured) == 1:
                raise bad
            return PM_OK

    result = contracts.invoke_with_contract(
        agent="Portfolio Manager", mode="validate+repair", state=dict(PM_STATE_OK),
        structured_llm=Model(), plain_llm=None, prompt="ORIGINAL PROMPT TEXT",
        recovery_prompt=lambda issues: contracts.build_repair_prompt(
            "ORIGINAL PROMPT TEXT", issues, "the required sections"))
    assert len(captured) == 2
    assert captured[1].startswith("ORIGINAL PROMPT TEXT")
    assert "executive_summary: missing" in captured[1]
    assert result.record.calls.semantic_repair == 1


# ---------------------------------------------------------------------------
# T-20 / E / T-30 -- the CLI streaming path: no decision on failure, no leakage
# ---------------------------------------------------------------------------

def _cli_run(tmp_path, monkeypatch, model, mode="validate", fail=True):
    """Drive the real CLI streaming path with a scripted graph and no terminal."""
    from cli import run as cli_run

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg.update(results_dir=str(tmp_path / "results"), data_cache_dir=str(tmp_path / "cache"),
               memory_log_path=str(tmp_path / "log.md"), agent_contract_mode=mode)
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode=mode)

    recorded, cleared = [], []
    original_record = graph.record_decision
    original_clear = graph.clear_checkpoint_on_success
    graph.record_decision = lambda *a, **k: (recorded.append(a), original_record(*a, **k))[0]
    graph.clear_checkpoint_on_success = lambda *a, **k: (
        cleared.append(a), original_clear(*a, **k))[0]

    monkeypatch.setattr(cli_run, "TradingAgentsGraph", lambda *a, **k: graph)
    monkeypatch.setattr(cli_run, "_build_run_config", lambda selections, checkpoint: cfg)
    monkeypatch.setattr(cli_run, "_run_directory",
                        lambda config, ticker, date: Path(config["results_dir"]) / "cli_run")
    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags: {
        "ticker": "NVDA", "analysis_date": TRADE_DATE, "asset_type": "stock",
        "analysts": {schemas_type("market")}, "research_depth": 1, "output_language": "English",
    })
    monkeypatch.setattr(cli_run.console, "print", lambda *a, **k: None)
    # A real, empty buffer: the CLI wraps its methods itself to write a message log.
    monkeypatch.setattr(cli_run, "message_buffer", type(cli_run.message_buffer)())
    monkeypatch.setattr(cli_run, "create_layout", lambda: None)
    monkeypatch.setattr(cli_run, "update_display", lambda *a, **k: None)
    import rich.live as rich_live
    monkeypatch.setattr(rich_live, "Live", _NoLive)
    monkeypatch.setattr(cli_run, "display_complete_report", lambda state: None)
    return graph, recorded, cleared


class _NoLive:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, *a, **k):
        pass


def schemas_type(name):
    from cli.models import AnalystType
    return AnalystType(name)


def cli_run_run_analysis():
    """Call the real CLI entry point with the patched collaborators from ``_cli_run``."""
    from cli import run as cli_run

    return cli_run.run_analysis(flags={"ticker": "NVDA", "save": True, "show": False})


def test_t20_cli_failure_records_no_decision(tmp_path, monkeypatch, offline):
    """A contract failure on the streaming path writes no decision and clears nothing."""
    model = AgentModel(structured=True,
                       answers=ScriptedAnswers(StrictPortfolioDecision=[PM_BAD]))
    graph, recorded, cleared = _cli_run(tmp_path, monkeypatch, model, mode="validate")

    with pytest.raises((AgentContractError, SystemExit)) as caught:
        cli_run_run_analysis()

    if isinstance(caught.value, SystemExit):
        pytest.fail("the CLI did not surface the contract failure")
    assert recorded == [] and cleared == []
    assert graph.memory_log.load_entries() == []
    audits = list((tmp_path / "results" / "failed_runs").glob("NVDA*.md"))
    assert len(audits) == 1


def test_e_cli_success_report_carries_records_and_does_not_leak(tmp_path, monkeypatch, offline):
    """A successful streamed run keeps its records for the report; the next run starts clean."""
    from tradingagents.agents import contracts as contract_module

    model = AgentModel(structured=True)
    graph, recorded, cleared = _cli_run(tmp_path, monkeypatch, model, mode="validate", fail=False)
    cli_run_run_analysis()

    assert len(recorded) == 1 and len(cleared) == 1
    first = graph.contract_records()
    assert set(first) == {"Research Manager", "Trader", "Portfolio Manager"}
    # Restored on exit: no scope leaks into the caller.
    assert contract_module._RUN_RECORDS.get() is None

    # The successful streamed run's saved report carries the contract block, read from the
    # records the CLI kept for it.
    reports = list((tmp_path / "results").rglob("complete_report.md"))
    assert reports, "the CLI did not save the report"
    assert "## Contract Validation" in reports[0].read_text(encoding="utf-8")

    # A different run whose PM fails must not inherit the earlier success: the old records
    # are live for that run only while its own agents run, and a failure records a failure.
    failing_model = AgentModel(structured=True,
                               answers=ScriptedAnswers(StrictPortfolioDecision=[PM_BAD]))
    failing, failing_recorded, failing_cleared = _cli_run(
        tmp_path / "second", monkeypatch, failing_model, mode="validate")
    with pytest.raises(AgentContractError):
        cli_run_run_analysis()
    assert failing_recorded == [] and failing_cleared == []
    assert failing.contract_records()["Portfolio Manager"].ok is False
    assert failing.contract_records()["Research Manager"].ok is True
    # And the first graph's records are unchanged by the second run.
    assert graph.contract_records() == first
    assert contract_module._RUN_RECORDS.get() is None


def test_e_successful_run_saves_records_into_state_for_resume(tmp_path, monkeypatch, offline):
    """Records are additive state keys, serialisable, and present only in enhanced modes."""
    model = AgentModel(structured=True)
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate")
    state, _ = graph.propagate("NVDA", TRADE_DATE)
    saved = state.get("contract_records")
    assert set(saved) == {"Research Manager", "Trader", "Portfolio Manager"}
    assert saved["Trader"]["ok"] is True
    import json
    json.dumps(saved)   # a checkpoint round trip must not need custom types

    off_graph = _graph(tmp_path / "off", monkeypatch, AgentModel(), agent_contract_mode="off")
    off_state, _ = off_graph.propagate("NVDA", TRADE_DATE)
    assert "contract_records" not in off_state or off_state["contract_records"] == {}


# ---------------------------------------------------------------------------
# T-22 -- a same-mode resume revalidates the saved upstream payloads
# ---------------------------------------------------------------------------

def _seed_thread(graph, tmp_path, as_node, **state_values):
    """Save a checkpoint under this graph's own signature, as an interrupted run would.

    ``update_state`` with ``as_node`` is the supported way to write a partially-complete
    checkpoint: the thread resumes at the edge leaving ``as_node`` instead of re-running
    the analysts, which is exactly the state a resume sees.
    """
    signature = graph._run_signature("stock")
    tid = checkpointer.thread_id("NVDA", TRADE_DATE, signature)
    with checkpointer.get_checkpointer(str(tmp_path / "cache"), "NVDA") as saver:
        graph.workflow.compile(checkpointer=saver).update_state(
            {"configurable": {"thread_id": tid}}, state_values, as_node=as_node)
    assert checkpointer.checkpoint_step(str(tmp_path / "cache"), "NVDA", TRADE_DATE,
                                        signature) is not None
    return tid


def test_t22_same_mode_resume_revalidates_a_stale_saved_plan(tmp_path, monkeypatch, offline):
    """A resume that still holds an invalid plan is refused at INPUT with zero Trader calls."""
    model = AgentModel(structured=True)
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate",
                   checkpoint_enabled=True)
    _seed_thread(graph, tmp_path, "Research Manager",
                 investment_plan=STALE_PLAN_RATING_ONLY,
                 trader_investment_plan=STALE_TRADER_BLANK_REASONING)

    with pytest.raises(AgentContractError) as caught:
        graph.propagate("NVDA", TRADE_DATE)

    assert caught.value.agent == "Trader"
    assert caught.value.stage == contracts.Stage.INPUT
    assert model.calls.get("Trader", 0) == 0 and model.plain_calls.get("Trader", 0) == 0


def test_t22_valid_saved_plan_resumes_normally(tmp_path, monkeypatch, offline):
    """Control case: the same resume with valid saved plans runs to a decision."""
    model = AgentModel(structured=True)
    graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate",
                   checkpoint_enabled=True)
    good_plan = _rendered_plan(recommendation=schemas.PortfolioRating.OVERWEIGHT,
                               rationale="Bulls led.", strategic_actions="Scale in.")
    _seed_thread(graph, tmp_path, "Research Manager", investment_plan=good_plan,
                 company_of_interest="NVDA", trade_date=TRADE_DATE,
                 market_report="Market.", sentiment_report="", news_report="",
                 fundamentals_report="", risk_debate_state={
                     "aggressive_history": "", "conservative_history": "",
                     "neutral_history": "", "history": "", "latest_speaker": "",
                     "current_aggressive_response": "", "current_conservative_response": "",
                     "current_neutral_response": "", "count": 0},
                 investment_debate_state={"bull_history": "Bull.", "bear_history": "Bear.",
                                          "history": "Bull: add.", "current_response": "Bull: add.",
                                          "count": 2})

    state, signal = graph.propagate("NVDA", TRADE_DATE)
    assert signal == "Overweight"
    assert state["investment_plan"] == good_plan


# ---------------------------------------------------------------------------
# T-30 / G -- a failed run never overwrites evidence; settlement stays independent
# ---------------------------------------------------------------------------
def test_g_two_consecutive_failures_keep_both_audits(tmp_path, monkeypatch, offline):
    """Two failures in the same second each keep their own audit file (review item G)."""
    failures = []
    for _ in range(2):
        model = AgentModel(structured=True,
                           answers=ScriptedAnswers(StrictPortfolioDecision=[PM_BAD]))
        graph = _graph(tmp_path, monkeypatch, model, agent_contract_mode="validate")
        with pytest.raises(AgentContractError) as caught:
            graph.propagate("NVDA", TRADE_DATE)
        failures.append(caught.value)

    audits = sorted((tmp_path / "results" / "failed_runs").glob("NVDA*.md"))
    assert len(audits) == 2, [p.name for p in audits]
    assert audits[0].name != audits[1].name
    for path in audits:
        assert "failed" in path.read_text(encoding="utf-8").lower()


def test_t30_failed_run_preserves_the_earlier_success_and_adds_no_entry(tmp_path, monkeypatch, offline):
    """The failed audit is separate; the earlier success and memory log are untouched."""
    good_model = AgentModel(structured=True)
    graph = _graph(tmp_path, monkeypatch, good_model, agent_contract_mode="validate")
    graph.propagate("NVDA", TRADE_DATE)
    entries_before = graph.memory_log.load_entries()
    reports_before = sorted(p.relative_to(tmp_path) for p in (tmp_path / "results").rglob("*.md")
                            if "failed_runs" not in p.parts)
    assert len(entries_before) == 1

    bad_model = AgentModel(structured=True,
                           answers=ScriptedAnswers(StrictPortfolioDecision=[PM_BAD]))
    graph = _graph(tmp_path, monkeypatch, bad_model, agent_contract_mode="validate")
    with pytest.raises(AgentContractError):
        graph.propagate("NVDA", TRADE_DATE)

    assert graph.memory_log.load_entries() == entries_before
    reports_after = sorted(p.relative_to(tmp_path) for p in (tmp_path / "results").rglob("*.md")
                           if "failed_runs" not in p.parts)
    assert reports_after == reports_before
    assert list((tmp_path / "results" / "failed_runs").glob("NVDA*.md"))


# ---------------------------------------------------------------------------
# H -- optional analyst reports in the real nodes: absent, None and blank
# ---------------------------------------------------------------------------
#
# Supervisor-found gap: the helper-level T-2 tests passed while the real nodes still
# indexed ``state["market_report"]`` and friends, so a state without those keys raised
# ``KeyError`` in mode ``validate``. These tests run the production node factories with a
# ``MagicMock`` model and assert the invocation the node actually made. Every state here is
# a complete, valid state for its node: the only thing that varies is the optional reports.

OPTIONAL_REPORTS = ("market_report", "sentiment_report", "news_report", "fundamentals_report")

# The four-debator input contract state: every key a debator reads, so the only variable is
# the optional reports. The identity and the risk history are required and stay valid.
RISK_STATE_OK = {
    "company_of_interest": "NVDA",
    "trader_investment_plan": "**Action**: Buy\n\n**Reasoning**: r",
    "risk_debate_state": {"history": "Aggressive: add. Conservative: trim.",
                          "aggressive_history": "", "conservative_history": "",
                          "neutral_history": "", "latest_speaker": "",
                          "current_aggressive_response": "",
                          "current_conservative_response": "",
                          "current_neutral_response": "", "count": 2},
}

DEBATORS = [
    ("Aggressive", create_aggressive_debator),
    ("Conservative", create_conservative_debator),
    ("Neutral", create_neutral_debator),
]


def _with_reports(state, variant):
    """``state`` with every optional report absent, ``None`` or blank."""
    state = copy.deepcopy(state)
    for key in OPTIONAL_REPORTS:
        if variant == "absent":
            state.pop(key, None)
        elif variant == "none":
            state[key] = None
        else:
            state[key] = "   "
    return state


def _trader_answer():
    """A valid strict Trader payload -- what the structured route returns by default."""
    return StrictTraderProposal(action=schemas.TraderAction.BUY,
                                reasoning="The plan supports adding here.",
                                entry_price=189.5, stop_loss=172.0, position_sizing="5%")


def _mock_llm(answer="argument"):
    """A ``MagicMock`` model: its ``invoke`` is observed, not exercised.

    ``with_structured_output`` returns a real runnable whose calls are recorded in
    ``structured_calls``, as a provider that supports structured output does, so the
    node's structured call is the one that happens and can be counted exactly.
    """
    from unittest.mock import MagicMock

    structured_calls: list = []
    llm = MagicMock()
    llm.invoke.return_value = AIMessage(content=answer if isinstance(answer, str) else "argument")
    llm.with_structured_output.return_value = RunnableLambda(
        lambda prompt: (structured_calls.append(prompt), _trader_answer())[1])
    llm.structured_calls = structured_calls
    return llm


def _config(mode):
    return copy.deepcopy(DEFAULT_CONFIG) | {"agent_contract_mode": mode}


def _run_node_in_mode(node, state, mode):
    """Run ``node`` with ``mode`` and a fresh record scope bound, as the graph binds them."""
    with tradingagents_config.run_config(_config(mode)):
        token = contracts.begin_record_scope()
        try:
            return node(copy.deepcopy(state))
        finally:
            contracts.end_record_scope(token)


@pytest.mark.parametrize("variant", ["absent", "none", "blank"])
@pytest.mark.parametrize("mode", ["validate", "validate+repair"])
def test_h_trader_accepts_optional_reports_absent_none_or_blank(variant, mode):
    """The real Trader node accepts a valid plan when the market report is not there."""
    llm = _mock_llm()
    node = create_trader(llm)
    state = _with_reports(TRADER_STATE_OK, variant)
    state["instrument_context"] = "NVDA"

    update = _run_node_in_mode(node, state, mode)

    llm.with_structured_output.assert_called_once()
    assert len(llm.structured_calls) == 1, "the structured route must run exactly once"
    llm.invoke.assert_not_called()
    assert update["trader_investment_plan"].startswith("**Action**: Buy")
    assert update["contract_records"]["Trader"]["ok"] is True
    # The absent report is not invented into the prompt.
    assert "Technical Market Report:" not in str(llm.structured_calls[0])


@pytest.mark.parametrize("bad_report", [12345, ["text"], {"body": "text"}], ids=[
    "integer", "list", "mapping"])
def test_h_trader_rejects_a_malformed_report_under_the_input_contract(bad_report):
    """A non-text report is still an explicit input failure, and costs no model call."""
    llm = _mock_llm()
    node = create_trader(llm)
    state = _with_reports(TRADER_STATE_OK, "absent")
    state["market_report"] = bad_report

    with pytest.raises(AgentContractError) as caught:
        _run_node_in_mode(node, state, "validate")

    assert caught.value.stage == contracts.Stage.INPUT
    assert ("market_report", "wrong_input_type") in {(i.field, i.code) for i in caught.value.issues}
    assert caught.value.record.calls.logical_total == 0
    assert llm.structured_calls == []
    llm.invoke.assert_not_called()


@pytest.mark.parametrize("variant", ["absent", "none", "blank"])
@pytest.mark.parametrize("label,factory", DEBATORS, ids=[name for name, _ in DEBATORS])
def test_h_risk_debators_accept_optional_reports_absent_none_or_blank(variant, label, factory):
    """Each real risk debator accepts a valid trader plan with no analyst reports."""
    llm = _mock_llm(f"{label} argument.")
    node = factory(llm)
    state = _with_reports(RISK_STATE_OK, variant)

    update = _run_node_in_mode(node, state, "validate")

    llm.invoke.assert_called_once()
    prompt = llm.invoke.call_args.args[0]
    # The model is told the report is missing, not shown an empty finding.
    assert "(No market report in this run" in prompt
    assert f"{label} Analyst: {label} argument." in update["risk_debate_state"]["history"]
    assert update["risk_debate_state"]["count"] == 3


@pytest.mark.parametrize("label,factory", DEBATORS, ids=[name for name, _ in DEBATORS])
def test_h_risk_debators_reject_a_malformed_report(label, factory):
    """A non-text report is an input failure at the guard, with zero model calls."""
    llm = _mock_llm(f"{label} argument.")
    node = factory(llm)
    state = _with_reports(RISK_STATE_OK, "blank")
    state["news_report"] = ["not", "text"]

    with pytest.raises(AgentContractError) as caught:
        _run_node_in_mode(node, state, "validate")

    assert caught.value.stage == contracts.Stage.INPUT
    assert ("news_report", "wrong_input_type") in {(i.field, i.code) for i in caught.value.issues}
    llm.invoke.assert_not_called()


# The supervisor's own reproduction of the gap, kept verbatim as a test: a real Trader node,
# a ``MagicMock`` whose structured call returns the F-01 payload, ``validate`` mode, the
# fixture state minus ``market_report``. Before the fix this raised ``KeyError``.
def test_h_supervisor_reproduction_trader_without_market_report():
    from unittest.mock import MagicMock

    from tests.test_agent_contracts import TRADER_STATE

    calls = []
    llm = MagicMock()
    llm.with_structured_output.return_value = RunnableLambda(
        lambda prompt: (calls.append(prompt), _trader_answer())[1])
    node = create_trader(llm)
    state = copy.deepcopy(TRADER_STATE)
    state.pop("market_report", None)
    state["company_of_interest"] = "NVDA"
    state["instrument_context"] = "NVDA"

    update = _run_node_in_mode(node, state, "validate")

    assert len(calls) == 1
    llm.invoke.assert_not_called()
    assert update["trader_investment_plan"]
    assert update["contract_records"]["Trader"]["ok"] is True


# Required upstream plans are never loosened: an invalid one still rejects with zero calls.


@pytest.mark.parametrize("plan", [None, "", "   ", 42], ids=["none", "empty", "blank", "wrong-type"])
def test_h_trader_rejects_a_required_invalid_plan_with_zero_calls(plan):
    llm = _mock_llm("**Action**: Buy\n\n**Reasoning**: The plan supports adding.")
    node = create_trader(llm)
    state = _with_reports(dict(TRADER_STATE_OK, investment_plan=plan), "absent")

    with pytest.raises(AgentContractError) as caught:
        _run_node_in_mode(node, state, "validate")

    assert caught.value.stage == contracts.Stage.INPUT
    assert "investment_plan" in {issue.field for issue in caught.value.issues}
    assert caught.value.record.calls.logical_total == 0
    assert llm.structured_calls == []
    llm.invoke.assert_not_called()


def test_h_trader_rejects_a_rendered_plan_missing_its_narrative_with_zero_calls():
    """A plan that is text but fails its own contract is refused, naming the bad field."""
    llm = _mock_llm("**Action**: Buy\n\n**Reasoning**: The plan supports adding.")
    node = create_trader(llm)
    state = _with_reports(dict(TRADER_STATE_OK, investment_plan=STALE_PLAN_RATING_ONLY), "absent")

    with pytest.raises(AgentContractError) as caught:
        _run_node_in_mode(node, state, "validate")

    assert caught.value.stage == contracts.Stage.INPUT
    assert ("rationale", "missing_field") in {(i.field, i.code) for i in caught.value.issues}
    assert caught.value.record.calls.logical_total == 0
    assert llm.structured_calls == []
    llm.invoke.assert_not_called()


@pytest.mark.parametrize("label,factory", DEBATORS, ids=[name for name, _ in DEBATORS])
@pytest.mark.parametrize("plan", [None, "", "   "], ids=["none", "empty", "blank"])
def test_h_risk_debators_reject_a_required_invalid_plan_with_zero_calls(label, factory, plan):
    llm = _mock_llm(f"{label} argument.")
    node = factory(llm)
    state = _with_reports(dict(RISK_STATE_OK, trader_investment_plan=plan), "absent")

    with pytest.raises(AgentContractError) as caught:
        _run_node_in_mode(node, state, "validate")

    assert caught.value.stage == contracts.Stage.INPUT
    llm.invoke.assert_not_called()


@pytest.mark.parametrize("label,factory", DEBATORS, ids=[name for name, _ in DEBATORS])
def test_h_risk_debators_accept_a_plan_whose_optional_price_is_unreadable(label, factory):
    """ADR-002 section 4: the bad *optional* level is absent; the plan is still usable."""
    llm = _mock_llm(f"{label} argument.")
    node = factory(llm)
    state = _with_reports(dict(RISK_STATE_OK, trader_investment_plan=STALE_TRADER_BAD_PRICE), "absent")

    node(state)

    llm.invoke.assert_called_once()


@pytest.mark.parametrize("label,factory", DEBATORS, ids=[name for name, _ in DEBATORS])
def test_h_risk_state_remains_required(label, factory):
    """The risk debate state is required by the debators and was not loosened."""
    llm = _mock_llm("argument")
    node = factory(llm)
    broken = _with_reports(RISK_STATE_OK, "none")
    broken.pop("risk_debate_state", None)

    with pytest.raises(AgentContractError) as caught:
        _run_node_in_mode(node, broken, "validate")

    assert ("risk_debate_state", "missing_required_input") in {
        (i.field, i.code) for i in caught.value.issues}
    llm.invoke.assert_not_called()


@pytest.mark.parametrize("variant", ["present", "none"])
@pytest.mark.parametrize("label,factory", DEBATORS, ids=[name for name, _ in DEBATORS])
def test_h_off_mode_reads_are_unchanged_for_a_valid_fixture(variant, label, factory):
    """Mode ``off`` is untouched: a fixture that carries the optional keys still runs."""
    llm = _mock_llm("argument")
    state = _with_reports(RISK_STATE_OK, variant)

    _run_node_in_mode(factory(llm), state, "off")

    llm.invoke.assert_called_once()


@pytest.mark.parametrize("variant", ["present", "none"])
def test_h_off_mode_trader_reads_are_unchanged_for_a_valid_fixture(variant):
    """The Trader's own mode-``off`` route still runs once for the same fixture."""
    llm = _mock_llm("**Action**: Buy\n\n**Reasoning**: The plan supports adding.")
    state = _with_reports(TRADER_STATE_OK, variant)

    node = create_trader(llm)
    # ``off`` binds the baseline schema through ``with_structured_output``.
    update = _run_node_in_mode(node, state, "off")

    llm.with_structured_output.assert_called_once()
    assert update["trader_investment_plan"]
    assert "contract_records" not in update
