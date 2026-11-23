"""Startup configuration preflight — ADR-002 section 3, tests PF-1..PF-11.

Every expectation is written from the ADR and the repository code it cites, not from the
implementation: a spy records whether an LLM client, a node factory, a directory or a live
view was touched, and a damaged key is supplied one at a time so the failure names exactly
the parameter that is wrong.
"""

from __future__ import annotations

import copy
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import tradingagents.default_config as default_config_module
import tradingagents.graph.setup as graph_setup
import tradingagents.graph.trading_graph as trading_graph
from tradingagents.config_validation import (
    ConfigValidationError,
    effective_config,
    validate_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _valid_config(tmp_path) -> dict:
    config = copy.deepcopy(default_config_module.DEFAULT_CONFIG)
    config.update(
        results_dir=str(tmp_path / "results"),
        data_cache_dir=str(tmp_path / "cache"),
        memory_log_path=str(tmp_path / "log.md"),
    )
    return config


def _parameters(config) -> set[str]:
    return {problem.parameter for problem in validate_config(config)}


# ---------------------------------------------------------------------------
# PF-1 — every invalid parameter is named, all at once
# ---------------------------------------------------------------------------


def test_pf1_every_invalid_parameter_is_named_in_one_report():
    config = _valid_config(Path(".")) | {
        "agent_contract_mode": "strict",
        "max_debate_rounds": -1,
        "max_tokens": 0,
        "temperature": float("nan"),
        "deep_think_llm": "   ",
        "backend_url": "not a url",
    }
    problems = validate_config(config)

    assert {p.parameter for p in problems} == {
        "agent_contract_mode", "max_debate_rounds", "max_tokens", "temperature",
        "deep_think_llm", "backend_url",
    }
    assert all(p.detail for p in problems)
    with pytest.raises(ConfigValidationError) as caught:
        raise ConfigValidationError(problems)
    for parameter in ("agent_contract_mode", "max_debate_rounds", "max_tokens", "temperature",
                      "deep_think_llm", "backend_url"):
        assert parameter in str(caught.value)


# ---------------------------------------------------------------------------
# PF-2 — a malformed config fails at graph construction with no side effect
# ---------------------------------------------------------------------------


def _spy_clients(monkeypatch):
    calls = []

    def factory(config, tier, **kwargs):
        calls.append(tier)
        raise AssertionError("a client must not be constructed for an invalid configuration")

    monkeypatch.setattr(trading_graph, "create_tier_client", factory)
    return calls


def _spy_nodes(monkeypatch):
    calls = []

    def setup(self, *args, **kwargs):
        calls.append(args)
        raise AssertionError("no node may be built for an invalid configuration")

    monkeypatch.setattr(graph_setup.GraphSetup, "setup_graph", setup)
    return calls


@pytest.mark.parametrize("key,value", [
    ("agent_contract_mode", "strict"),
    ("max_debate_rounds", -1),
    ("max_risk_discuss_rounds", 1.5),
    ("max_recur_limit", 0),
    ("max_tool_rounds", 0),
    ("max_tokens", 0),
    ("llm_max_retries", -1),
    ("temperature", float("inf")),
    ("deep_think_llm", ""),
    ("quick_think_llm", None),
    ("llm_provider", "nonesuch"),
    ("backend_url", "localhost:8000"),
    ("checkpoint_enabled", "yes"),
    ("data_vendors", "yfinance"),
    ("tool_vendors", {"get_stock_data": 3}),
    ("tool_vendors", ["get_stock_data"]),
    ("benchmark_map", {1: "SPY"}),
    ("benchmark_map", "SPY"),
    ("benchmark_ticker", ""),
    ("output_language", ""),
    ("memory_log_max_entries", -1),
])
def test_pf2_malformed_config_fails_before_any_side_effect(tmp_path, monkeypatch, key, value):
    results, cache = tmp_path / "results", tmp_path / "cache"
    config = _valid_config(tmp_path) | {key: value}
    clients = _spy_clients(monkeypatch)
    nodes = _spy_nodes(monkeypatch)

    with pytest.raises(ConfigValidationError) as caught:
        trading_graph.TradingAgentsGraph(config=config)

    # A container problem names the exact member (``tool_vendors.get_stock_data``), so the
    # offending parameter is never hidden behind the container's name.
    assert any(p.parameter == key or p.parameter.startswith(f"{key}.")
               for p in caught.value.problems), caught.value.problems
    assert clients == [] and nodes == []
    assert not results.exists() and not cache.exists()


# ---------------------------------------------------------------------------
# PF-3 — preflight runs first, even where the old check failed after the clients existed
# ---------------------------------------------------------------------------


def test_pf3_tool_round_limit_is_checked_before_clients_are_built(tmp_path, monkeypatch):
    config = _valid_config(tmp_path) | {"max_tool_rounds": 20, "max_recur_limit": 30}
    clients = _spy_clients(monkeypatch)

    with pytest.raises(ConfigValidationError) as caught:
        trading_graph.TradingAgentsGraph(config=config)

    assert "max_tool_rounds" in {p.parameter for p in caught.value.problems}
    assert "max_recur_limit" in str(caught.value)
    assert clients == []
    assert not (tmp_path / "results").exists()


def test_pf3_valid_boundary_of_the_tool_round_limit_is_accepted():
    assert validate_config({"max_tool_rounds": 20, "max_recur_limit": 100}) == []
    assert validate_config({"max_tool_rounds": 1, "max_recur_limit": 5}) == []
    assert "max_tool_rounds" in _parameters({"max_tool_rounds": 20, "max_recur_limit": 42})
    assert "max_tool_rounds" in _parameters({"max_tool_rounds": 20, "max_recur_limit": 41})


# ---------------------------------------------------------------------------
# PF-4 / PF-5 — valid configurations construct unchanged
# ---------------------------------------------------------------------------


class _Stub:
    def get_llm(self):
        return object()


def test_pf4_a_valid_configuration_constructs_and_creates_its_directories(tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(trading_graph, "create_tier_client",
                        lambda config, tier, **kw: built.append(tier) or _Stub())

    trading_graph.TradingAgentsGraph(config=_valid_config(tmp_path))

    assert built == ["deep", "quick"]
    assert (tmp_path / "results").is_dir() and (tmp_path / "cache").is_dir()


@pytest.mark.parametrize("overrides", [
    {"llm_provider": "openai_compatible", "backend_url": "http://localhost:8000/v1",
     "deep_think_llm": "custom-deep-2026", "quick_think_llm": "custom-quick-2026"},
    {"llm_provider": "ollama", "backend_url": None, "deep_think_llm": "llama-x",
     "quick_think_llm": "llama-x"},
])
def test_pf5_custom_endpoints_and_keyless_providers_are_accepted(tmp_path, monkeypatch, overrides):
    config = _valid_config(tmp_path) | overrides
    assert validate_config(config) == []

    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **kw: _Stub())
    trading_graph.TradingAgentsGraph(config=config)


def test_pf5_the_validator_reads_no_environment_variable(monkeypatch):
    """The validator works on the config it is given; it never consults the environment."""
    monkeypatch.setattr("os.environ", _ExplodingEnviron(), raising=False)
    assert validate_config(_valid_config(Path("."))) == []


class _ExplodingEnviron(dict):
    def __getitem__(self, key):
        raise AssertionError(f"the validator read the environment variable {key}")

    def get(self, key, default=None):
        raise AssertionError(f"the validator read the environment variable {key}")


# ---------------------------------------------------------------------------
# PF-6 — a partial config merges defaults; a supplied invalid value is not replaced
# ---------------------------------------------------------------------------


def test_pf6_partial_config_is_completed_from_the_defaults():
    merged = effective_config({"max_debate_rounds": 2})
    assert set(default_config_module.DEFAULT_CONFIG) <= set(merged)
    assert merged["max_debate_rounds"] == 2
    assert merged["max_recur_limit"] == default_config_module.DEFAULT_CONFIG["max_recur_limit"]


def test_pf6_an_invalid_supplied_value_is_reported_not_replaced():
    merged = effective_config({"max_debate_rounds": -2})
    assert merged["max_debate_rounds"] == -2          # not silently the default 1
    assert "max_debate_rounds" in _parameters(merged)


# ---------------------------------------------------------------------------
# PF-7 / PF-8 — environment-derived defaults
# ---------------------------------------------------------------------------


def _clean_env(monkeypatch, **values):
    for name in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("variable,value,key", [
    ("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "-1", "max_debate_rounds"),
    ("TRADINGAGENTS_MAX_TOKENS", "0", "max_tokens"),
    ("TRADINGAGENTS_TEMPERATURE", "nan", "temperature"),
    ("TRADINGAGENTS_AGENT_CONTRACT_MODE", "strict", "agent_contract_mode"),
    ("TRADINGAGENTS_LLM_BACKEND_URL", "not a url", "backend_url"),
    ("TRADINGAGENTS_LLM_PROVIDER", " ", "llm_provider"),
])
def test_pf7_malformed_environment_fails_when_the_defaults_are_built(monkeypatch, variable, value, key):
    _clean_env(monkeypatch, **{variable: value})
    with pytest.raises((ConfigValidationError, ValueError)) as caught:
        default_config_module.build_default_config()
    message = str(caught.value)
    assert key in message


def test_pf8_a_valid_environment_overlay_builds_a_valid_config(monkeypatch):
    _clean_env(monkeypatch, TRADINGAGENTS_MAX_DEBATE_ROUNDS="3", TRADINGAGENTS_TEMPERATURE="0.2",
               TRADINGAGENTS_LLM_MAX_RETRIES="6", TRADINGAGENTS_AGENT_CONTRACT_MODE="validate")
    config = default_config_module.build_default_config()
    assert config["max_debate_rounds"] == 3
    assert validate_config(config) == []
    assert config["agent_contract_mode"] == "validate"


# ---------------------------------------------------------------------------
# PF-9 — the CLI path fails before any side effect and before the graph
# ---------------------------------------------------------------------------


def _selections(**overrides):
    from cli.models import AnalystType

    base = {
        "ticker": "NVDA", "analysis_date": "2026-01-02", "analysts": {AnalystType.MARKET},
        "research_depth": 1, "llm_provider": "openai", "backend_url": None,
        "deep_think_llm": "d", "quick_think_llm": "q", "output_language": "English",
        "asset_type": "stock",
    }
    base.update(overrides)
    return base


def test_pf9_cli_run_fails_before_the_graph_and_before_any_directory(tmp_path, monkeypatch):
    import cli.run as cli_run

    config = _valid_config(tmp_path) | {"max_tokens": "0", "temperature": "hot"}
    graph_calls = []
    live_blocks = []
    mkdir_calls = []

    monkeypatch.setattr(cli_run, "get_user_selections", lambda flags: _selections())
    monkeypatch.setattr(cli_run, "_build_run_config", lambda selections, checkpoint: dict(config))
    monkeypatch.setattr(cli_run, "TradingAgentsGraph", lambda *a, **k: graph_calls.append(a))
    monkeypatch.setattr(cli_run, "Live", lambda *a, **k: live_blocks.append(a))

    real_mkdir = Path.mkdir

    def mkdir(self, *args, **kwargs):
        mkdir_calls.append(self)
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(sys, "stdin", _TtyStdin())

    with pytest.raises(ConfigValidationError) as caught:
        cli_run.run_analysis(flags={})

    assert {"max_tokens", "temperature"} <= {p.parameter for p in caught.value.problems}
    assert graph_calls == [] and live_blocks == [] and mkdir_calls == []


class _TtyStdin:
    def isatty(self):
        return True


def test_pf9_cli_prints_a_red_block_and_exits_non_zero(monkeypatch):
    import typer

    import cli.main as main

    def raise_invalid(**kwargs):
        raise ConfigValidationError([
            _problem("max_tokens", "must be > 0, got 0"),
            _problem("temperature", "expected a number, got 'hot'"),
        ])

    printed = []
    monkeypatch.setattr(main, "run_analysis", raise_invalid)
    monkeypatch.setattr(main.console, "print", lambda *args, **kw: printed.append(args[0]))

    with pytest.raises(typer.Exit) as exited:
        main.analyze(ctx=_InactiveContext(), checkpoint=None, clear_checkpoints=False,
                     portfolio=None, ticker=None, date=None, analysts=None, save=None,
                     show=None, html=None)

    assert exited.value.exit_code != 0
    output = "\n".join(str(line) for line in printed)
    assert "max_tokens" in output and "temperature" in output


def _problem(parameter, detail, code="invalid_number"):
    from tradingagents.config_validation import ConfigProblem

    return ConfigProblem(parameter, code, detail)


class _InactiveContext:
    invoked_subcommand = None


# ---------------------------------------------------------------------------
# PF-10 / PF-11 — extension keys, no import cycle, no vendor import
# ---------------------------------------------------------------------------


def test_pf10_unknown_extension_keys_stay_allowed():
    assert validate_config({"future_extension_key": object(), "another": [1, 2]}) == []


@pytest.mark.parametrize("key", ["max_debate_rounds", "max_risk_discuss_rounds",
                                 "holding_period_days", "news_article_limit",
                                 "global_news_article_limit"])
def test_pf10_non_negative_round_counts_accept_zero(key):
    assert validate_config({key: 0}) == []
    assert key in _parameters({key: -1})
    assert key in _parameters({key: True})


@pytest.mark.parametrize("key", ["max_tool_rounds", "global_news_lookback_days"])
def test_pf10_positive_integers_reject_zero(key):
    assert validate_config({key: 1}) == []
    assert key in _parameters({key: 0})
    assert key in _parameters({key: True})


def test_pf10_numeric_strings_from_the_environment_are_accepted():
    assert validate_config({"llm_max_retries": "6", "max_tokens": "512", "temperature": "0.2"}) == []
    assert "llm_max_retries" in _parameters({"llm_max_retries": True})
    assert "llm_max_retries" in _parameters({"llm_max_retries": "-1"})


def test_pf10_temperature_bounds():
    assert validate_config({"temperature": 0}) == []
    assert validate_config({"temperature": 2}) == []
    assert validate_config({"temperature": None}) == []
    for bad in (-0.1, 2.1, "hot", True):
        assert "temperature" in _parameters({"temperature": bad}), bad


def test_pf10_provider_names_are_case_insensitive_and_keys_are_never_required():
    assert validate_config({"llm_provider": "Ollama", "deep_think_provider": "openai_compatible",
                            "quick_think_provider": None}) == []
    for bad in ("", "   ", "nonesuch", 3):
        assert "llm_provider" in _parameters({"llm_provider": bad}), bad


# ---------------------------------------------------------------------------
# PF-12 — a null primary provider is an error, not a silent fallback
# ---------------------------------------------------------------------------

def test_pf12_a_null_primary_provider_is_reported_not_inherited():
    problems = validate_config({"llm_provider": None})
    assert [p.parameter for p in problems] == ["llm_provider"]
    assert problems[0].code == "missing_required_input"
    assert "must name a provider" in problems[0].detail


def test_pf12_a_null_primary_provider_never_reaches_the_factory(tmp_path, monkeypatch):
    """It must fail as a named problem, not as an ``AttributeError`` in a client builder."""
    config = _valid_config(tmp_path) | {"llm_provider": None}
    clients = _spy_clients(monkeypatch)
    with pytest.raises(ConfigValidationError) as caught:
        trading_graph.TradingAgentsGraph(config=config)
    assert "llm_provider" in {p.parameter for p in caught.value.problems}
    assert clients == []
    assert not (tmp_path / "results").exists() and not (tmp_path / "cache").exists()


def test_pf12_a_null_per_tier_provider_inherits_the_primary():
    assert validate_config({"llm_provider": "openai", "deep_think_provider": None,
                            "quick_think_provider": None}) == []


def test_pf12_an_absent_primary_provider_is_filled_from_defaults_not_reported():
    assert validate_config({}) == []
    assert validate_config({"deep_think_provider": None}) == []


def test_pf12_keyless_and_custom_model_identifiers_remain_valid(tmp_path):
    keyless = _valid_config(tmp_path) | {"llm_provider": "ollama", "backend_url": None,
                                         "deep_think_llm": "some-local-model:7b",
                                         "quick_think_llm": "some-local-model:7b"}
    custom = _valid_config(tmp_path) | {"llm_provider": "openai_compatible",
                                        "backend_url": "http://localhost:11434/v1",
                                        "deep_think_llm": "anything-at-all",
                                        "quick_think_llm": "anything-at-all"}
    assert validate_config(keyless) == [] and validate_config(custom) == []


def test_pf10_backend_urls(tmp_path):
    ok_urls = [None, "", "http://localhost:8000/v1", "https://example.com/v1"]
    assert validate_config({"backend_url": None, "deep_think_backend_url": "",
                            "quick_think_backend_url": "https://example.com/v1"}) == []
    assert all(validate_config({"backend_url": url}) == [] for url in ok_urls)
    for bad in ("localhost:8000", "ftp://x", "http://", "http://a b", "http://user:pw@h/v1"):
        assert "backend_url" in _parameters({"backend_url": bad}), bad


# ---------------------------------------------------------------------------
# PF-13 — a malformed or out-of-range port is a named problem
#
# Reported probe: ``backend_url="http://localhost:notaport/v1"`` and ``…:99999/v1`` both
# validated cleanly, because ``urlsplit`` only raises when ``.port`` is read.
# ---------------------------------------------------------------------------

BAD_PORTS = [
    "http://localhost:notaport/v1",
    "http://localhost:99999/v1",
    "http://localhost:-1/v1",
    "https://api.example.com:8080x/v1",
    "http://localhost:/v1",
    "http://[::1]extra/v1",
    "http://example.com:65536",
]


@pytest.mark.parametrize("url", BAD_PORTS)
@pytest.mark.parametrize("key", ["backend_url", "deep_think_backend_url", "quick_think_backend_url"])
def test_pf13_a_bad_port_is_named_for_every_url_key(key, url):
    problems = validate_config({key: url})
    assert [p.parameter for p in problems] == [key]
    assert problems[0].code == "invalid_url"
    assert "port" in problems[0].detail.lower()


@pytest.mark.parametrize("key", ["backend_url", "deep_think_backend_url", "quick_think_backend_url"])
def test_pf13_good_ports_are_accepted_for_every_url_key(key):
    for url in (None, "", "http://localhost:8000/v1", "https://example.com:443/v1",
                "http://[::1]:11434/v1", "http://localhost:0/v1", "https://example.com/v1"):
        assert validate_config({key: url}) == [], url


# Some ``urlsplit`` versions reject a bracket host with trailing data while splitting
# (``ValueError: Invalid IPv6 URL``), before ``.port`` is ever read; others accept it and rely
# on the explicit bracket-suffix check. Either way the caller must get the same actionable
# port report. The patch below simulates the strict parser for this value only.
@pytest.mark.parametrize("key", ["backend_url", "deep_think_backend_url", "quick_think_backend_url"])
def test_pf13_a_strict_urlsplit_still_names_the_malformed_bracket_suffix(key, monkeypatch):
    import tradingagents.config_validation as config_validation

    real_urlsplit = config_validation.urlsplit

    def strict_urlsplit(url, *args, **kwargs):
        if url == "http://[::1]extra/v1":
            raise ValueError("Invalid IPv6 URL")
        return real_urlsplit(url, *args, **kwargs)

    monkeypatch.setattr(config_validation, "urlsplit", strict_urlsplit)
    problems = validate_config({key: "http://[::1]extra/v1"})
    assert [p.parameter for p in problems] == [key]
    assert problems[0].code == "invalid_url"
    assert "port" in problems[0].detail.lower()
    # The strict path and the permissive path report the same thing.
    monkeypatch.setattr(config_validation, "urlsplit", real_urlsplit)
    assert validate_config({key: "http://[::1]extra/v1"}) == problems


def test_pf13_a_strict_urlsplit_report_never_echoes_a_secret(monkeypatch):
    import tradingagents.config_validation as config_validation

    def strict_urlsplit(url, *args, **kwargs):
        raise ValueError("Invalid IPv6 URL")

    monkeypatch.setattr(config_validation, "urlsplit", strict_urlsplit)
    problems = validate_config(
        {"backend_url": "http://user:sk-live-abcdef123456@[::1]extra/v1"}
    )
    text = str(problems[0])
    # A credential-bearing URL keeps the established credential diagnostic on both parsers.
    assert problems[0].code == "invalid_url"
    assert "must not embed credentials" in text
    assert "sk-live-abcdef123456" not in text and "user:" not in text
    assert "<redacted>" in text


def test_pf13_a_bad_port_fails_before_any_client_or_directory(tmp_path, monkeypatch):
    config = _valid_config(tmp_path) | {"backend_url": "http://localhost:notaport/v1"}
    clients = _spy_clients(monkeypatch)
    with pytest.raises(ConfigValidationError) as caught:
        trading_graph.TradingAgentsGraph(config=config)
    assert "backend_url" in {p.parameter for p in caught.value.problems}
    assert clients == []
    assert not (tmp_path / "results").exists() and not (tmp_path / "cache").exists()


def test_pf13_a_url_with_credentials_and_a_bad_port_never_echoes_the_secret():
    problems = validate_config({"backend_url": "http://user:sk-live-abcdef123456@host:notaport/v1"})
    text = str(problems[0])
    assert "sk-live-abcdef123456" not in text and "user:" not in text
    assert "<redacted>" in text


def test_pf10_credential_shaped_values_are_redacted():
    problems = validate_config({"backend_url": "http://user:sk-secretvalue123@host/v1"})
    text = str(problems[0])
    assert "sk-secretvalue123" not in text and "secretvalue" not in text
    assert "<redacted>" in text


def test_pf11_the_module_imports_without_the_graph_dataflow_or_provider_layer():
    code = (
        "import sys\n"
        "import tradingagents.config_validation as v\n"
        "assert v.validate_config({}) == []\n"
        "bad = [m for m in ('tradingagents.graph', 'tradingagents.dataflows', "
        "'tradingagents.llm_clients') if m in sys.modules]\n"
        "print(','.join(bad) or 'clean')"
    )
    output = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True,
                            text=True, encoding="utf-8")
    assert output.returncode == 0, output.stderr
    assert output.stdout.strip().splitlines()[-1] == "clean"


def test_pf11_the_real_defaults_are_valid():
    assert validate_config(default_config_module.DEFAULT_CONFIG) == []


def test_pf11_module_exposes_the_documented_surface():
    module = importlib.import_module("tradingagents.config_validation")
    assert callable(module.validate_config) and callable(module.effective_config)
    assert issubclass(module.ConfigValidationError, ValueError)
