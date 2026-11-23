"""Startup configuration preflight (ADR-002 section 3, requirement FR-A).

One validator for every configuration key the graph and the CLI actually read, so a
malformed setting fails **before** any LLM client is constructed and **before** any
directory is created, naming every offending parameter at once instead of one per attempt.

Layering: this is a leaf module. It imports the standard library and
``tradingagents.default_config`` only. ``default_config`` is itself a leaf imported during
``tradingagents.dataflows`` initialisation, so importing this module from there would cycle;
that is why :func:`validate_config` is imported *inside*
``tradingagents.default_config.build_default_config`` and the env-derived defaults are
validated there. No provider registry, no vendor SDK and no credential is imported or read.

Three properties are deliberate:

* A value the caller **supplied** is never replaced by a default. A partial configuration is
  normal and is completed by :func:`effective_config`; a supplied value that is wrong is
  reported, never silently corrected.
* Nothing that used to be legal becomes illegal: any model identifier is accepted (local
  servers, relays and OpenAI-compatible endpoints take arbitrary names), no API key is
  required, and unknown extension keys are ignored.
* A reported detail never echoes a value that looks like a credential: it is replaced with
  ``<redacted>`` before it can reach a log line.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

# Layering: this module must not import the graph, the dataflow layer or a provider SDK. The
# only repository module it names is ``tradingagents.default_config``, and it is imported
# *inside* the functions below. ``default_config.build_default_config`` imports this module
# (ADR-002 section 3), so a module-level import of ``DEFAULT_CONFIG`` here would either
# re-execute this file or read a name that is not bound yet; deferring it avoids both.
_DEFAULT_CONFIG_MODULE = "tradingagents.default_config"


def _default_config() -> dict:
    """The process defaults, or ``{}`` while ``default_config`` is still building them."""
    import importlib

    module = importlib.import_module(_DEFAULT_CONFIG_MODULE)
    return dict(getattr(module, "DEFAULT_CONFIG", {}))


def _configured() -> bool:
    """Whether ``tradingagents.default_config`` has finished building its defaults."""
    import importlib

    module = importlib.import_module(_DEFAULT_CONFIG_MODULE)
    return hasattr(module, "DEFAULT_CONFIG")


def _contract_modes() -> tuple[str, ...]:
    """The accepted contract modes, read from the module that owns them (no cycle)."""
    import importlib

    module = importlib.import_module(_DEFAULT_CONFIG_MODULE)
    return tuple(module.AGENT_CONTRACT_MODES)

# Providers the client factory can build. Kept here as a literal list rather than imported
# from the provider registry: the registry lives above this layer and imports SDK modules.
# ``openai_compatible`` and ``ollama`` are keyless on purpose and stay accepted.
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "openai", "anthropic", "google", "azure", "bedrock", "xai", "deepseek", "qwen", "qwen-cn",
    "glm", "glm-cn", "minimax", "minimax-cn", "openrouter", "mistral", "kimi", "groq", "nvidia",
    "ollama", "chatgpt", "openai_compatible",
)

_REDACTED = "<redacted>"
# A value shaped like a credential must never appear in a detail. The patterns are
# deliberately broad: a false redaction costs a reader one extra look, a leaked key costs
# much more.
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b(?:api[_-]?key|token|secret|password|passwd|pwd)\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
)
_NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_INT_RE = re.compile(r"^[+-]?\d+$")


def redact(text: str) -> str:
    """``text`` with anything credential-shaped replaced by ``<redacted>``."""
    redacted = text
    for pattern in _SECRET_SHAPES:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


@dataclass(frozen=True)
class ConfigProblem:
    """One configuration problem: the key path, a stable code and a neutral detail."""

    parameter: str
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"parameter": self.parameter, "code": self.code, "detail": self.detail}

    def __str__(self) -> str:
        return f"{self.parameter}: {self.detail}"


class ConfigValidationError(ValueError):
    """Raised when a configuration has at least one problem.

    ``problems`` holds every problem found, so a caller can print them all; the message
    names each one. Values are already redacted.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        joined = "; ".join(str(problem) for problem in self.problems)
        super().__init__(f"invalid configuration: {joined}")


def effective_config(config: Mapping | None) -> dict:
    """``config`` over the defaults, one level deep for mapping values.

    Every key of :data:`DEFAULT_CONFIG` the caller omitted is filled in; a key the caller
    supplied keeps its value exactly, including an invalid one, so the validator can report
    it rather than a default masking it.
    """
    merged = copy.deepcopy(_default_config())
    for key, value in (config or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **copy.deepcopy(value)}
        else:
            merged[key] = copy.deepcopy(value)
    return merged


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------


def _describe(value) -> str:
    """A short, redacted description of a supplied value for a detail message."""
    if isinstance(value, str):
        shown = value if len(value) <= 40 else value[:37] + "..."
        return redact(repr(shown))
    return redact(repr(value))


def _check_int(problems, config, key, *, minimum, allow_none=False, allow_numeric_string=False,
               code="invalid_integer"):
    """An integer key, optionally a numeric string (env vars arrive as strings)."""
    if key not in config:
        return None
    value = config[key]
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        problems.append(ConfigProblem(key, code, "a boolean is not an integer"))
        return None
    number = None
    if isinstance(value, int):
        number = value
    elif allow_numeric_string and isinstance(value, str) and _INT_RE.match(value.strip()):
        number = int(value.strip())
    if number is None:
        problems.append(ConfigProblem(key, code, f"expected an integer, got {_describe(value)}"))
        return None
    if number < minimum:
        problems.append(ConfigProblem(
            key, "out_of_range", f"must be {'>=' if minimum <= 0 else '>'} {minimum}, got {number}"
        ))
        return None
    return number


def _check_float(problems, config, key, *, low=None, high=None, code="invalid_number"):
    """A finite optional float in ``[low, high]``; a numeric string is accepted."""
    if key not in config:
        return None
    value = config[key]
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        problems.append(ConfigProblem(key, code, "a boolean is not a number"))
        return None
    number = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and _NUMERIC_RE.match(value.strip()):
        number = float(value.strip())
    if number is None:
        problems.append(ConfigProblem(key, code, f"expected a number, got {_describe(value)}"))
        return None
    if not math.isfinite(number):
        problems.append(ConfigProblem(key, "nonfinite_number", "a finite number is required"))
        return None
    if (low is not None and number < low) or (high is not None and number > high):
        problems.append(ConfigProblem(
            key, "out_of_range", f"must be between {low} and {high}, got {number}"
        ))
        return None
    return number


def _check_text(problems, config, key, *, allow_none=False, code="blank_string"):
    """A non-blank string key."""
    if key not in config:
        return None
    value = config[key]
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        problems.append(ConfigProblem(
            key, "wrong_type", f"expected text, got {_describe(value)}"
        ))
        return None
    if not value.strip():
        problems.append(ConfigProblem(key, code, "must not be blank"))
        return None
    return value.strip()


def _check_provider(problems, config, key):
    """A provider name the client factory supports (case insensitive).

    ``llm_provider`` is the primary provider: every client falls back to it, so an explicit
    ``None`` there is a configuration error, not \"use the default\" -- accepting it would let
    a missing primary reach the factory and fail later (or fall back to a different provider
    than the caller meant). A per-tier key (``deep_think_provider``, ``quick_think_provider``)
    may be ``None``: that inherits ``llm_provider``. Absence of any key is never checked.
    """
    if key not in config:
        return None
    primary = key == "llm_provider"
    if config[key] is None and primary:
        # One clear problem for a null primary provider, not a type error plus a vague follow-on.
        problems.append(ConfigProblem(
            key, "missing_required_input",
            "llm_provider must name a provider; only a per-tier *_provider may be null",
        ))
        return None
    value = _check_text(problems, config, key, allow_none=not primary)
    if value is None:
        return None
    name = value.strip().lower()
    if name not in SUPPORTED_PROVIDERS:
        problems.append(ConfigProblem(
            key, "unsupported_provider",
            f"unknown provider {_describe(value.strip())}; expected one of "
            + ", ".join(SUPPORTED_PROVIDERS),
        ))
        return None
    return name


def _url_authority(text: str) -> str | None:
    """The ``userinfo@host:port`` authority of an absolute URL, or ``None`` if it has none.

    A deterministic split of ``scheme://authority/...``. It only locates the structure
    ``urlsplit`` failed on; no component is normalised or validated here.
    """
    scheme, sep, rest = text.partition("://")
    if not sep or not scheme or "/" in scheme or "?" in scheme or "#" in scheme:
        return None
    end = len(rest)
    for delimiter in "/?#":
        found = rest.find(delimiter)
        if found >= 0:
            end = min(end, found)
    return rest[:end]


# The one diagnostic for a bracketed host followed by stray data. It is a constant on purpose:
# the suffix itself is never echoed, so a credential cannot leak through it, and the wording
# does not depend on which ``urlsplit`` (permissive or strict) examined the value.
_BRACKET_SUFFIX_DETAIL = (
    "a bracketed host must be followed by a port ('[host]:port') or end the authority"
)


def _has_bracket_suffix(text: str) -> bool:
    """Whether a bracketed host is followed by a non-colon, non-empty suffix.

    ``[::1]extra`` has one; ``[::1]``, ``[::1]:11434`` and ``[::1]:`` do not. The numeric port
    is not examined here -- the ``.port`` read below reports that. Whether ``urlsplit`` itself
    rejects such an authority varies by interpreter (CPython ``_check_bracketed_netloc`` raises
    ``Invalid IPv6 URL``; older code accepts it), so the rule is applied here directly.
    """
    authority = _url_authority(text)
    if authority is None:
        return False
    host_port = authority.rpartition("@")[2]
    _, bracket, bracketed = host_port.partition("[")
    if not bracket:
        return False
    if "]" not in bracketed:
        return True  # unterminated bracket: no valid port can follow it
    suffix = bracketed.split("]", 1)[1]
    return bool(suffix) and not suffix.startswith(":")


def _check_backend_url(problems, config, key):
    """``None``/blank, or an ``http``/``https`` URL with a host, no whitespace and no userinfo."""
    if key not in config:
        return None
    value = config[key]
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        problems.append(ConfigProblem(
            key, "invalid_url", f"expected a URL string, got {_describe(value)}"
        ))
        return None
    text = value.strip()
    if any(ch.isspace() for ch in text):
        problems.append(ConfigProblem(
            key, "invalid_url", f"a URL cannot contain whitespace: {_describe(text)}"
        ))
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        # A strict ``urlsplit`` (CPython ``_check_bracketed_netloc``) raises here for a
        # bracketed host with a trailing suffix, before ``.port`` can be read; older parsers
        # accept it and reach the explicit check below. Diagnose it here the same way, so the
        # report does not depend on which parser runs.
        authority = _url_authority(text)
        if authority is not None and "@" in authority:
            problems.append(ConfigProblem(
                key, "invalid_url", f"a URL must not embed credentials: {_REDACTED}"
            ))
            return None
        if _has_bracket_suffix(text):
            problems.append(ConfigProblem(
                key, "invalid_url", f"{_BRACKET_SUFFIX_DETAIL}: {_describe(text)}"
            ))
            return None
        problems.append(ConfigProblem(
            key, "invalid_url", f"could not be parsed as a URL: {_describe(text)}"
        ))
        return None
    if parts.scheme.lower() not in ("http", "https"):
        problems.append(ConfigProblem(
            key, "invalid_url",
            f"scheme must be http or https, got {_describe(parts.scheme or 'none')}",
        ))
        return None
    if "@" in (parts.netloc or ""):
        # A URL carrying credentials is reported without them.
        problems.append(ConfigProblem(
            key, "invalid_url", f"a URL must not embed credentials: {_REDACTED}"
        ))
        return None
    if not parts.hostname:
        problems.append(ConfigProblem(
            key, "invalid_url", f"a URL needs a host: {_describe(text)}"
        ))
        return None
    # ``hostname`` alone does not settle the port. ``urlsplit`` accepts ``:notaport`` and
    # ``:99999`` in the netloc and only fails when ``.port`` is actually read (``ValueError``
    # for a non-numeric port or one outside 0..65535). Reading it here, under a guard, is what
    # turns those into a named problem instead of a failure later inside a client builder.
    authority = parts.netloc.rsplit("@", 1)[-1]
    if authority.rstrip().endswith(":"):
        problems.append(ConfigProblem(
            key, "invalid_url", f"a URL port after ':' is empty: {_describe(text)}"
        ))
        return None
    if _has_bracket_suffix(text):
        problems.append(ConfigProblem(
            key, "invalid_url", f"{_BRACKET_SUFFIX_DETAIL}: {_describe(text)}"
        ))
        return None
    try:
        port = parts.port
    except ValueError:
        problems.append(ConfigProblem(
            key, "invalid_url",
            f"the port must be a number in 0..65535: {_describe(text)}",
        ))
        return None
    if port is not None and not 0 <= port <= 65535:  # defensive: urlsplit already guards this
        problems.append(ConfigProblem(
            key, "invalid_url", f"the port must be in 0..65535, got {port}"
        ))
        return None
    return text


def _check_text_mapping(problems, config, key):
    """A mapping with string keys and string values (a vendor selection container)."""
    if key not in config:
        return None
    value = config[key]
    if not isinstance(value, Mapping):
        problems.append(ConfigProblem(
            key, "wrong_container",
            f"expected a mapping of names to vendors, got {_describe(value)}",
        ))
        return None
    for name, vendor in value.items():
        if not isinstance(name, str):
            problems.append(ConfigProblem(
                f"{key}.{name}", "wrong_type", "a mapping key must be text"
            ))
            continue
        if not isinstance(vendor, str):
            problems.append(ConfigProblem(
                f"{key}.{name}", "wrong_type",
                f"a vendor name must be text, got {_describe(vendor)}",
            ))
    return value


def _check_mode(problems, config, key):
    """The contract mode vocabulary."""
    if key not in config:
        return None
    value = config[key]
    if value is None:
        return "off"
    if not isinstance(value, str):
        problems.append(ConfigProblem(
            key, "unsupported_mode", f"expected a mode name, got {_describe(value)}"
        ))
        return None
    text = value.strip().lower()
    modes = _contract_modes()
    if text not in modes:
        problems.append(ConfigProblem(
            key, "unsupported_mode",
            f"expected one of {', '.join(modes)}, got {_describe(value.strip())}",
        ))
        return None
    return text


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


def validate_config(config: Mapping | None) -> list[ConfigProblem]:
    """Every problem in ``config``, in a stable order; empty when the configuration is valid.

    Only keys the caller (or the environment overlay) supplied are checked for validity;
    absence is filled by :func:`effective_config` and is never a problem. Cross-field rules
    need both values, so they are evaluated on the effective configuration.

    The caller's mapping is not mutated.
    """
    supplied = dict(config or {})
    effective = effective_config(supplied) if _configured() else supplied
    problems: list[ConfigProblem] = []

    mode = _check_mode(problems, supplied, "agent_contract_mode")
    if "agent_contract_version" in supplied:
        # The version has a single source of truth (ADR section 2.10); ask the module that
        # owns it rather than duplicating the constant here.
        import importlib

        version = importlib.import_module(_DEFAULT_CONFIG_MODULE).AGENT_CONTRACT_VERSION
        if supplied["agent_contract_version"] != version:
            problems.append(ConfigProblem(
                "agent_contract_version", "unsupported_version",
                f"this build implements contract version {version}",
            ))

    if mode is not None and mode != "off" and "max_debate_rounds" not in supplied:
        problems.append(ConfigProblem(
            "max_debate_rounds", "missing_required_input",
            "an enhanced agent_contract_mode needs max_debate_rounds",
        ))

    for key in ("max_debate_rounds", "max_risk_discuss_rounds", "holding_period_days",
                "news_article_limit", "global_news_article_limit"):
        _check_int(problems, supplied, key, minimum=0)
    for key in ("max_recur_limit", "max_tool_rounds", "global_news_lookback_days"):
        _check_int(problems, supplied, key, minimum=1)
    _check_int(problems, supplied, "max_tokens", minimum=1, allow_none=True,
               allow_numeric_string=True)
    _check_int(problems, supplied, "llm_max_retries", minimum=0, allow_none=True,
               allow_numeric_string=True)
    _check_int(problems, supplied, "memory_log_max_entries", minimum=0, allow_none=True)

    _check_float(problems, supplied, "temperature", low=0, high=2)

    for key in ("deep_think_llm", "quick_think_llm"):
        _check_text(problems, supplied, key)
    for key in ("llm_provider", "deep_think_provider", "quick_think_provider"):
        _check_provider(problems, supplied, key)
    for key in ("backend_url", "deep_think_backend_url", "quick_think_backend_url"):
        _check_backend_url(problems, supplied, key)

    if "checkpoint_enabled" in supplied and not isinstance(supplied["checkpoint_enabled"], bool):
        problems.append(ConfigProblem(
            "checkpoint_enabled", "wrong_type",
            f"expected true or false, got {_describe(supplied['checkpoint_enabled'])}",
        ))

    for key in ("data_vendors", "tool_vendors", "benchmark_map"):
        _check_text_mapping(problems, supplied, key)

    _check_text(problems, supplied, "benchmark_ticker", allow_none=True)
    _check_text(problems, supplied, "output_language")

    # Cross-field: an analyst takes two graph steps per tool round plus its first turn and
    # its wrap-up. A limit past the recursion limit would end the run there instead.
    tool_rounds = _effective_int(effective, "max_tool_rounds")
    recur_limit = _effective_int(effective, "max_recur_limit")
    if tool_rounds is not None and recur_limit is not None and 2 * tool_rounds + 2 >= recur_limit:
        problems.append(ConfigProblem(
            "max_tool_rounds", "recursion_limit_too_small",
            f"max_tool_rounds={tool_rounds} needs max_recur_limit above {2 * tool_rounds + 2}, "
            f"got {recur_limit}",
        ))

    return problems


def _effective_int(config: Mapping, key: str) -> int | None:
    value = config.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def require_valid_config(config: Mapping | None) -> dict:
    """Raise :class:`ConfigValidationError` with every problem, else return the effective config."""
    problems = validate_config(config)
    if problems:
        raise ConfigValidationError(problems)
    return effective_config(config)


__all__ = [
    "ConfigProblem",
    "ConfigValidationError",
    "SUPPORTED_PROVIDERS",
    "effective_config",
    "redact",
    "require_valid_config",
    "validate_config",
]
