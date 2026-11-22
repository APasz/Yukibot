import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { runBandwidthTest } from './bandwidth';
import { bandwidthStatistics } from './metrics';
import { Recorder } from './recorder';
import type { BandwidthEndpoint, Limits, Target } from './types';

const target: Target = {
  id: 'erin',
  label: 'Erin',
  url: 'https://erin.invalid/ping',
  kind: 'node',
};
const endpoint: BandwidthEndpoint = {
  target_id: target.id,
  url: 'https://erin.invalid/bandwidth',
  grant: {
    token: 'transfer-only',
    limits: {
      idle_duration_ms: 100,
      transfer_duration_ms: 120,
      probe_interval_ms: 20,
      max_bytes_per_direction: 64,
      max_request_bytes: 16,
      parallel_requests: 2,
      max_tests_per_recording: 5,
    },
  },
};
const limits: Limits = {
  interval_ms: 2000,
  timeout_ms: 5000,
  context_interval_ms: 10000,
  max_duration_ms: 60000,
  max_targets: 16,
  retention_days: 7,
};
let documentEvents: EventTarget & { hidden: boolean };

beforeEach(() => {
  vi.useFakeTimers();
  documentEvents = Object.assign(new EventTarget(), { hidden: false });
  vi.stubGlobal('document', documentEvents);
  vi.stubGlobal('window', { location: { origin: 'https://portal.invalid' } });
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function transport(
  options: {
    badReceipt?: boolean;
    downloadStatus?: number;
    slowUpload?: boolean;
    stalledTransfers?: boolean;
  } = {},
) {
  const fetch = vi.fn((input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = new URL(String(input));
    const upload = url.pathname.endsWith('/upload');
    const download = url.pathname.endsWith('/download');
    return new Promise((resolve, reject) => {
      const abort = () => {
        clearTimeout(timer);
        init.signal?.removeEventListener('abort', abort);
        reject(new DOMException('Aborted', 'AbortError'));
      };
      const timer = setTimeout(
        () => {
          init.signal?.removeEventListener('abort', abort);
          if (download) {
            resolve(
              options.downloadStatus
                ? new Response(null, { status: options.downloadStatus })
                : new Response(new Uint8Array(Number(url.searchParams.get('size'))), {
                    headers: { 'Content-Encoding': 'identity' },
                  }),
            );
          } else if (upload) {
            const size = init.body instanceof Uint8Array ? init.body.byteLength : 0;
            resolve(Response.json({ received_bytes: options.badReceipt ? size - 1 : size }));
          } else resolve(new Response(null, { status: 204 }));
        },
        download || upload
          ? options.stalledTransfers
            ? 60000
            : options.slowUpload && upload
              ? 1000
              : 25
          : 5,
      );
      init.signal?.addEventListener('abort', abort, { once: true });
      if (init.signal?.aborted) abort();
    });
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

function run(signal = new AbortController().signal) {
  return runBandwidthTest({ target, endpoint, signal, timeoutMs: 1000, onProgress: () => {} });
}

it('rejects an invalid destination before starting sampling or visibility tracking', async () => {
  const fetch = transport();
  const added = vi.spyOn(documentEvents, 'addEventListener');
  await expect(
    runBandwidthTest({
      target,
      endpoint: { ...endpoint, url: 'http://[' },
      signal: new AbortController().signal,
      timeoutMs: 1000,
      onProgress: () => {},
    }),
  ).rejects.toThrow();
  expect(fetch).not.toHaveBeenCalled();
  expect(added).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});

it('skips upload allocation when cancelled and cleans up if payload creation fails', async () => {
  const fetch = transport();
  const random = vi.spyOn(crypto, 'getRandomValues').mockImplementation(() => {
    throw new Error('Random payload unavailable');
  });
  const removed = vi.spyOn(documentEvents, 'removeEventListener');
  const abort = new AbortController();
  abort.abort();
  const cancelled = run(abort.signal);
  await vi.advanceTimersByTimeAsync(10);
  expect((await cancelled).state).toBe('cancelled');
  expect(random).not.toHaveBeenCalled();
  const failed = run();
  const result = expect(failed).resolves.toMatchObject({
    state: 'failed',
    error: 'Random payload unavailable',
    phases: [
      { phase: 'idle', ended_by: 'duration' },
      { phase: 'download', ended_by: 'byte_limit' },
      { phase: 'upload', ended_by: 'error' },
    ],
  });
  await vi.advanceTimersByTimeAsync(1000);
  await result;
  expect(removed).toHaveBeenCalledWith('visibilitychange', expect.any(Function));
  expect(fetch.mock.calls.filter(([url]) => String(url).endsWith('/finish'))).toHaveLength(2);
  expect(vi.getTimerCount()).toBe(0);
});

it('caps both directions, records idle and loaded latency, and uses transfer-only credentials directly', async () => {
  const fetch = transport();
  const pending = run();
  await vi.advanceTimersByTimeAsync(1000);
  const result = await pending;
  expect(result.state).toBe('completed');
  expect(result.max_bytes_per_direction).toBe(endpoint.grant.limits.max_bytes_per_direction);
  expect(result.transfer_duration_ms).toBe(endpoint.grant.limits.transfer_duration_ms);
  expect(result.phases.map((phase) => phase.transferred_bytes)).toEqual([0, 64, 64]);
  expect(result.phases.map((phase) => phase.ended_by)).toEqual([
    'duration',
    'byte_limit',
    'byte_limit',
  ]);
  expect(result.probes.length).toBeGreaterThan(3);
  expect(bandwidthStatistics(result).every((row) => row.statistics.successful > 0)).toBe(true);
  for (const [input, init] of fetch.mock.calls) {
    if (!init) throw new Error('Missing request options.');
    expect(new URL(String(input)).origin).toBe('https://erin.invalid');
    expect(init.credentials).toBe('omit');
    expect(init.redirect).toBe('error');
    if (!String(input).includes('/ping'))
      expect(init.headers).toMatchObject({ Authorization: 'Bearer transfer-only' });
    else expect(init.headers).toBeUndefined();
  }
  expect(JSON.stringify(result)).not.toContain('transfer-only');
  expect(vi.getTimerCount()).toBe(0);
});

it('keeps cancelled uploads uncounted until the server acknowledges them', async () => {
  transport({ slowUpload: true });
  const abort = new AbortController();
  const pending = run(abort.signal);
  await vi.advanceTimersByTimeAsync(170);
  abort.abort();
  await vi.advanceTimersByTimeAsync(10);
  const result = await pending;
  expect(result.state).toBe('cancelled');
  expect(result.phases.at(-1)).toMatchObject({
    phase: 'upload',
    transferred_bytes: 0,
    ended_by: 'cancelled',
  });
  expect(bandwidthStatistics(result).at(-1)?.mbps).toBeNull();
  expect(vi.getTimerCount()).toBe(0);
});

it('stops when the tab is hidden and cancels without starting transfers during idle', async () => {
  const fetch = transport();
  const pending = run();
  await vi.advanceTimersByTimeAsync(50);
  documentEvents.hidden = true;
  documentEvents.dispatchEvent(new Event('visibilitychange'));
  await vi.advanceTimersByTimeAsync(10);
  expect((await pending).phases).toHaveLength(1);
  expect(fetch.mock.calls.some(([url]) => String(url).includes('/download'))).toBe(false);
  expect(vi.getTimerCount()).toBe(0);
});

it.each([{ downloadStatus: 503 }, { badReceipt: true }])(
  'reports transfer failures as partial results: %j',
  async (options) => {
    transport(options);
    const pending = run();
    await vi.advanceTimersByTimeAsync(1000);
    const result = await pending;
    expect(result.state).toBe('failed');
    expect(result.error).toBeTruthy();
    expect(result.phases.at(-1)?.ended_by).toBe('error');
    expect(vi.getTimerCount()).toBe(0);
  },
);

it('stops uploads at the phase deadline even when none finish', async () => {
  transport({ slowUpload: true });
  const pending = run();
  await vi.advanceTimersByTimeAsync(1000);
  const result = await pending;
  expect(result.state).toBe('completed');
  expect(result.phases.at(-1)).toMatchObject({ transferred_bytes: 0, ended_by: 'duration' });
  expect(vi.getTimerCount()).toBe(0);
});

it.each([5000, 10000, 20000])(
  'honours a selected %i ms limit independently for each direction',
  async (durationMs) => {
    transport({ stalledTransfers: true });
    const configured = structuredClone(endpoint);
    configured.grant.limits.transfer_duration_ms = durationMs;
    configured.grant.limits.probe_interval_ms = 500;
    const pending = runBandwidthTest({
      target,
      endpoint: configured,
      signal: new AbortController().signal,
      timeoutMs: 1000,
      onProgress: () => {},
    });
    await vi.advanceTimersByTimeAsync(
      configured.grant.limits.idle_duration_ms + 2 * durationMs + 10,
    );
    const result = await pending;
    expect(result.state).toBe('completed');
    expect(result.transfer_duration_ms).toBe(durationMs);
    expect(result.phases.slice(1).map((phase) => phase.end_ms - phase.offset_ms)).toEqual([
      durationMs,
      durationMs,
    ]);
    expect(
      result.phases
        .slice(1)
        .every((phase) => phase.ended_by === 'duration' && phase.transferred_bytes === 0),
    ).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  },
);

it('does not run transfers during normal recording, and attaches an explicitly requested test when stopped', async () => {
  const fetch = transport();
  const recorder = new Recorder(
    [target],
    'Game',
    limits,
    () => {},
    async () => ({ observed_at_ms: Date.now(), nodes: [] }),
    () => {},
  );
  await vi.advanceTimersByTimeAsync(50);
  expect(fetch.mock.calls.every(([url]) => String(url).includes('/ping'))).toBe(true);
  const pending = recorder.measureBandwidth(
    target,
    endpoint,
    new AbortController().signal,
    () => {},
  );
  expect(() => recorder.requireBandwidthCapacity(endpoint.grant.limits)).toThrow('already running');
  await vi.advanceTimersByTimeAsync(130);
  const stopped = recorder.stop();
  await vi.advanceTimersByTimeAsync(20);
  await stopped;
  expect((await pending).state).toBe('cancelled');
  const attached = recorder.recording.bandwidth_tests?.[0];
  expect(attached?.offset_ms).toBe(50);
  expect(attached!.offset_ms + attached!.test.duration_ms).toBeLessThanOrEqual(
    recorder.recording.duration_ms,
  );
  expect(vi.getTimerCount()).toBe(0);
});
