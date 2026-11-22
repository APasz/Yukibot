import type { Probe, Target } from './types';

export interface ProbeTiming {
  target_id: string;
  offset_ms: number;
  hidden: boolean;
  browser_delay_ms: number;
}

export async function measureProbe(
  target: Target,
  timing: ProbeTiming,
  timeoutMs: number,
  signal: AbortSignal,
): Promise<Probe | null> {
  const timeout = new AbortController();
  const handle = setTimeout(() => timeout.abort(), timeoutMs);
  const startedAt = performance.now();
  try {
    const url = new URL(target.url, window.location.origin);
    url.searchParams.set('_diagnostic', `${Date.now()}-${timing.offset_ms}`);
    const response = await fetch(url, {
      cache: 'no-store',
      credentials: 'omit',
      mode: 'cors',
      redirect: 'error',
      signal: AbortSignal.any([signal, timeout.signal]),
    });
    await response.arrayBuffer();
    if (signal.aborted) return null;
    const elapsed = performance.now() - startedAt;
    if (elapsed > timeoutMs) return { ...timing, status: 'browser_delayed' };
    if (!response.ok) return { ...timing, status: 'http_error', http_status: response.status };
    return { ...timing, status: 'ok', rtt_ms: elapsed };
  } catch {
    if (signal.aborted) return null;
    const elapsed = performance.now() - startedAt;
    if (elapsed > timeoutMs + 1000) return { ...timing, status: 'browser_delayed' };
    return {
      ...timing,
      status: timeout.signal.aborted ? 'timeout' : 'network_error',
      http_status: null,
    };
  } finally {
    clearTimeout(handle);
  }
}
