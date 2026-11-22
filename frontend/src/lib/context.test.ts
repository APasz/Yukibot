import { describe, expect, it } from 'vitest';
import { contextAt, observationIsStale } from './context';
import type { RecordedContext, Recording } from './types';

const context: RecordedContext = {
  offset_ms: 5000,
  snapshot: { observed_at_ms: 100000, nodes: [] },
  error: null,
};
const recording: Recording = {
  schema_version: 1,
  id: '4e01b892-56cc-488e-919c-79cf8134d9b0',
  started_at_ms: 0,
  duration_ms: 60000,
  title: 'UL',
  notes: '',
  targets: [{ id: 'erin', label: 'Erin', url: '/ping', kind: 'node' }],
  probes: [],
  contexts: [context, { offset_ms: 30000, snapshot: null, error: 'unavailable' }],
  markers: [],
};

describe('server context timing', () => {
  it('does not show later samples, recovered metrics across a failed sample, or data outside the recording', () => {
    expect(contextAt(recording, 0)).toBeUndefined();
    expect(contextAt(recording, 4999)).toBeUndefined();
    expect(contextAt(recording, 5000)).toBe(context);
    expect(contextAt(recording, 30000)?.error).toBe('unavailable');
    expect(contextAt(recording, -1)).toBeUndefined();
    expect(contextAt(recording, 60001)).toBeUndefined();
  });

  it('ages host and portal measurements from their capture time, including gaps after the context sample', () => {
    expect(observationIsStale(95000, context, 5000)).toBe(false);
    expect(observationIsStale(95000, context, 16000)).toBe(true);
    expect(observationIsStale(80000, context, 5000)).toBe(true);
    expect(observationIsStale(null, context, 5000)).toBe(true);
  });
});
