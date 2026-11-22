import { describe, expect, it } from 'vitest';
import {
  bandwidthSpeedGuide,
  bandwidthStatistics,
  dataSize,
  percentile,
  statistics,
} from './metrics';
import type { Probe } from './types';

const base = { target_id: 'erin', hidden: false, browser_delay_ms: 0 };

describe('diagnostic statistics', () => {
  it.each([
    [32, 40],
    [64, 80],
    [128, 160],
    [256, 320],
    [512, 640],
    [1024, 1280],
  ])('guides a %i MiB cap for a five-second phase', (size, rate) => {
    const bytes = size * 1024 ** 2;
    expect(bandwidthSpeedGuide(bytes, 5000)).toBe(rate);
    expect(((rate * 1e6) / 8) * 5).toBeLessThan(bytes);
  });

  it.each([
    [5000, 40],
    [10000, 20],
    [20000, 10],
  ])('updates the suggested link speed for a %i ms phase', (duration, rate) => {
    expect(bandwidthSpeedGuide(32 * 1024 ** 2, duration)).toBe(rate);
  });

  it('formats caps and total allowances without confusing MiB and GiB', () => {
    expect(dataSize(32 * 1024 ** 2)).toBe('32 MiB');
    expect(dataSize(1024 ** 3)).toBe('1 GiB');
    expect(dataSize(2 * 1024 ** 3)).toBe('2 GiB');
  });
  it('shows no measurements rather than treating missing data as zero', () => {
    expect(statistics([])).toMatchObject({
      median: null,
      p95: null,
      maximum: null,
      variation: null,
      failed: 0,
    });
    expect(percentile([], 0.95)).toBeNull();
  });

  it('keeps browser delays separate from failed probes and resets variation across gaps', () => {
    const probes: Probe[] = [
      { ...base, status: 'ok', rtt_ms: 100, offset_ms: 0 },
      { ...base, status: 'ok', rtt_ms: 120, offset_ms: 2000 },
      { ...base, status: 'browser_delayed', offset_ms: 60000, browser_delay_ms: 58000 },
      { ...base, status: 'ok', rtt_ms: 300, offset_ms: 62000 },
      { ...base, status: 'timeout', offset_ms: 64000 },
      { ...base, status: 'ok', rtt_ms: 100, offset_ms: 70000 },
    ];
    expect(statistics(probes)).toEqual({
      successful: 4,
      failed: 1,
      delayed: 1,
      median: 110,
      p95: 300,
      maximum: 300,
      variation: 20,
      longestFailureMs: 6000,
    });
  });

  it('averages both middle observations for even sample counts', () => {
    const observations: Probe[] = [100, 900, 200, 300].map((rtt_ms, index) => ({
      ...base,
      status: 'ok',
      rtt_ms,
      offset_ms: index * 2000,
    }));
    expect(statistics(observations).median).toBe(250);
    expect(statistics(observations.slice(0, 3)).median).toBe(200);
    expect(statistics(observations.slice(0, 1)).median).toBe(100);
  });

  it('excludes latency samples that straddle load phases and leaves missing throughput unavailable', () => {
    const rows = bandwidthStatistics({
      target: { id: 'erin', label: 'Erin', url: '/ping', kind: 'node' },
      max_bytes_per_direction: 32 * 1024 ** 2,
      transfer_duration_ms: 5000,
      started_at_ms: 0,
      duration_ms: 3000,
      state: 'completed',
      phases: [
        { phase: 'idle', offset_ms: 0, end_ms: 1000, transferred_bytes: 0, ended_by: 'duration' },
        {
          phase: 'download',
          offset_ms: 1000,
          end_ms: 2000,
          transferred_bytes: 1000000,
          ended_by: 'duration',
        },
        {
          phase: 'upload',
          offset_ms: 2000,
          end_ms: 3000,
          transferred_bytes: 0,
          ended_by: 'duration',
        },
      ],
      probes: [
        { ...base, status: 'ok', offset_ms: 100, rtt_ms: 100 },
        { ...base, status: 'ok', offset_ms: 900, rtt_ms: 200 },
        { ...base, status: 'ok', offset_ms: 1100, rtt_ms: 300 },
        { ...base, status: 'ok', offset_ms: 1200, rtt_ms: 800, hidden: true },
        { ...base, status: 'timeout', offset_ms: 2200 },
      ],
    });
    expect(rows.map((row) => row.statistics.median)).toEqual([100, 300, null]);
    expect(rows.map((row) => row.mbps)).toEqual([null, 8, null]);
    expect(rows[2]?.statistics.failed).toBe(1);
  });
});
