import type { RecordedContext, Recording } from './types';

const MAX_OBSERVATION_AGE_MS = 15000;

export function contextAt(recording: Recording, offset: number): RecordedContext | undefined {
  if (offset < 0 || offset > recording.duration_ms) return undefined;
  return recording.contexts.filter((item) => item.offset_ms <= offset).at(-1);
}

export function observationIsStale(
  capturedAt: number | null | undefined,
  context: RecordedContext,
  position: number,
): boolean {
  if (capturedAt == null || !context.snapshot) return true;
  const age = context.snapshot.observed_at_ms - capturedAt + position - context.offset_ms;
  return age > MAX_OBSERVATION_AGE_MS;
}
