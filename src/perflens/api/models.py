"""Pydantic v2 models describing the HTTP API and the agent command JSON.

These models are the source of truth for the OpenAPI schema, which in turn
generates the frontend's TypeScript types (`npm run typegen`). Hot-path
handlers return pre-serialized orjson responses — FastAPI skips response
validation for those but still documents the declared ``response_model``.

The agent command models document the flag-2/3 wire JSON. The wire protocol
itself (framing, flags) is FROZEN — these only describe the JSON payloads
the existing C agent already speaks.
"""

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Core / status
# ---------------------------------------------------------------------------

class Status(BaseModel):
    status: Literal['ok']
    agent_connected: bool
    agent_addr: Optional[str] = None
    total_samples: int
    chunk_count: int


class DataVersion(BaseModel):
    """Version stamp for the notify-and-fetch cycle. Broadcast over SSE
    (with event_types) and echoed by /api/per-event (without)."""
    chunk_count: int
    total_samples: int
    event_types: Optional[list[str]] = None


class ErrorResponse(BaseModel):
    error: str


# ---------------------------------------------------------------------------
# Profile data
# ---------------------------------------------------------------------------

class FlamegraphNode(BaseModel):
    name: str
    value: int
    children: list['FlamegraphNode'] = Field(default_factory=list)
    inlined: Optional[bool] = None
    module: Optional[str] = None


class FunctionEntry(BaseModel):
    name: str
    module: str
    samples: int
    percent: float
    self_samples: int
    self_percent: float
    total_samples: int
    total_percent: float


class FunctionSummary(BaseModel):
    total_samples: int
    functions: list[FunctionEntry]


class ThreadRef(BaseModel):
    tid: int
    comm: str


class SourceFileRef(BaseModel):
    path: str
    found: bool
    total_samples: int
    functions: list[str]


class SourceLine(BaseModel):
    line_no: int
    text: str
    samples: int
    percent: float
    model_config = ConfigDict(extra='allow')


class PerEventEntry(BaseModel):
    function_summary: FunctionSummary
    flamegraph: FlamegraphNode
    source_files: list[SourceFileRef]
    threads: list[ThreadRef]
    # Replay/import only: {file_path: {lines: [...]}} annotated source
    source: Optional[dict[str, Any]] = None


class PerEventResponse(BaseModel):
    """GET /api/per-event?event=<evt>"""
    event: str
    data: PerEventEntry
    version: DataVersion


class PerEventAllResponse(BaseModel):
    """GET /api/per-event (no event param)"""
    per_event: dict[str, PerEventEntry]
    version: DataVersion


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class SessionMetadata(BaseModel):
    model_config = ConfigDict(extra='allow')

    version: str = ''
    session_id: str
    agent: str = ''
    timestamp: str = ''
    total_samples: int = 0
    chunks: int = 0
    event_types: list[str] = Field(default_factory=list)
    perf_stat: dict[str, Any] = Field(default_factory=dict)
    platform: Optional[dict[str, Any]] = None
    metrics_summary: Optional[dict[str, Any]] = None


class SessionReplayResponse(BaseModel):
    metadata: SessionMetadata
    per_event: dict[str, PerEventEntry]
    metrics: Optional[dict[str, Any]] = None


class ImportResponse(BaseModel):
    session_id: str
    total_samples: int
    event_types: list[str]


class StopResponse(BaseModel):
    stopped: bool
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Threads / time window / source
# ---------------------------------------------------------------------------

class ThreadTopFunction(BaseModel):
    name: str
    samples: int
    percent: float


class ThreadSummaryEntry(BaseModel):
    tid: int
    comm: str
    samples: int
    percent: float
    top_function: str
    top_function_samples: int
    top_functions: list[ThreadTopFunction]


class ThreadSummaryResponse(BaseModel):
    total_samples: int
    threads: list[ThreadSummaryEntry]


class ThreadViewResponse(BaseModel):
    flamegraph: FlamegraphNode
    function_summary: FunctionSummary
    source_files: list[SourceFileRef] = Field(default_factory=list)


class TimeWindow(BaseModel):
    start: float
    end: float
    samples: int


class TimeWindowResponse(BaseModel):
    flamegraph: FlamegraphNode
    function_summary: FunctionSummary
    window: TimeWindow


class SourceResponse(BaseModel):
    file: str
    lines: list[dict[str, Any]]
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Index / metrics / browse
# ---------------------------------------------------------------------------

class IndexStatus(BaseModel):
    model_config = ConfigDict(extra='allow')
    indexing: bool = False
    symbols_loaded: int = 0
    source_files_found: int = 0


class IndexFilesResponse(BaseModel):
    total: int
    offset: int
    limit: int
    files: list[str]


class MetricsFrame(BaseModel):
    """One health-metrics snapshot from the agent (flag-4 frame). The
    agent's shape varies by type and platform — modeled loosely on
    purpose; `type` is system|process|network|disk|threads."""
    model_config = ConfigDict(extra='allow')
    type: str = ''
    ts: float = 0


class BrowseEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: Optional[int] = None


class BrowseResponse(BaseModel):
    path: str
    parent: str
    entries: list[BrowseEntry]


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

class WizardState(BaseModel):
    model_config = ConfigDict(extra='allow')

    step: int = 0
    agent_host: str = ''
    agent_port: int = 9999
    connected: bool = False
    perf_verified: bool = False
    binary_path: str = ''
    source_dir: str = ''
    pid: Optional[int] = None
    process_name: str = ''
    frequency: int = 99
    duration: int = 8


# ---------------------------------------------------------------------------
# Agent control (HTTP side)
# ---------------------------------------------------------------------------

class ConnectRequest(BaseModel):
    host: str
    port: int = 9999


class AgentHello(BaseModel):
    """The agent's flag-3 hello payload (frozen wire JSON)."""
    model_config = ConfigDict(extra='allow')
    type: Literal['hello'] = 'hello'
    version: str = ''
    platform: dict[str, Any] = Field(default_factory=dict)
    token: Optional[str] = None


class ConnectResponse(BaseModel):
    ok: bool
    hello: Optional[AgentHello] = None
    addr: Optional[str] = None
    error: Optional[str] = None


class AgentCommandRequest(BaseModel):
    """Generic command relay body for POST /api/agent/command."""
    cmd: str = ''
    args: dict[str, Any] = Field(default_factory=dict)
    timeout: int = 60


class AgentCommandResponse(BaseModel):
    model_config = ConfigDict(extra='allow')
    ok: bool = True
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Agent command payloads (frozen flag-2 wire JSON, documented here)
# ---------------------------------------------------------------------------

class StartArgs(BaseModel):
    """start: begin collection on a PID. `events` may narrow the probed
    record-event set; frequency/duration override the agent defaults."""
    pid: int
    frequency: Optional[int] = None
    duration: Optional[int] = None
    events: Optional[list[str]] = None


class ConfigureArgs(BaseModel):
    """configure: change sampling parameters mid-session."""
    frequency: Optional[int] = None
    duration: Optional[int] = None


class ConfigureMetricsArgs(BaseModel):
    """configure_metrics: toggle opt-in collectors / interval on the agent."""
    enabled: Optional[bool] = None
    network: Optional[bool] = None
    disk: Optional[bool] = None
    threads: Optional[bool] = None
    interval: Optional[int] = None


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AgentCommand(BaseModel):
    """One flag-2 command frame: {id, cmd, args}. The documented command
    set of the frozen agent; used for OpenAPI/TS documentation now and
    server-side enforcement in API v2."""
    id: str
    cmd: Literal['ping', 'status', 'start', 'stop', 'pause', 'resume',
                 'configure', 'configure_metrics', 'list_processes',
                 'reprobe', 'probe_binary']
    args: Union[StartArgs, ConfigureArgs, ConfigureMetricsArgs,
                dict[str, Any]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------

class PathConfigRequest(BaseModel):
    path: str = ''


class PathMapConfigRequest(BaseModel):
    path_map: dict[str, str] = Field(default_factory=dict)


class ToolchainConfigRequest(BaseModel):
    prefix: str = ''
    sysroot: Optional[str] = None


class ConfigResult(BaseModel):
    model_config = ConfigDict(extra='allow')
    ok: bool
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# SSE event catalog (documentation; payloads reuse the models above)
# ---------------------------------------------------------------------------

class SSEStatusEvent(BaseModel):
    """`status` event: agent connect/disconnect."""
    connected: bool
    agent: Optional[str] = None


class SSEAgentConnectedEvent(BaseModel):
    """`agent_connected` event: fired once per new agent session."""
    agent: str
    platform: dict[str, Any] = Field(default_factory=dict)


class SSECatalog(BaseModel):
    """Not an endpoint — enumerates every event on GET /api/stream so the
    payload models land in the OpenAPI components for TS generation.

    Events: `status` (SSEStatusEvent), `agent_connected`
    (SSEAgentConnectedEvent), `event_types` (list[str]), `data_version`
    (DataVersion, with event_types), `perf_stat` (dict), `metrics_system`
    / `metrics_process` / `metrics_network` / `metrics_disk` /
    `metrics_threads` (MetricsFrame).
    """
    status: SSEStatusEvent
    agent_connected: SSEAgentConnectedEvent
    event_types: list[str]
    data_version: DataVersion
    perf_stat: dict[str, Any]
    metrics: MetricsFrame
