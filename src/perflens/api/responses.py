"""orjson response helpers with per-route gzip negotiation.

Deliberately NOT Starlette GZipMiddleware — that would also wrap the SSE
stream and buffer events. Only explicitly opted-in big payloads compress.
"""

import gzip

import orjson
from fastapi.responses import Response


def dumps(data):
    # OPT_NON_STR_KEYS matches json.dumps behavior (int dict keys become
    # strings) — line-number keyed maps rely on it.
    return orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS)


def error_response(code, message, status):
    """The v2 error envelope: every error is
    {"error": {"code": "<slug>", "message": "..."}} with a real status."""
    return json_response({'error': {'code': code, 'message': message}},
                         status)


def json_response(data, status=200, request=None, allow_gzip=False):
    body = dumps(data)
    headers = {'Access-Control-Allow-Origin': '*'}
    # Compress big payloads when the client accepts it — the per-event
    # snapshot can be multi-MB on large profiles, gzips ~10x
    if (allow_gzip and request is not None and len(body) > 8192 and
            'gzip' in request.headers.get('accept-encoding', '')):
        body = gzip.compress(body, compresslevel=1)
        headers['Content-Encoding'] = 'gzip'
    return Response(content=body, status_code=status,
                    media_type='application/json', headers=headers)
