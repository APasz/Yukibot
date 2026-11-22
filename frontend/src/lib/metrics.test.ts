import { describe, expect, it } from 'vitest';
import { percentile, statistics } from './metrics';
import type { Probe } from './types';

const base = { target_id: 'erin', hidden: false, browser_delay_ms: 0 };

describe('diagnostic statistics', () => {
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
});
