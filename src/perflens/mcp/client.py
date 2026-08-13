"""Async HTTP client for the PerfLens REST API.

The MCP server is a second consumer of the same API the web UI uses — it
holds no profiling state of its own. Everything here is a thin call plus
two pieces of value the tools depend on:

  * error translation — the API's {"error": {code, message}} envelope
    becomes a `PerfLensError` whose text tells the agent what to do next,
    not just what went wrong;
  * a replay cache — a saved session's replay payload is hundreds of KB
    and an investigation touches it repeatedly, so it is fetched once.
"""

import asyncio
import json
import time

import httpx

DEFAULT_URL = 'http://127.0.0.1:8080'

# A session replay can take a while to rebuild from raw chunks the first
# time; agent commands have their own (longer) budget passed per call.
DEFAULT_TIMEOUT = 60.0

# Live data is identified by this pseudo session id everywhere in the tools.
LIVE = 'live'


class PerfLensError(Exception):
    """A tool-level failure with an actionable next step in the message."""


def _envelope_message(payload, status):
    """Pull {"error": {code, message}} out of a response body."""
    if isinstance(payload, dict):
        err = payload.get('error')
        if isinstance(err, dict):
            return err.get('code') or '', err.get('message') or ''
    return '', f'HTTP {status}'


# code -> what the agent should do about it. The API's slugs are stable
# (they are part of the documented error model), so this mapping is too.
_HINTS = {
    'no_agent': ("No agent is connected. Use perflens_agent_connect(host, "
                 "port) to reach an agent started with --listen, or start "
                 "the agent on the device with --server <this host>."),
    'no_mapper': ("Source mapping is unavailable. The server needs --binary "
                  "pointing at an unstripped build (compiled with -g)."),
    'no_data': ("No profiling data yet. Check perflens_status, or start "
                "collection with perflens_start_profiling(pid)."),
    'no_perf': "perf was not found on the server, so perf.data import is unavailable.",
    'agent_transport': ("The agent link dropped mid-command. Re-check "
                        "perflens_agent_info before retrying."),
}


class PerfLensClient:
    """Async client over the PerfLens HTTP API."""

    def __init__(self, base_url=DEFAULT_URL, timeout=DEFAULT_TIMEOUT,
                 transport=None):
        """`transport` lets the tests drive a real ASGI app in-process
        instead of opening a socket; production always uses the default."""
        self.base_url = base_url.rstrip('/')
        self._timeout = timeout
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout,
                                       transport=transport)
        # session_id -> (fetched_at, replay payload)
        self._replay_cache = {}
        self._replay_cache_max = 4

    async def aclose(self):
        await self._http.aclose()

    # -- plumbing --------------------------------------------------------

    async def _request(self, method, path, *, params=None, json_body=None,
                       timeout=None):
        try:
            resp = await self._http.request(method, path, params=params,
                                            json=json_body, timeout=timeout)
        except httpx.ConnectError:
            raise PerfLensError(
                f'No PerfLens server is reachable at {self.base_url}. Start '
                f'one with `perflens serve`, or point this MCP server at a '
                f'different address with --server-url.') from None
        except httpx.TimeoutException:
            raise PerfLensError(
                f'The PerfLens server at {self.base_url} did not respond in '
                f'time for {method} {path}.') from None

        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            code, message = _envelope_message(payload, resp.status_code)
            hint = _HINTS.get(code)
            raise PerfLensError(f'{message}. {hint}' if hint else message)

        if not resp.content:
            return {}
        return resp.json()

    async def _get(self, path, **params):
        clean = {k: v for k, v in params.items() if v is not None}
        return await self._request('GET', path, params=clean)

    # -- status / sessions ------------------------------------------------

    async def status(self):
        return await self._get('/api/status')

    async def index_status(self):
        return await self._get('/api/index/status')

    async def sessions(self, offset=0, limit=100):
        return await self._get('/api/sessions', offset=offset, limit=limit)

    async def session_replay(self, session_id):
        """Replay a saved session, cached — replays are large and a single
        investigation queries the same session from several tools."""
        hit = self._replay_cache.get(session_id)
        if hit:
            return hit[1]
        try:
            payload = await self._get(f'/api/sessions/{session_id}')
        except PerfLensError as exc:
            if 'not found' in str(exc).lower():
                raise PerfLensError(
                    f'No saved session with id {session_id!r}. Call '
                    f'perflens_list_sessions to see the valid ids.') from None
            raise
        if len(self._replay_cache) >= self._replay_cache_max:
            oldest = min(self._replay_cache, key=lambda k: self._replay_cache[k][0])
            self._replay_cache.pop(oldest, None)
        self._replay_cache[session_id] = (time.time(), payload)
        return payload

    async def snapshot(self, event=None):
        return await self._get('/api/snapshot', event=event)

    # -- unified live/session access --------------------------------------

    async def per_event(self, source):
        """Return (per_event dict, meta dict) for 'live' or a session id.

        Both shapes converge here so every analysis tool can take one
        `source` argument instead of splitting live and saved code paths.
        """
        if source == LIVE:
            payload = await self.snapshot()
            per_event = payload.get('per_event') or {}
            version = payload.get('version') or {}
            meta = {'source': LIVE,
                    'total_samples': version.get('total_samples', 0),
                    'chunk_count': version.get('chunk_count', 0)}
            if not per_event:
                raise PerfLensError(
                    'No live profiling data yet. Check perflens_status; if an '
                    'agent is connected, start collection with '
                    'perflens_start_profiling(pid), then allow a few seconds '
                    'for the first samples to arrive.')
            return per_event, meta

        payload = await self.session_replay(source)
        per_event = payload.get('per_event') or {}
        metadata = payload.get('metadata') or {}
        meta = {'source': source,
                'total_samples': metadata.get('total_samples', 0),
                'timestamp': metadata.get('timestamp', ''),
                'agent': metadata.get('agent', ''),
                'perf_stat': metadata.get('perf_stat') or {},
                'platform': metadata.get('platform') or {}}
        if not per_event:
            raise PerfLensError(f'Session {source!r} contains no samples.')
        return per_event, meta

    async def event_entry(self, source, event=None):
        """Return (event name, per-event entry, meta) picking a sensible
        default event when the caller did not name one."""
        per_event, meta = await self.per_event(source)
        chosen = pick_event(per_event, event)
        return chosen, per_event[chosen], meta

    # -- live-only views ---------------------------------------------------

    async def threads(self, event):
        return await self._get('/api/threads', event=event)

    async def thread_view(self, tid, event):
        return await self._get(f'/api/threads/{tid}', event=event)

    async def source(self, file, event=None, tid=None):
        return await self._get('/api/source', file=file, event=event, tid=tid)

    async def stream_head(self, wait=3.0):
        """Read the opening frames of the SSE stream.

        Live hardware counters and the live event-type list have no REST
        endpoint, but the stream emits `data_version` and `perf_stat`
        immediately on connect when data exists. So open it, take those two
        frames, and hang up — far cheaper than pulling a whole snapshot
        just to learn which events exist.

        The whole read is bounded: the stream never ends on its own, and a
        tool that blocks forever is worse than one that reports nothing.
        Returns {} when the frames do not arrive in time.
        """
        try:
            return await asyncio.wait_for(self._stream_head(wait), wait + 2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return {}

    async def _stream_head(self, wait):
        head = {}
        try:
            async with self._http.stream('GET', '/api/stream',
                                         timeout=wait + 2) as resp:
                if resp.status_code >= 400:
                    return head
                event_name = None
                deadline = time.monotonic() + wait
                async for line in resp.aiter_lines():
                    if time.monotonic() > deadline:
                        break
                    line = line.strip()
                    if line.startswith('event:'):
                        event_name = line[6:].strip()
                    elif line.startswith('data:') and event_name:
                        try:
                            head[event_name] = json.loads(line[5:].strip() or '{}')
                        except json.JSONDecodeError:
                            pass
                        # Both frames arrive back to back; stop once we
                        # have them rather than holding the stream open.
                        if 'data_version' in head and 'perf_stat' in head:
                            break
        except httpx.ConnectError:
            raise PerfLensError(
                f'No PerfLens server is reachable at {self.base_url}. Start '
                f'one with `perflens serve`.') from None
        except httpx.TimeoutException:
            pass
        return head

    # -- metrics -----------------------------------------------------------

    async def metrics_current(self):
        return await self._get('/api/metrics/current')

    async def metrics_history(self, kind='system', start=None, end=None):
        return await self._get('/api/metrics/history', type=kind,
                               start=start, end=end)

    # -- agent -------------------------------------------------------------

    async def agent_info(self):
        return await self._get('/api/agent')

    async def agent_connect(self, host, port=9999):
        return await self._request('POST', '/api/agent/connect',
                                   json_body={'host': host, 'port': port},
                                   timeout=90)

    async def agent_command(self, cmd, args=None, timeout=60):
        """Relay a command to the connected agent.

        The HTTP timeout is given headroom over the agent-side budget so a
        slow probe surfaces as the agent's own error rather than as a
        client timeout.
        """
        body = {'cmd': cmd, 'args': args or {}, 'timeout': timeout}
        resp = await self._request('POST', '/api/agent/command',
                                   json_body=body, timeout=timeout + 30)
        if not resp.get('ok', True):
            raise PerfLensError(resp.get('error') or f'agent rejected {cmd}')
        return resp

    # -- export ------------------------------------------------------------

    async def export(self, source, fmt, event=None):
        """Fetch an export as raw bytes (collapsed stacks, JSON, or SVG)."""
        path = ('/api/live/export' if source == LIVE
                else f'/api/sessions/{source}/export')
        params = {'format': fmt}
        if event:
            params['event'] = event
        try:
            resp = await self._http.get(path, params=params, timeout=120)
        except httpx.ConnectError:
            raise PerfLensError(
                f'No PerfLens server is reachable at {self.base_url}.') from None
        if resp.status_code >= 400:
            try:
                _code, message = _envelope_message(resp.json(), resp.status_code)
            except ValueError:
                message = f'HTTP {resp.status_code}'
            raise PerfLensError(message or f'export failed ({resp.status_code})')
        return resp.content


def pick_event(per_event, event=None):
    """Choose which event to analyse.

    `cycles` is the default because it is the one event that answers "where
    does the time go"; anything else has to be asked for deliberately.
    """
    if not per_event:
        raise PerfLensError('No events have data.')
    available = sorted(per_event)
    if event:
        if event in per_event:
            return event
        raise PerfLensError(
            f'No data for event {event!r}. Events with data: '
            f'{", ".join(available)}.')
    if 'cycles' in per_event:
        return 'cycles'
    return available[0]
