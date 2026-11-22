<script lang="ts">
  import type { Recording } from './types';
  import { contextAt, observationIsStale } from './context';
  import { duration } from './metrics';

  let { recording, offset }: { recording: Recording | null; offset: number | null } = $props();
  const position = $derived(offset ?? recording?.duration_ms ?? 0);
  const context = $derived(recording ? contextAt(recording, position) : undefined);
  const app = $derived(context?.snapshot?.app);
  const sample = $derived(app?.process);
  const stale = $derived(!context || observationIsStale(sample?.captured_at_ms, context, position));

  function bytes(value: number | null | undefined): string {
    if (value == null) return '—';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    const unit = Math.min(units.length - 1, Math.floor(Math.log2(Math.max(1, value)) / 10));
    return `${(value / 1024 ** unit).toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
  }

  function rate(value: number | null | undefined): string {
    return value == null ? '—' : `${bytes(value)}/s`;
  }
</script>

{#if recording?.monitored_app}
  <article class="context-card process-card">
    <div class="context-title">
      <strong>{recording.monitored_app.label} · {recording.monitored_app.node_label}</strong>
      <span class="status-label" class:bad={sample?.state === 'stopped'}>
        {sample ? `${sample.state}${stale ? ' · stale' : ''}` : 'No sample'}
      </span>
    </div>
    {#if sample?.state === 'running'}
      <dl class="process-metrics">
        <div>
          <dt>Process CPU</dt>
          <dd>{sample.cpu_percent == null ? '—' : `${sample.cpu_percent.toFixed(1)}%`}</dd>
        </div>
        <div>
          <dt>Memory (RSS)</dt>
          <dd>{bytes(sample.memory_rss_bytes)}</dd>
        </div>
        <div>
          <dt>Disk reads</dt>
          <dd>{rate(sample.read_bytes_per_second)}</dd>
        </div>
        <div>
          <dt>Disk writes</dt>
          <dd>{rate(sample.write_bytes_per_second)}</dd>
        </div>
        <div>
          <dt>Players</dt>
          <dd>
            {app?.player_count == null
              ? '—'
              : `${app.player_count}${app.player_capacity == null ? '' : ` / ${app.player_capacity}`}`}
          </dd>
        </div>
        <div>
          <dt>Threads</dt>
          <dd>{sample.thread_count ?? '—'}</dd>
        </div>
        <div>
          <dt>Processes</dt>
          <dd>{sample.process_count ?? '—'}</dd>
        </div>
        <div>
          <dt>Process uptime</dt>
          <dd>
            {sample.started_at_ms == null
              ? '—'
              : duration(Math.max(0, sample.captured_at_ms - sample.started_at_ms))}
          </dd>
        </div>
      </dl>
      {#if sample.cpu_percent == null}<p class="caption">
          Rates need two recent samples of the same process tree.
        </p>{/if}
      <small class="muted"
        >PID {sample.pid} · {new Date(sample.captured_at_ms).toLocaleTimeString()} · +{(
          (context?.offset_ms ?? 0) / 1000
        ).toFixed(0)}s</small
      >
    {:else}
      <p class="caption">
        {sample?.state === 'stopped'
          ? 'App stopped at this time.'
          : sample?.state === 'unavailable'
            ? 'Process sample unavailable.'
            : 'Waiting for process sample.'}
      </p>
    {/if}
  </article>
  <p class="caption">
    Process tree totals · 100% CPU = one logical core · RSS may double-count shared memory ·
    OS-reported disk I/O
  </p>
{:else}
  <p class="empty-state small">Select an app to record its usage alongside latency.</p>
{/if}
