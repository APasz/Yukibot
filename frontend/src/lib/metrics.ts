import type { BandwidthPhase, BandwidthTest, Probe } from './types';

export interface ProbeStatistics {
  successful: number;
  failed: number;
  delayed: number;
  median: number | null;
  p95: number | null;
  maximum: number | null;
  variation: number | null;
  longestFailureMs: number | null;
}

export function percentile(values: readonly number[], fraction: number): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.max(0, Math.ceil(sorted.length * fraction) - 1)] ?? null;
}

function median(values: readonly number[]): number | null {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  const upper = sorted[middle];
  const lower = sorted[values.length % 2 === 0 ? middle - 1 : middle];
  return upper === undefined || lower === undefined ? null : (lower + upper) / 2;
}

export function statistics(probes: readonly Probe[]): ProbeStatistics {
  const values: number[] = [];
  const changes: number[] = [];
  let previous: number | null = null;
  let failed = 0;
  let delayed = 0;
  let failureStart: number | null = null;
  let longestFailureMs: number | null = null;
  for (const probe of probes) {
    if (probe.status === 'browser_delayed') {
      delayed += 1;
      previous = null;
      failureStart = null;
    } else if (probe.status === 'ok') {
      values.push(probe.rtt_ms);
      if (previous !== null) changes.push(Math.abs(probe.rtt_ms - previous));
      previous = probe.rtt_ms;
      if (failureStart !== null) {
        longestFailureMs = Math.max(longestFailureMs ?? 0, probe.offset_ms - failureStart);
        failureStart = null;
      }
    } else {
      failed += 1;
      previous = null;
      failureStart ??= probe.offset_ms;
      longestFailureMs = Math.max(longestFailureMs ?? 0, probe.offset_ms - failureStart);
    }
  }
  return {
    successful: values.length,
    failed,
    delayed,
    median: median(values),
    p95: percentile(values, 0.95),
    maximum: values.length ? Math.max(...values) : null,
    variation: changes.length
      ? changes.reduce((total, value) => total + value, 0) / changes.length
      : null,
    longestFailureMs,
  };
}

export function milliseconds(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${Math.round(value)} ms`;
}

interface BandwidthPhaseStatistics {
  phase: BandwidthPhase;
  mbps: number | null;
  statistics: ProbeStatistics;
}

export function bandwidthStatistics(test: BandwidthTest): BandwidthPhaseStatistics[] {
  return test.phases.map((phase) => ({
    phase,
    mbps:
      phase.phase !== 'idle' && phase.transferred_bytes > 0 && phase.end_ms > phase.offset_ms
        ? (phase.transferred_bytes * 8) / (phase.end_ms - phase.offset_ms) / 1000
        : null,
    statistics: statistics(
      test.probes.filter(
        (probe) =>
          !probe.hidden &&
          probe.offset_ms >= phase.offset_ms &&
          probe.offset_ms + (probe.status === 'ok' ? probe.rtt_ms : 0) < phase.end_ms,
      ),
    ),
  }));
}

export function dataSize(bytes: number): string {
  const gibibytes = bytes >= 1024 ** 3;
  return `${Number((bytes / 1024 ** (gibibytes ? 3 : 2)).toFixed(1))} ${gibibytes ? 'GiB' : 'MiB'}`;
}

export function bandwidthSpeedGuide(bytes: number, durationMs: number): number {
  // Leave a quarter of the data allowance unused at the suggested speed.
  const payloadMbps = (bytes * 8) / durationMs / 1000;
  return Math.floor((payloadMbps * 0.75) / 10) * 10;
}

export function duration(value: number): string {
  const seconds = Math.floor(value / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

export function probeLabel(probe: Probe): string {
  switch (probe.status) {
    case 'ok':
      return milliseconds(probe.rtt_ms);
    case 'timeout':
      return 'Request timed out';
    case 'http_error':
      return `HTTP ${probe.http_status}`;
    case 'network_error':
      return 'Request failed (network, TLS or CORS)';
    case 'browser_delayed':
      return 'Browser scheduling delay';
  }
}
