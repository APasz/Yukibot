import type { Context, Limits, Probe, Recording, Target } from './types';

interface ProbeTiming {
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

function pause(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(handle);
      signal.removeEventListener('abort', finish);
      resolve();
    };
    const handle = setTimeout(finish, milliseconds);
    signal.addEventListener('abort', finish, { once: true });
    if (signal.aborted) finish();
  });
}

export class Recorder {
  private readonly abort = new AbortController();
  private readonly startedAt = performance.now();
  private readonly tasks: Promise<void>[];
  private readonly stopTimer: ReturnType<typeof setTimeout>;
  private stopping: Promise<void> | undefined;
  private stoppedAt: number | undefined;
  readonly recording: Recording;

  constructor(
    targets: Target[],
    title: string,
    private readonly limits: Limits,
    private readonly onChange: (recording: Recording) => void,
    private readonly loadContext: (signal: AbortSignal) => Promise<Context>,
    private readonly onStop: () => void,
  ) {
    this.recording = {
      schema_version: 1,
      id: crypto.randomUUID(),
      started_at_ms: Date.now(),
      duration_ms: 0,
      title,
      notes: '',
      targets,
      probes: [],
      contexts: [],
      markers: [],
    };
    this.tasks = [...targets.map((target) => this.sampleTarget(target)), this.sampleContext()];
    this.stopTimer = setTimeout(() => {
      void this.stop();
    }, limits.max_duration_ms);
    this.publish();
  }

  private elapsed(): number {
    return (
      this.stoppedAt ??
      Math.min(this.limits.max_duration_ms, Math.round(performance.now() - this.startedAt))
    );
  }

  private publish(): void {
    this.recording.duration_ms = this.elapsed();
    this.onChange({ ...this.recording });
  }

  mark(label: string): void {
    if (this.abort.signal.aborted) throw new Error('The recording has stopped.');
    if (this.recording.markers.length >= 100)
      throw new Error('This recording already has 100 event markers.');
    this.recording.markers.push({
      offset_ms: this.elapsed(),
      label: label.trim() || 'Interaction delayed',
    });
    this.publish();
  }

  private async sampleTarget(target: Target): Promise<void> {
    let scheduledAt = performance.now();
    while (!this.abort.signal.aborted && this.elapsed() < this.limits.max_duration_ms) {
      const delay = Math.max(0, Math.round(performance.now() - scheduledAt));
      const timing: ProbeTiming = {
        target_id: target.id,
        offset_ms: this.elapsed(),
        hidden: document.hidden,
        browser_delay_ms: delay,
      };
      const probe: Probe | null =
        delay > this.limits.interval_ms * 2
          ? { ...timing, status: 'browser_delayed' }
          : await measureProbe(target, timing, this.limits.timeout_ms, this.abort.signal);
      if (probe) {
        this.recording.probes.push(probe);
        this.publish();
      }
      scheduledAt = performance.now() + this.limits.interval_ms;
      await pause(this.limits.interval_ms, this.abort.signal);
    }
  }

  private async sampleContext(): Promise<void> {
    while (!this.abort.signal.aborted && this.elapsed() < this.limits.max_duration_ms) {
      try {
        const snapshot = await this.loadContext(
          AbortSignal.any([this.abort.signal, AbortSignal.timeout(this.limits.timeout_ms)]),
        );
        if (!this.abort.signal.aborted)
          this.recording.contexts.push({ offset_ms: this.elapsed(), snapshot, error: null });
      } catch {
        if (!this.abort.signal.aborted)
          this.recording.contexts.push({
            offset_ms: this.elapsed(),
            snapshot: null,
            error: 'unavailable',
          });
      }
      if (!this.abort.signal.aborted) this.publish();
      await pause(this.limits.context_interval_ms, this.abort.signal);
    }
  }

  stop(): Promise<void> {
    return (this.stopping ??= this.finish());
  }

  private async finish(): Promise<void> {
    this.stoppedAt = this.elapsed();
    clearTimeout(this.stopTimer);
    this.abort.abort();
    await Promise.all(this.tasks);
    this.publish();
    this.onStop();
  }
}
