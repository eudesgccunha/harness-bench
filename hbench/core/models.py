"""Versioned data contract: frozen dataclasses that validate themselves and round-trip through dicts.

`to_dict()` gives a JSON-ready dict (enums as values, datetimes as ISO 8601, tuples as lists).
`from_dict()` rejects unknown and missing fields and any `schema_version` with another major version.
Unknown cost is `None`, never 0.
"""

import dataclasses
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum, StrEnum
from types import MappingProxyType, UnionType
from typing import Any, ClassVar, Self, get_args, get_origin, get_type_hints

SCHEMA_VERSION = "1.5.0"
_VERSION_RE = re.compile(r"^(\d+)\.\d+\.\d+$")
_MAJOR = SCHEMA_VERSION.split(".")[0]
_RUN_ID_RE = re.compile(r"[0-9a-f]{32}")  # `new_run_id()`: a UUID4 in hex
_TRIAL_ID_RE = re.compile(r"[0-9a-f]{16}")  # `trial_id()`: 16 hex characters
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ContractError(ValueError):
    """Data does not satisfy the contract."""


class CostSource(StrEnum):
    GATEWAY = "gateway"
    PRICE_TABLE = "price_table"
    HARNESS = "harness"
    UNKNOWN = "unknown"


class TrialStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"


# ---------------------------------------------------------------- helpers


def _plain(value: object) -> Any:
    """JSON-ready form of a contract value."""
    if isinstance(value, _Model):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _identity(value: Any) -> Any:
    return value


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ContractError(f"expected an ISO 8601 string, got {describe_value(value)}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as e:
        raise ContractError(f"invalid ISO 8601 timestamp ({describe_value(value)})") from e


def _optional(decode: Callable[[Any], Any]) -> Callable[[Any], Any]:
    return lambda value: None if value is None else decode(value)


def _tuple_of(decode: Callable[[Any], Any]) -> Callable[[Any], tuple[Any, ...]]:
    def decode_all(value: Any) -> tuple[Any, ...]:
        if not isinstance(value, list):
            raise ContractError(f"expected a list, got {describe_value(value)}")
        return tuple(decode(v) for v in value)

    return decode_all


def _require_aware(name: str, value: datetime) -> None:
    if value.utcoffset() is None:
        raise ContractError(f"{name} must be timezone-aware")


def _check_schema_version(version: str) -> None:
    match = _VERSION_RE.match(version)
    if match is None:
        raise ContractError(f"invalid schema_version {version!r}")
    if match.group(1) != _MAJOR:
        raise ContractError(f"unsupported schema_version {version!r}: this reader understands {_MAJOR}.x.y")


def _check_non_negative(name: str, value: float | None) -> None:
    """Known numbers must be finite and >= 0 (types are checked before, so bool and str never get here)."""
    if value is not None and not (math.isfinite(value) and value >= 0):
        raise ContractError(f"{name} must be a finite number >= 0, got {value}")


def _check_unit(name: str, value: float) -> None:
    """A rate: a number in [0, 1] (NaN and infinity fail the comparison)."""
    if not 0 <= value <= 1:
        raise ContractError(f"{name} must be in [0, 1], got {value}")


def describe_value(value: object) -> str:
    """A value for an error message: numbers as they are, anything else (it may be long or hold content) by type."""
    if isinstance(value, int | float):
        return repr(value)
    return f"a {type(value).__name__}"


def _matches(value: object, expected: Any) -> bool:
    """Whether `value` fits the annotation `expected`: the shapes the contract models use, bool never an int."""
    if expected is type(None):
        return value is None
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
    origin, args = get_origin(expected), get_args(expected)
    if origin is UnionType:
        return any(_matches(value, option) for option in args)
    if origin is tuple:
        return isinstance(value, tuple) and all(_matches(v, args[0]) for v in value)
    if origin is Mapping:
        return isinstance(value, Mapping) and all(
            _matches(k, args[0]) and _matches(v, args[1]) for k, v in value.items()
        )
    return isinstance(value, expected)


@dataclass(frozen=True, kw_only=True)
class _Model:
    """Base of every contract model: dict conversion and strict field checking."""

    # Keys that `to_dict` adds and `from_dict` accepts but derives again (never stored on the instance).
    DERIVED_KEYS: ClassVar[tuple[str, ...]] = ()
    # Per-field decoders applied by `from_dict`; fields not listed are taken as they are.
    DECODERS: ClassVar[Mapping[str, Callable[[Any], Any]]] = {}

    def __post_init__(self) -> None:
        """Every field must fit its annotation; models with more checks call this first."""
        hints = get_type_hints(type(self))
        for f in fields(self):
            value = getattr(self, f.name)
            if not _matches(value, hints[f.name]):
                raise ContractError(
                    f"{type(self).__name__}.{f.name}: expected {hints[f.name]}, got {describe_value(value)}"
                )

    @classmethod
    def required_keys(cls) -> frozenset[str]:
        """Keys a serialized form must carry: fields without a default, `schema_version` and derived keys."""
        plain = {f.name for f in fields(cls) if f.default is dataclasses.MISSING}
        versioned = {"schema_version"} & {f.name for f in fields(cls)}
        return frozenset(plain | versioned | set(cls.DERIVED_KEYS))

    def to_dict(self) -> dict[str, Any]:
        return {f.name: _plain(getattr(self, f.name)) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Self:
        names = {f.name for f in fields(cls)}
        extra = set(d) - names - set(cls.DERIVED_KEYS)
        missing = cls.required_keys() - set(d) - set(cls.DERIVED_KEYS)
        if extra or missing:
            raise ContractError(f"{cls.__name__}: unknown fields {sorted(extra)}, missing fields {sorted(missing)}")
        try:
            obj = cls(**{k: cls.DECODERS.get(k, _identity)(v) for k, v in d.items() if k in names})
        except ContractError:
            raise
        except (ValueError, TypeError) as e:
            raise ContractError(f"{cls.__name__}: {e}") from e
        obj._check_derived(d)
        return obj

    def _check_derived(self, d: Mapping[str, Any]) -> None:
        """Hook for models with derived keys: the stored values must agree with the recomputed ones."""


# ---------------------------------------------------------------- models


@dataclass(frozen=True, kw_only=True)
class Target(_Model):
    """What is being measured: a harness at a version driving a model with parameters."""

    harness: str
    harness_version: str
    model: str
    params: Mapping[str, str | int | float | bool]

    def __post_init__(self) -> None:
        super().__post_init__()
        # A private read-only copy: the caller's dict cannot change a frozen model afterwards.
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    def __reduce__(self) -> tuple[Any, ...]:
        # MappingProxyType cannot be pickled or deep-copied: rebuild from a plain dict instead.
        return (_make_target, (self.harness, self.harness_version, self.model, dict(self.params)))

    def __hash__(self) -> int:
        return hash((self.harness, self.harness_version, self.model, tuple(sorted(self.params.items()))))

    @property
    def target_id(self) -> str:
        return f"{self.harness}@{self.harness_version}:{self.model}"


def _make_target(harness: str, harness_version: str, model: str, params: dict[str, Any]) -> Target:
    return Target(harness=harness, harness_version=harness_version, model=model, params=params)


@dataclass(frozen=True, kw_only=True)
class GraderResult(_Model):
    grader: str
    grader_version: str
    passed: bool
    score: float | None = None
    timed_out: bool = False
    details_uri: str | None = None
    judge: str | None = None  # the model that judged the artifact (llm_rubric); None for every other grader

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.score is not None and not 0 <= self.score <= 1:
            raise ContractError(f"score must be in [0, 1], got {self.score}")


@dataclass(frozen=True, kw_only=True)
class Costs(_Model):
    """Every known cost of a trial in USD; `None` means unknown (never 0)."""

    gateway: float | None = None
    price_table: float | None = None
    harness: float | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in ("gateway", "price_table", "harness"):
            _check_non_negative(f"cost {name}", getattr(self, name))

    def pick(self, source: CostSource | None = None) -> tuple[float | None, CostSource]:
        """The cost and its source: the forced `source`, or by order gateway > price_table > harness."""
        by_source = {
            CostSource.GATEWAY: self.gateway,
            CostSource.PRICE_TABLE: self.price_table,
            CostSource.HARNESS: self.harness,
        }
        order = tuple(by_source) if source is None else (source,)
        for candidate in order:
            value = by_source.get(candidate)
            if value is not None:
                return value, candidate
        return None, CostSource.UNKNOWN


_COUNT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "turns",
    "tool_calls",
)


@dataclass(frozen=True, kw_only=True)
class Usage(_Model):
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    turns: int = 0
    parsed: bool
    costs: Costs
    tool_calls: int | None = None

    DERIVED_KEYS: ClassVar[tuple[str, ...]] = ("cost_usd", "cost_source")
    DECODERS: ClassVar[Mapping[str, Callable[[Any], Any]]] = {"costs": Costs.from_dict}

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in _COUNT_FIELDS:
            _check_non_negative(name, getattr(self, name))

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def cost_usd(self) -> float | None:
        return self.costs.pick()[0]

    @property
    def cost_source(self) -> CostSource:
        return self.costs.pick()[1]

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "cost_usd": self.cost_usd, "cost_source": self.cost_source.value}

    def _check_derived(self, d: Mapping[str, Any]) -> None:
        stored = {k: d[k] for k in self.DERIVED_KEYS if k in d}
        if stored and stored != {k: v for k, v in self.to_dict().items() if k in stored}:
            raise ContractError(f"Usage: {stored} disagrees with the costs {self.costs}")


@dataclass(frozen=True, kw_only=True)
class TrialResult(_Model):
    """One attempt of one task by one target. Holds references only: no prompts, stdout or diffs."""

    schema_version: str = SCHEMA_VERSION
    run_id: str
    trial_id: str
    task_id: str
    suite_version: str
    target: Target
    rep: int
    status: TrialStatus
    graders: tuple[GraderResult, ...]
    usage: Usage
    latency_s: float
    started_at: datetime
    agent_exit_code: int | None
    sandboxed: bool
    image_digest: str | None = None
    trace_id: str | None = None
    artifact_uri: str | None = None

    DECODERS: ClassVar[Mapping[str, Callable[[Any], Any]]] = {
        "target": Target.from_dict,
        "status": TrialStatus,
        "graders": _tuple_of(GraderResult.from_dict),
        "usage": Usage.from_dict,
        "started_at": _parse_datetime,
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_schema_version(self.schema_version)
        if self.rep < 1:
            raise ContractError(f"rep must be >= 1, got {self.rep}")
        _check_non_negative("latency_s", self.latency_s)
        _require_aware("started_at", self.started_at)

    @property
    def passed(self) -> bool:
        return self.status is TrialStatus.PASSED


@dataclass(frozen=True, kw_only=True)
class RunInfo(_Model):
    """Metadata of one `run` invocation (`run.json`)."""

    schema_version: str = SCHEMA_VERSION
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    harness_bench_version: str
    config_sha256: str
    suite_version: str
    python: str
    platform: str
    sandboxed: bool
    targets: tuple[Target, ...]
    sandbox: str = "unknown"  # what ran the commands: `docker:<image>`, `prefix`, `local-tmp`, a plugin's name
    reps: int | None = None  # repetitions per task and pair the run plans; None: not recorded (the config says)

    DECODERS: ClassVar[Mapping[str, Callable[[Any], Any]]] = {
        "started_at": _parse_datetime,
        "finished_at": _optional(_parse_datetime),
        "targets": _tuple_of(Target.from_dict),
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_schema_version(self.schema_version)
        _require_aware("started_at", self.started_at)
        if self.finished_at is not None:
            _require_aware("finished_at", self.finished_at)
        if self.reps is not None and self.reps < 1:
            raise ContractError(f"reps must be >= 1, got {self.reps}")


@dataclass(frozen=True, kw_only=True)
class TrialSpec(_Model):
    """Names one trial of a run so any worker can run it (ADR 0012). It carries no command, prompt, path, env or
    secret: the worker resolves everything from its own copy of the config, which `config_sha256` must match."""

    schema_version: str = SCHEMA_VERSION
    run_id: str
    trial_id: str
    task_id: str
    harness: str
    model: str
    rep: int
    config_sha256: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_schema_version(self.schema_version)
        for name, pattern in (
            ("run_id", _RUN_ID_RE),
            ("trial_id", _TRIAL_ID_RE),
            ("config_sha256", _SHA256_RE),
        ):
            if pattern.fullmatch(getattr(self, name)) is None:
                raise ContractError(f"TrialSpec.{name} must match {pattern.pattern}")
        for name in ("task_id", "harness", "model"):
            if not getattr(self, name):
                raise ContractError(f"TrialSpec.{name} must not be empty")
        if self.rep < 1:
            raise ContractError(f"rep must be >= 1, got {self.rep}")


# The cost source names of `--cost-source` and the configs (`report.COST_SOURCES` repeats them; a test checks it).
COST_SOURCE_NAMES = ("auto", "litellm", "computed", "reported")
# The constraints `recommend` accepts, and so the only keys a `TargetDecision` may record.
CONSTRAINT_KEYS = ("min_pass_rate", "max_cost_per_solved", "max_cost_per_run", "max_latency_s")


@dataclass(frozen=True, kw_only=True)
class Evidence(_Model):
    """The measured figures behind a `TargetDecision`; `None` means unknown (never 0)."""

    runs: int
    solved: int
    pass_rate: float
    pass_ci_low: float
    pass_ci_high: float
    cost_per_solved: float | None
    cost_per_run: float | None
    avg_wall_s: float | None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.runs < 1 or not 0 <= self.solved <= self.runs:
            raise ContractError(f"need 0 <= solved <= runs and runs >= 1, got {self.solved}/{self.runs}")
        for name in ("pass_rate", "pass_ci_low", "pass_ci_high"):
            _check_unit(name, getattr(self, name))
        if abs(self.pass_rate - self.solved / self.runs) > 1e-9:
            raise ContractError(f"pass_rate {self.pass_rate} is not solved / runs ({self.solved}/{self.runs})")
        if not self.pass_ci_low - 1e-9 <= self.pass_rate <= self.pass_ci_high + 1e-9:
            raise ContractError("the pass-rate interval must contain pass_rate")
        for name in ("cost_per_solved", "cost_per_run", "avg_wall_s"):
            _check_non_negative(name, getattr(self, name))


@dataclass(frozen=True, kw_only=True)
class TargetDecision(_Model):
    """The winning harness x model of a run (ADR 0013), as a versionable file. It holds no command, env, secret or
    path: only what names the target, the run it was measured in, the constraints given and the evidence."""

    schema_version: str = SCHEMA_VERSION
    harness: str
    model: str
    run_id: str
    config_sha256: str
    suite_version: str
    cost_source: str
    constraints: Mapping[str, float]
    evidence: Evidence

    DECODERS: ClassVar[Mapping[str, Callable[[Any], Any]]] = {"evidence": Evidence.from_dict}

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_schema_version(self.schema_version)
        for name in ("harness", "model", "suite_version"):
            if not getattr(self, name):
                raise ContractError(f"TargetDecision.{name} must not be empty")
        if _RUN_ID_RE.fullmatch(self.run_id) is None:
            raise ContractError(f"TargetDecision.run_id must match {_RUN_ID_RE.pattern}")
        if self.cost_source not in COST_SOURCE_NAMES:
            raise ContractError(f"TargetDecision.cost_source must be one of {', '.join(COST_SOURCE_NAMES)}")
        if _SHA256_RE.fullmatch(self.config_sha256) is None:
            raise ContractError(f"TargetDecision.config_sha256 must match {_SHA256_RE.pattern}")
        unknown = sorted(set(self.constraints) - set(CONSTRAINT_KEYS))
        if unknown:
            raise ContractError(f"TargetDecision.constraints: unknown constraint(s) {unknown}")
        for key, value in self.constraints.items():
            if key == "min_pass_rate":
                _check_unit(key, value)
            else:
                _check_non_negative(key, value)
        # A private read-only copy: the caller's dict cannot change a frozen model afterwards.
        object.__setattr__(self, "constraints", MappingProxyType(dict(self.constraints)))
