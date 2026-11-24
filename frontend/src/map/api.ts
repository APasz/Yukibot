export const MAP_REQUEST_TIMEOUT_MS = 15000;

export interface JsonResult<T> {
  data: T;
  source: 'live' | 'stale';
  updatedAt: number | null;
  etag: string | null;
}

export function apiUrl(
  base: string,
  suffix: string,
  params: Record<string, string | number> = {},
): string {
  const url = new URL(base, window.location.origin);
  url.pathname = `${url.pathname.replace(/\/$/, '')}${suffix}`;
  for (const [key, value] of Object.entries(params)) url.searchParams.set(key, String(value));
  return url.toString();
}

export async function fetchJson<T>(
  url: string,
  options: RequestInit = {},
): Promise<JsonResult<T> | null> {
  const timeout = new AbortController();
  const timer = window.setTimeout(
    () => timeout.abort(new Error('Map request timed out.')),
    MAP_REQUEST_TIMEOUT_MS,
  );
  const signal = options.signal
    ? AbortSignal.any([options.signal, timeout.signal])
    : timeout.signal;
  try {
    const headers = new Headers(options.headers);
    headers.set('Accept', 'application/json');
    const response = await fetch(url, { ...options, headers, signal });
    if (response.status === 304) return null;
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const payload: unknown = await response.json();
        if (
          payload &&
          typeof payload === 'object' &&
          'detail' in payload &&
          typeof payload.detail === 'string'
        ) {
          message = payload.detail;
        }
      } catch (error) {
        if (signal.aborted) throw signal.reason;
      }
      throw new Error(message);
    }
    const data: T = await response.json();
    signal.throwIfAborted();
    const updatedAt = Number(response.headers.get('X-Yukibot-Map-Cache-Updated-At'));
    return {
      data,
      source: response.headers.get('X-Yukibot-Map-Source') === 'stale' ? 'stale' : 'live',
      updatedAt: Number.isFinite(updatedAt) && updatedAt > 0 ? updatedAt : null,
      etag: response.headers.get('ETag'),
    };
  } finally {
    window.clearTimeout(timer);
  }
}

/** Coalesce automatic work and retain the latest explicit selection. */
export class LatestTask {
  private pending: Promise<void> | null = null;
  private queued: (() => Promise<void>) | null = null;
  private cancelled = false;

  run(operation: () => Promise<void>, queue = false): Promise<void> {
    if (this.cancelled) return Promise.resolve();
    if (this.pending) {
      if (queue) this.queued = operation;
      return this.pending;
    }
    const pending = Promise.resolve().then(async () => {
      try {
        let next: (() => Promise<void>) | null = operation;
        let failure: unknown;
        let succeeded = true;
        while (next && !this.cancelled) {
          succeeded = true;
          try {
            await next();
          } catch (error) {
            succeeded = false;
            failure = error;
          }
          next = this.queued;
          this.queued = null;
        }
        if (!succeeded && !this.cancelled) throw failure;
      } finally {
        if (this.pending === pending) this.pending = null;
      }
    });
    this.pending = pending;
    return pending;
  }

  cancel(): void {
    this.cancelled = true;
    this.queued = null;
  }
}
