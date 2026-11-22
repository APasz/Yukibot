import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { measureProbe, Recorder } from './recorder';
import type { Context, Limits, Recording, Target } from './types';

const target: Target = {
  id: 'erin',
  label: 'Erin',
  url: 'https://example.invalid/ping',
  kind: 'node',
};
const timing = { target_id: 'erin', offset_ms: 0, hidden: false, browser_delay_ms: 0 };
const limits: Limits = {
  interval_ms: 2000,
  timeout_ms: 5000,
  context_interval_ms: 10000,
  max_duration_ms: 60000,
  max_targets: 16,
  retention_days: 7,
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('window', { location: { origin: 'https://portal.invalid' } });
  vi.stubGlobal('document', { hidden: false });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('probe transport', () => {
  it('measures successful requests without sending credentials to a node', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetch);
    const result = await measureProbe(target, timing, 5000, new AbortController().signal);
    expect(result?.status).toBe('ok');
    expect(fetch.mock.calls[0]?.[1]).toMatchObject({
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
    });
  });

  it('reports HTTP errors separately from request failures', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 503 })));
    expect(await measureProbe(target, timing, 5000, new AbortController().signal)).toMatchObject({
      status: 'http_error',
      http_status: 503,
    });
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Network unavailable')));
    expect(await measureProbe(target, timing, 5000, new AbortController().signal)).toMatchObject({
      status: 'network_error',
    });
  });

  it('does not count stopping a recording as a network failure', async () => {
    const abort = new AbortController();
    abort.abort();
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new DOMException('Aborted', 'AbortError')));
    expect(await measureProbe(target, timing, 5000, abort.signal)).toBeNull();
  });

  it('distinguishes request timeouts from browser scheduling stalls', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_url: URL, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () =>
              reject(new DOMException('Aborted', 'AbortError')),
            );
          }),
      ),
    );
    const timed = measureProbe(target, timing, 5000, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(5000);
    expect(await timed).toMatchObject({ status: 'timeout' });

    let finish: ((value: Response) => void) | undefined;
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            finish = resolve;
          }),
      ),
    );
    const stalled = measureProbe(target, timing, 5000, new AbortController().signal);
    await vi.advanceTimersByTimeAsync(15000);
    finish?.(new Response(null, { status: 204 }));
    expect(await stalled).toMatchObject({ status: 'browser_delayed' });
  });
});

it('retains observations through outages, records markers, and stops without leaking timers', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Offline')));
  const snapshots: Recording[] = [];
  const stopped = vi.fn();
  const context: Context = { observed_at_ms: 0, nodes: [] };
  const recorder = new Recorder(
    [target],
    'UL',
    limits,
    (recording) => snapshots.push(recording),
    async () => context,
    stopped,
  );
  await vi.advanceTimersByTimeAsync(6000);
  recorder.mark('Container delayed');
  await recorder.stop();
  const final = snapshots.at(-1);
  expect(final?.probes.length).toBeGreaterThanOrEqual(3);
  expect(final?.probes.every((probe) => probe.status === 'network_error')).toBe(true);
  expect(final?.markers[0]?.label).toBe('Container delayed');
  expect(final?.duration_ms).toBe(6000);
  expect(stopped).toHaveBeenCalledOnce();
  expect(vi.getTimerCount()).toBe(0);
});

it('places context at response time and freezes the duration when stopped with a request pending', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Offline')));
  let finishContext: ((context: Context) => void) | undefined;
  const recorder = new Recorder(
    [target],
    'UL',
    limits,
    () => {},
    () =>
      new Promise<Context>((resolve) => {
        finishContext = resolve;
      }),
    () => {},
  );
  await vi.advanceTimersByTimeAsync(3000);
  finishContext?.({ observed_at_ms: Date.now(), nodes: [] });
  await vi.advanceTimersByTimeAsync(10000);
  expect(recorder.recording.contexts[0]?.offset_ms).toBe(3000);
  const stopped = recorder.stop();
  await vi.advanceTimersByTimeAsync(2000);
  finishContext?.({ observed_at_ms: Date.now(), nodes: [] });
  await stopped;
  expect(recorder.recording.duration_ms).toBe(13000);
  expect(recorder.recording.contexts).toHaveLength(1);
  expect(vi.getTimerCount()).toBe(0);
});
