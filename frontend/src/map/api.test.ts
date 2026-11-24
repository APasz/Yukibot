// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiUrl, fetchJson, LatestTask } from './api';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it('preserves the base query and encodes world names', () => {
  const url = new URL(apiUrl('/map?node=erin', '/worlds/My%20Save/settings', { zoom: 4 }));
  expect(url.pathname).toBe('/map/worlds/My%20Save/settings');
  expect(url.searchParams.get('node')).toBe('erin');
  expect(url.searchParams.get('zoom')).toBe('4');
});

describe('bounded JSON requests', () => {
  it('times out a stalled response body and releases its timer', async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, options: RequestInit) =>
        Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
          json: () =>
            new Promise((_resolve, reject) => {
              options.signal!.addEventListener('abort', () => reject(options.signal!.reason), {
                once: true,
              });
            }),
        }),
      ),
    );
    const request = fetchJson('/stalled');
    const rejection = expect(request).rejects.toThrow('timed out');
    await vi.advanceTimersByTimeAsync(15000);
    await rejection;
    expect(vi.getTimerCount()).toBe(0);
  });

  it('retains caller cancellation and handles 304 without reading a body', async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_url: string, options: RequestInit) =>
          new Promise((_resolve, reject) => {
            options.signal!.addEventListener('abort', () => reject(options.signal!.reason));
          }),
      ),
    );
    const request = fetchJson('/cancel', { signal: controller.signal });
    controller.abort(new Error('Page left'));
    await expect(request).rejects.toThrow('Page left');
    expect(vi.getTimerCount()).toBe(0);
    const json = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 304, json }));
    expect(await fetchJson('/unchanged')).toBeNull();
    expect(json).not.toHaveBeenCalled();
  });

  it('propagates API errors and cache freshness', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ detail: 'Rendering disabled' }), { status: 404 }),
        ),
    );
    await expect(fetchJson('/bad')).rejects.toThrow('Rendering disabled');
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response('{}', {
          headers: {
            'X-Yukibot-Map-Source': 'stale',
            'X-Yukibot-Map-Cache-Updated-At': '1234',
            ETag: '"one"',
          },
        }),
      ),
    );
    expect(await fetchJson('/cached')).toMatchObject({
      source: 'stale',
      updatedAt: 1234,
      etag: '"one"',
    });
  });
});

describe('latest task', () => {
  it('does not lose a queued operation while the previous promise is settling', async () => {
    const task = new LatestTask();
    const first = task.run(async () => {});
    await Promise.resolve();
    await Promise.resolve();
    const next = vi.fn(async () => {});
    await Promise.all([first, task.run(next, true)]);
    expect(next).toHaveBeenCalledOnce();
  });

  it('coalesces polls and runs only the latest queued selection after a failed request', async () => {
    const task = new LatestTask();
    const calls: string[] = [];
    let fail!: (error: Error) => void;
    const first = task.run(
      () =>
        new Promise((_resolve, reject) => {
          calls.push('poll');
          fail = reject;
        }),
    );
    await Promise.resolve();
    const dropped = task.run(async () => {
      calls.push('extra poll');
    });
    const old = task.run(async () => {
      calls.push('old world');
    }, true);
    const latest = task.run(async () => {
      calls.push('latest world');
    }, true);
    fail(new Error('Old poll failed'));
    await Promise.all([first, dropped, old, latest]);
    expect(calls).toEqual(['poll', 'latest world']);
    await expect(
      task.run(async () => {
        throw new Error('New failure');
      }),
    ).rejects.toThrow('New failure');
    await task.run(async () => {
      calls.push('recovered');
    });
    expect(calls.at(-1)).toBe('recovered');
  });

  it('drops queued work after cancellation', async () => {
    const task = new LatestTask();
    let release!: () => void;
    const pending = task.run(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );
    await Promise.resolve();
    const queued = vi.fn(async () => {});
    void task.run(queued, true);
    task.cancel();
    release();
    await pending;
    await task.run(queued);
    expect(queued).not.toHaveBeenCalled();
  });
});
