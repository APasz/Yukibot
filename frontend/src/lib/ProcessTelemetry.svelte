<script lang="ts">
  import type { Recording } from './types';
  import { contextAt, observationIsStale } from './context';
  import { duration } from './metrics';

  let { recording, offset }: { recording: Recording; offset: number | null } = $props();
  const position = $derived(offset ?? recording.duration_ms);
  const context = $derived(contextAt(recording, position));
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

{#if recording.monitored_app}
  <article class="process-card">
    <div class="context-title">
      <h2>{recording.monitored_app.label} · {recording.monitored_app.node_label}</h2>
      <span class="status-label" class:bad={sample?.state === 'stopped'}>
        {sample ? `${sample.state}${stale ? ' · stale' : ''}` : 'No sample'}
      </span>
    </div>
    {#if sample?.state === 'running'}
      <dl class="process-metrics">
        <div>
          <dt>CPU</dt>
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
          <dt>Uptime</dt>
          <dd>
            {sample.started_at_ms == null
              ? '—'
              : duration(Math.max(0, sample.captured_at_ms - sample.started_at_ms))}
          </dd>
        </div>
      </dl>
      {#if sample.cpu_percent == null}<p class="caption">Waiting for rates…</p>{/if}
      <details>
        <summary>Process details</summary>
        <dl>
          <div>
            <dt>Threads</dt>
            <dd>{sample.thread_count ?? '—'}</dd>
          </div>
          <div>
            <dt>Processes</dt>
            <dd>{sample.process_count ?? '—'}</dd>
          </div>
        </dl>
        <small class="muted"
          >PID {sample.pid ?? '—'} · {new Date(sample.captured_at_ms).toLocaleTimeString()} · +{(
            (context?.offset_ms ?? 0) / 1000
          ).toFixed(0)}s</small
        >
      </details>
    {/if}
  </article>
{/if}
