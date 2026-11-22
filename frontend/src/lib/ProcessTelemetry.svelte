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
        {sample ? `${sample.state}${stale ? ' · stale' : ''}` : 'No observation at this time'}
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
          CPU and I/O rates need two recent observations of the same process tree.
        </p>{/if}
      <small class="muted"
        >PID {sample.pid} · captured {new Date(sample.captured_at_ms).toLocaleTimeString()} · sample at
        +{((context?.offset_ms ?? 0) / 1000).toFixed(0)}s</small
      >
    {:else}
      <p class="caption">
        {sample?.state === 'stopped'
          ? 'The selected app was stopped at this time. Its earlier observations remain in this recording.'
          : sample?.state === 'unavailable'
            ? 'Process measurements could not be read. This does not establish whether the game was unhealthy.'
            : 'Waiting for a process observation. Missing samples remain unavailable.'}
      </p>
    {/if}
  </article>
  <p class="caption">
    Inspect a moment on the connection timeline to see its process sample. CPU is summed across the
    app process tree: 100% represents one fully used logical CPU. RSS includes child processes and
    can count shared memory more than once. Disk rates show OS-reported process I/O; these
    measurements do not establish game tick or simulation health.
  </p>
{:else}
  <p class="empty-state small">
    Choose a running app before starting a recording to capture its CPU, memory, disk I/O and player
    count alongside your connection.
  </p>
{/if}
