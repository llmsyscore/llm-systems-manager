// The alarm engine's frontend ships no test runner of its own, so its one
// pure helper is exercised from here: ApiClient._errorText, which turns a
// FastAPI error body into text a human can act on. Before it existed, every
// 422 in the AE UI rendered as the literal string "[object Object]".
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const AE_API = path.resolve(here, '../../../llm-systems-alarm-engine/frontend/js/api.js');

// api.js is a classic script (no exports); evaluate it and lift ApiClient out.
// The script gets its own global, so `fetch` has to be injected on the context
// it actually sees rather than on the test realm's globalThis.
function loadApiClient() {
  const src = fs.readFileSync(AE_API, 'utf8');
  const ctx = { window: {}, fetch: undefined, console };
  vm.createContext(ctx);
  vm.runInContext(src + '\n;globalThis.__ApiClient = ApiClient;', ctx);
  return { api: ctx.__ApiClient, ctx };
}

const { api: ApiClient } = loadApiClient();
const text = (body, status = 422, statusText = 'Unprocessable Entity') =>
  ApiClient._errorText(body, status, statusText);

describe('AE ApiClient._errorText', () => {
  it('never returns the [object Object] string for any shape', () => {
    const shapes = [
      { detail: [{ loc: ['body', 'name'], msg: 'field required', type: 'missing' }] },
      { detail: [{ msg: 'bad' }, { msg: 'worse' }] },
      { detail: { nested: true } },
      { detail: [{}] },
      { detail: 'plain' },
      { error: 'proxy died' },
      {}, null, undefined,
    ];
    shapes.forEach((s) => expect(text(s)).not.toContain('[object Object]'));
  });

  it('names the offending field for a 422 validation array', () => {
    expect(text({ detail: [
      { loc: ['body', 'name'], msg: 'Field required', type: 'missing' },
    ] })).toBe('name: Field required');
  });

  it('joins multiple validation errors', () => {
    expect(text({ detail: [
      { loc: ['body', 'name'], msg: 'Field required' },
      { loc: ['body', 'config', 'webpush', 'url'], msg: 'Input should be a string' },
    ] })).toBe('name: Field required; config.webpush.url: Input should be a string');
  });

  it('drops the body/query prefix but keeps the rest of the path', () => {
    expect(text({ detail: [{ loc: ['query', 'limit'], msg: 'too large' }] }))
      .toBe('limit: too large');
    expect(text({ detail: [{ loc: ['channels', 0, 'id'], msg: 'bad' }] }))
      .toBe('channels.0.id: bad');
  });

  it('passes an HTTPException string straight through', () => {
    expect(text({ detail: 'management authentication required' }, 401, 'Unauthorized'))
      .toBe('management authentication required');
  });

  it('reads the manager proxy\'s {ok:false, error} shape', () => {
    expect(text({ ok: false, error: 'alarm engine unreachable' }, 502, 'Bad Gateway'))
      .toBe('alarm engine unreachable');
  });

  it('falls back to the status line when the body carries nothing usable', () => {
    expect(text({}, 500, 'Internal Server Error'))
      .toBe('HTTP 500: Internal Server Error');
    expect(text(null, 503, 'Service Unavailable'))
      .toBe('HTTP 503: Service Unavailable');
    expect(text({ detail: '   ' }, 500, 'Internal Server Error'))
      .toBe('HTTP 500: Internal Server Error');
  });

  it('survives a validation entry with no msg and no loc', () => {
    expect(text({ detail: [{ type: 'weird' }] })).toBe('{"type":"weird"}');
  });

  it('accepts a plain array of strings', () => {
    expect(text({ detail: ['first', 'second'] })).toBe('first; second');
  });
});

describe('AE ApiClient._request error surfacing', () => {
  const { api: client, ctx } = loadApiClient();

  beforeEach(() => {
    ctx.fetch = vi.fn();
  });
  afterEach(() => {
    ctx.fetch = undefined;
    vi.restoreAllMocks();
  });

  it('throws an Error whose message is the formatted detail', async () => {
    ctx.fetch.mockResolvedValue({
      ok: false, status: 422, statusText: 'Unprocessable Entity',
      json: async () => ({ detail: [
        { loc: ['body', 'name'], msg: 'Field required', type: 'missing' }] }),
    });
    await expect(client._request('/notifications/channels', { method: 'POST' }))
      .rejects.toThrow('name: Field required');
  });

  it('does not choke when the error body is not JSON at all', async () => {
    ctx.fetch.mockResolvedValue({
      ok: false, status: 502, statusText: 'Bad Gateway',
      json: async () => { throw new SyntaxError('not json'); },
    });
    await expect(client._request('/rules')).rejects.toThrow('HTTP 502: Bad Gateway');
  });
});
