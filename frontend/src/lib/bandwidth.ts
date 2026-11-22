import { pause } from './async';
import { measureProbe } from './probes';
import type { BandwidthEndpoint, BandwidthPhase, BandwidthTest, Probe, Target } from './types';

export interface BandwidthProgress {
  phase: BandwidthPhase['phase'] | 'connecting';
  transferred_bytes: number;
}

interface TestOptions {
  target: Target;
  endpoint: BandwidthEndpoint;
  signal: AbortSignal;
  now?: () => number;
  timeoutMs: number;
  onProgress: (progress: BandwidthProgress) => void;
}

export async function runBandwidthTest({
  target,
  endpoint,
  signal,
  now: clock = () => performance.now(),
  timeoutMs,
  onProgress,
}: TestOptions): Promise<BandwidthTest> {
  if (target.id !== endpoint.target_id)
    throw new Error('Bandwidth target does not match the node.');
  const base = new URL(`${endpoint.url.replace(/\/$/, '')}/`, window.location.origin);
  const startedAt = Date.now();
  const clockStart = clock();
  const now = () => Math.max(0, Math.round(clock() - clockStart));
  const { limits, token } = endpoint.grant;
  const visibility = new AbortController();
  const onVisibility = () => {
    if (document.hidden) visibility.abort();
  };
  document.addEventListener('visibilitychange', onVisibility);
  onVisibility();
  const external = AbortSignal.any([signal, visibility.signal]);
  const headers = { Authorization: `Bearer ${token}` };
  const phases: BandwidthPhase[] = [];
  const probes: Probe[] = [];
  const sampling = new AbortController();
  const samplingSignal = AbortSignal.any([external, sampling.signal]);
  const samples = (async () => {
    while (!samplingSignal.aborted && probes.length < 100) {
      const probe = await measureProbe(
        target,
        {
          target_id: target.id,
          offset_ms: now(),
          hidden: false,
          browser_delay_ms: 0,
        },
        timeoutMs,
        samplingSignal,
      );
      if (probe) probes.push(probe);
      await pause(limits.probe_interval_ms, samplingSignal);
    }
  })();
  let failure: string | null = null;
  let payload: Uint8Array<ArrayBuffer> | undefined;

  async function transfer(phase: 'download' | 'upload'): Promise<BandwidthPhase> {
    const offset = now();
    const deadline = new AbortController();
    const failed = new AbortController();
    const combined = AbortSignal.any([external, deadline.signal, failed.signal]);
    const timer = setTimeout(() => deadline.abort(), limits.transfer_duration_ms);
    let reserved = 0;
    let transferred = 0;
    let lastProgress = -Infinity;
    onProgress({ phase, transferred_bytes: 0 });

    function progress(count: number): void {
      transferred += count;
      if (performance.now() - lastProgress >= 100) {
        lastProgress = performance.now();
        onProgress({ phase, transferred_bytes: transferred });
      }
    }

    async function download(size: number): Promise<void> {
      const url = new URL('download', base);
      url.searchParams.set('size', String(size));
      url.searchParams.set('_diagnostic', crypto.randomUUID());
      const response = await fetch(url, {
        headers,
        signal: combined,
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
        mode: 'cors',
      });
      if (!response.ok) throw new Error(`Download failed (HTTP ${response.status}).`);
      const encoding = response.headers.get('Content-Encoding');
      if (encoding !== null && encoding !== 'identity')
        throw new Error('The test response was compressed.');
      if (!response.body) throw new Error('Download stream unavailable.');
      const expected = response.headers.has('Content-Length')
        ? Number(response.headers.get('Content-Length'))
        : size;
      if (!Number.isSafeInteger(expected) || expected < 1 || expected > size)
        throw new Error('Invalid download size.');
      const reader = response.body.getReader();
      let received = 0;
      try {
        while (!combined.aborted) {
          const chunk = await reader.read();
          if (chunk.done) break;
          received += chunk.value.byteLength;
          if (received > expected) throw new Error('Download exceeded its declared size.');
          progress(chunk.value.byteLength);
        }
        if (!combined.aborted && received !== expected) throw new Error('Download ended early.');
      } finally {
        await reader.cancel().catch(() => {});
        reader.releaseLock();
      }
    }

    async function upload(size: number): Promise<void> {
      if (!payload) {
        const bytes = new Uint8Array(limits.max_request_bytes);
        for (let offset = 0; offset < bytes.length; offset += 65536) {
          crypto.getRandomValues(bytes.subarray(offset, offset + 65536));
        }
        payload = bytes;
      }
      const response = await fetch(new URL('upload', base), {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/octet-stream' },
        body: payload.subarray(0, size),
        signal: combined,
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
        mode: 'cors',
      });
      if (!response.ok) throw new Error(`Upload failed (HTTP ${response.status}).`);
      const receipt: unknown = await response.json();
      if (
        !receipt ||
        typeof receipt !== 'object' ||
        !('received_bytes' in receipt) ||
        receipt.received_bytes !== size
      ) {
        throw new Error('Upload was not acknowledged by the node.');
      }
      if (!combined.aborted) progress(size);
    }

    async function worker(): Promise<void> {
      let size = Math.min(128 * 1024, limits.max_request_bytes);
      while (!combined.aborted && reserved < limits.max_bytes_per_direction) {
        const count = Math.min(size, limits.max_bytes_per_direction - reserved);
        reserved += count;
        const started = performance.now();
        try {
          if (phase === 'download') await download(count);
          else await upload(count);
        } catch (error) {
          if (!combined.aborted) {
            failure = error instanceof Error ? error.message.slice(0, 200) : 'Transfer failed.';
            failed.abort();
          }
          return;
        }
        const elapsed = Math.max(1, performance.now() - started);
        size = Math.max(
          1,
          Math.min(limits.max_request_bytes, Math.round(count * Math.min(4, 1000 / elapsed))),
        );
      }
    }

    try {
      await Promise.all(Array.from({ length: limits.parallel_requests }, worker));
    } finally {
      clearTimeout(timer);
    }
    onProgress({ phase, transferred_bytes: transferred });
    return {
      phase,
      offset_ms: offset,
      end_ms: now(),
      transferred_bytes: transferred,
      ended_by: failure
        ? 'error'
        : external.aborted
          ? 'cancelled'
          : deadline.signal.aborted
            ? 'duration'
            : 'byte_limit',
    };
  }

  try {
    const offset = now();
    onProgress({ phase: 'idle', transferred_bytes: 0 });
    await pause(limits.idle_duration_ms, external);
    phases.push({
      phase: 'idle',
      offset_ms: offset,
      end_ms: now(),
      transferred_bytes: 0,
      ended_by: external.aborted ? 'cancelled' : 'duration',
    });
    for (const phase of ['download', 'upload'] as const) {
      if (external.aborted || failure) break;
      phases.push(await transfer(phase));
    }
    sampling.abort();
    await samples;
    const state = failure ? 'failed' : external.aborted ? 'cancelled' : 'completed';
    const last = phases.at(-1);
    if (last && state !== 'completed') last.ended_by = state === 'failed' ? 'error' : 'cancelled';
    return {
      target,
      max_bytes_per_direction: limits.max_bytes_per_direction,
      transfer_duration_ms: limits.transfer_duration_ms,
      started_at_ms: startedAt,
      duration_ms: now(),
      probes,
      phases,
      state,
      error: failure,
    };
  } finally {
    sampling.abort();
    await samples;
    document.removeEventListener('visibilitychange', onVisibility);
    await fetch(new URL('finish', base), {
      method: 'POST',
      headers,
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
      mode: 'cors',
      signal: AbortSignal.timeout(1500),
    }).catch(() => {});
  }
}
