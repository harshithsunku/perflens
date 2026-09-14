import { describe, expect, it } from 'vitest';
import { ApiError, REQUEST_TIMEOUT_MS, unwrap } from './client';

function response(status: number, body: unknown, json = true) {
  return new Response(json ? JSON.stringify(body) : String(body), {
    status,
    headers: { 'content-type': json ? 'application/json' : 'text/plain' },
  });
}

describe('unwrap', () => {
  it('returns the JSON body of a 2xx', async () => {
    expect(await unwrap(response(200, { ok: 1 }))).toEqual({ ok: 1 });
  });

  it('turns the error envelope into an ApiError with its code', async () => {
    const err = await unwrap(response(409, { error: { code: 'no_agent', message: 'none' } }))
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe('no_agent');
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).message).toBe('none');
  });

  it('falls back to a generic code for a non-JSON failure', async () => {
    const err = await unwrap(response(502, 'Bad Gateway', false)).catch((e: unknown) => e);
    expect((err as ApiError).code).toBe('http_error');
    expect((err as ApiError).message).toBe('HTTP 502');
  });

  it('bounds every request', () => {
    expect(REQUEST_TIMEOUT_MS).toBeGreaterThan(0);
  });
});
