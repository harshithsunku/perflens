"""Every route's actual response body must match its declared response_model.

Handlers return a pre-serialized orjson Response (api/responses.py), and
FastAPI skips response validation entirely when a handler does that. So all 26
routes declare a schema that nothing checks, while the declared schema is
exactly what is exported to frontend/openapi.json and turned into the
browser's TypeScript types. CI diff-checks the exported file against the
models; nothing checked the models against reality. This does.

The design is deliberate and should stay — orjson plus per-route gzip is the
whole reason — so the contract gets a test instead of a rewrite.
"""

from typing import get_args

import pytest
from fastapi.routing import APIRoute
from pydantic import ConfigDict, create_model

from conftest import FIXTURE, materialize_fixture_session
from perflens import web

# How to exercise each JSON route: path -> (query params, path params).
# Routes are keyed by their declared path so a new route with no entry fails
# test_every_json_route_is_covered rather than silently going unchecked.
ROUTE_CASES = {
    '/api/status': {},
    '/api/snapshot': {},
    '/api/sessions': {},
    '/api/sessions/{session_id}': {'path': {'session_id': ':session:'}},
    '/api/threads': {},
    '/api/window': {'query': {'start': 0, 'end': 9e18}},
    '/api/index/status': {},
    '/api/index/files': {},
    '/api/metrics/current': {},
    '/api/metrics/history': {},
    '/api/browse': {},
    '/api/wizard': {},
    '/api/agent': {},
    '/api/config': {},
}

# Legitimately not JSON: SSE and two file downloads.
NON_JSON_PATHS = {
    '/api/stream',
    '/api/sessions/{session_id}/export',
    '/api/live/export',
}

# Routes whose success path needs state this test does not set up (a live
# agent, an uploaded file). Their models are covered by test_http_api.py.
SKIP_PATHS = {
    '/api/threads/{tid}',       # needs live per-thread aggregates
    '/api/source',              # needs a source mapper
    '/api/sessions/import',     # POST with a file body
    '/api/agent/connect',       # needs a reachable agent
    '/api/agent/command',       # needs a connected agent
}


def json_get_routes():
    """All GET routes declaring a response_model.

    Enumerate web.router directly. FastAPI's lazy router inclusion means
    app.routes holds an _IncludedRouter wrapper rather than the routes
    themselves, so the obvious `[r for r in app.routes if isinstance(r,
    APIRoute)]` returns a single route and every assertion below would pass
    while checking nothing.
    """
    return [r for r in web.router.routes
            if isinstance(r, APIRoute) and 'GET' in r.methods]


def test_route_enumeration_is_not_vacuous():
    """Guard against the lazy-inclusion trap described above."""
    routes = json_get_routes()
    assert len(routes) > 15, (
        f'only found {len(routes)} routes — enumeration is probably broken, '
        f'which would make every other test here pass without checking anything')


def test_every_json_route_is_covered():
    """A new route must be added to ROUTE_CASES, NON_JSON_PATHS or SKIP_PATHS."""
    known = set(ROUTE_CASES) | NON_JSON_PATHS | SKIP_PATHS
    paths = {r.path for r in json_get_routes()}
    uncovered = paths - known - {'/{full_path:path}'}
    assert not uncovered, f'routes with no response-model coverage: {uncovered}'


def _exercise(client, route, case, session_id):
    path = route.path
    for name, value in (case.get('path') or {}).items():
        value = session_id if value == ':session:' else value
        path = path.replace('{%s}' % name, str(value))
    return client.get(path, params=case.get('query') or {})


def _model_variants(model):
    """The concrete BaseModel classes a response_model can produce.

    A route whose body legitimately has more than one shape declares a Union
    (/api/snapshot returns one event or all of them). Unwrap it so the field
    check below applies to whichever member the body actually matches, rather
    than silently skipping the union.
    """
    args = get_args(model)
    candidates = args if args else (model,)
    return [m for m in candidates
            if isinstance(m, type) and hasattr(m, 'model_fields')]


@pytest.fixture()
def session_id(core):
    return materialize_fixture_session(FIXTURE, core.config.sessions_dir)


@pytest.mark.parametrize('path', sorted(ROUTE_CASES))
def test_response_matches_declared_model(client, session_id, path):
    route = next(r for r in json_get_routes() if r.path == path)
    assert route.response_model is not None, f'{path} declares no response_model'

    resp = _exercise(client, route, ROUTE_CASES[path], session_id)
    assert resp.status_code == 200, f'{path} -> {resp.status_code}: {resp.text[:300]}'

    # Raises ValidationError with the offending field when the body and the
    # declared schema disagree.
    adapter = create_model(
        'Wrapper', __config__=ConfigDict(arbitrary_types_allowed=True),
        body=(route.response_model, ...))
    adapter(body=resp.json())


@pytest.mark.parametrize('path', sorted(ROUTE_CASES))
def test_response_declares_every_field_it_returns(client, session_id, path):
    """Second, stricter pass.

    Seven models set extra='allow', so plain validation accepts undeclared
    fields — which is how IndexStatus drifted to declaring 3 of the 8 keys its
    route actually returns and still validated. Compare key sets directly to
    catch fields the server emits but the schema never mentions, and which the
    generated TypeScript types therefore do not have.
    """
    route = next(r for r in json_get_routes() if r.path == path)
    variants = _model_variants(route.response_model)
    if not variants:
        pytest.skip('container type (dict/list), not a plain model')

    body = _exercise(client, route, ROUTE_CASES[path], session_id).json()
    if not isinstance(body, dict):
        pytest.skip('non-object body')

    def undeclared_for(model):
        declared = set(model.model_fields) | {
            f.alias for f in model.model_fields.values() if f.alias}
        return set(body) - declared

    # For a union, the body only has to be fully declared by one member.
    best = min(variants, key=lambda m: len(undeclared_for(m)))
    undeclared = undeclared_for(best)
    assert not undeclared, (
        f'{route.path} returns keys its response_model does not declare: '
        f'{sorted(undeclared)} (model={best.__name__}). The exported OpenAPI '
        f'schema and the generated TS types are missing these.')
