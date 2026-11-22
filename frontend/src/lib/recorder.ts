import type {
  BandwidthEndpoint,
  BandwidthLimits,
  BandwidthTest,
  Context,
  Limits,
  MonitoredApp,
  Probe,
  Recording,
  Target,
} from './types';
import { pause } from './async';
import { measureProbe, type ProbeTiming } from './probes';
import { runBandwidthTest, type BandwidthProgress } from './bandwidth';
export { measureProbe } from './probes';
export class Recorder {
  private readonly abort = new AbortController();
  private readonly startedAt = performance.now();
  private readonly tasks: Promise<void>[];
  private readonly stopTimer: ReturnType<typeof setTimeout>;
  private stopping: Promise<void> | undefined;
  private stoppedAt: number | undefined;
  private bandwidthActive = false;
  readonly recording: Recording;

  constructor(
    targets: Target[],
    title: string,
    private readonly limits: Limits,
    private readonly onChange: (recording: Recording) => void,
    private readonly loadContext: (signal: AbortSignal) => Promise<Context>,
    private readonly onStop: () => void,
    monitoredApp: MonitoredApp | null = null,
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
      monitored_app: monitoredApp,
      bandwidth_tests: [],
    };
    this.tasks = [...targets.map((target) => this.sampleTarget(target)), this.sampleContext()];
    this.stopTimer = setTimeout(() => {
      void this.stop();
    }, limits.max_duration_ms);
    this.publish();
  }

  get signal(): AbortSignal {
    return this.abort.signal;
  }

  requireBandwidthCapacity(limits: BandwidthLimits): void {
    if (this.signal.aborted) throw new Error('The recording has stopped.');
    if (this.bandwidthActive) throw new Error('A speed test is already running.');
    if ((this.recording.bandwidth_tests?.length ?? 0) >= limits.max_tests_per_recording) {
      throw new Error('Bandwidth test limit reached for this recording.');
    }
  }

  measureBandwidth(
    target: Target,
    endpoint: BandwidthEndpoint,
    signal: AbortSignal,
    onProgress: (progress: BandwidthProgress) => void,
  ): Promise<BandwidthTest> {
    this.requireBandwidthCapacity(endpoint.grant.limits);
    const tests = (this.recording.bandwidth_tests ??= []);
    this.bandwidthActive = true;
    const offset = this.elapsed();
    const task = runBandwidthTest({
      target,
      endpoint,
      signal: AbortSignal.any([signal, this.signal]),
      now: () => this.elapsed(),
      timeoutMs: this.limits.timeout_ms,
      onProgress,
    })
      .then((test) => {
        tests.push({ offset_ms: offset, test });
        this.publish();
        return test;
      })
      .finally(() => {
        this.bandwidthActive = false;
      });
    this.tasks.push(
      task.then(
        () => {},
        () => {},
      ),
    );
    return task;
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
