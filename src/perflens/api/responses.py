"""orjson response helpers with per-route gzip negotiation.

Deliberately NOT Starlette GZipMiddleware — that would also wrap the SSE
stream and buffer events. Only explicitly opted-in big payloads compress.

No `Access-Control-Allow-Origin` header: the UI is same-origin (the Vite
dev server proxies /api), and a wildcard let any page the operator visited
read /api/browse, /api/agent and saved profiles from 127.0.0.1:8080.
"""

import gzip
import struct
import zlib

import orjson
from fastapi.responses import Response

GZIP_MIN_BYTES = 8192
GZIP_LEVEL = 1

# gzip member header: magic, deflate, no flags, no mtime, no extra flags,
# unknown OS. The trailer is crc32 + uncompressed size, little-endian.
_GZIP_HEADER = b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff'


def dumps(data):
    # OPT_NON_STR_KEYS matches json.dumps behavior (int dict keys become
    # strings) — line-number keyed maps rely on it.
    return orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS)


def deflate_segment(data):
    """Raw-deflate `data` as a self-contained, byte-aligned run of
    non-final blocks (Z_FULL_FLUSH resets the dictionary, so segments from
    independent compressors concatenate into one valid stream). What the
    aggregation worker caches per event."""
    c = zlib.compressobj(GZIP_LEVEL, zlib.DEFLATED, -zlib.MAX_WBITS)
    return c.compress(data) + c.flush(zlib.Z_FULL_FLUSH)


def gzip_join(parts):
    """One gzip body from (raw bytes, deflate segment) pairs.

    A cached multi-megabyte segment is spliced in as-is; only the small
    parts around it (the envelope, the version stamp) are compressed per
    request. The result is a single gzip member -- browsers do not agree
    on concatenated members -- with the crc and size computed over the
    concatenated raw bytes.
    """
    crc = 0
    size = 0
    out = [_GZIP_HEADER]
    for raw, segment in parts:
        crc = zlib.crc32(raw, crc)
        size += len(raw)
        out.append(segment)
    # The final (empty) block that terminates the deflate stream
    out.append(zlib.compressobj(GZIP_LEVEL, zlib.DEFLATED,
                                -zlib.MAX_WBITS).flush(zlib.Z_FINISH))
    out.append(struct.pack('<LL', crc & 0xffffffff, size & 0xffffffff))
    return b''.join(out)


def _wants_gzip(request):
    return (request is not None and
            'gzip' in request.headers.get('accept-encoding', ''))


def error_response(code, message, status):
    """The v2 error envelope: every error is
    {"error": {"code": "<slug>", "message": "..."}} with a real status."""
    return json_response({'error': {'code': code, 'message': message}},
                         status)


def json_response(data, status=200, request=None, allow_gzip=False):
    return bytes_response(dumps(data), status, request, allow_gzip)


def bytes_response(body, status=200, request=None, allow_gzip=False):
    """A JSON response from already-serialized bytes."""
    headers = {}
    # Compress big payloads when the client accepts it — the per-event
    # snapshot can be multi-MB on large profiles, gzips ~10x
    if allow_gzip and len(body) > GZIP_MIN_BYTES and _wants_gzip(request):
        body = gzip.compress(body, compresslevel=GZIP_LEVEL)
        headers['Content-Encoding'] = 'gzip'
    return Response(content=body, status_code=status,
                    media_type='application/json', headers=headers)


def spliced_response(parts, request=None):
    """A JSON response assembled from (raw bytes, deflate segment) parts:
    gzip by splicing when the client accepts it, the raw bytes otherwise."""
    raw_len = sum(len(raw) for raw, _ in parts)
    if raw_len > GZIP_MIN_BYTES and _wants_gzip(request):
        return Response(content=gzip_join(parts), media_type='application/json',
                        headers={'Content-Encoding': 'gzip'})
    return Response(content=b''.join(raw for raw, _ in parts),
                    media_type='application/json')
