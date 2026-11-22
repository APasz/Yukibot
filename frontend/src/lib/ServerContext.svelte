<script lang="ts">
  import type { Recording } from './types';
  import { milliseconds } from './metrics';
  import { contextAt, observationIsStale } from './context';
  let { recording, offset }: { recording: Recording; offset: number | null } = $props();
  const position = $derived(offset ?? recording.duration_ms);
  const context = $derived(contextAt(recording, position));
  const snapshot = $derived(context?.snapshot);
</script>

<div class="section-heading">
  <div>
    <h2>Server context</h2>
  </div>
  <span class="muted"
    >{context ? `+${(context.offset_ms / 1000).toFixed(0)}s` : 'Waiting for sample'}</span
  >
</div>
{#if snapshot}
  <div class="context-grid">
    {#each snapshot.nodes as node}
      {@const label =
        recording.targets.find((target) => target.id === node.target_id)?.label ?? node.target_id}
      {@const stale =
        !context ||
        observationIsStale(node.captured_at_ms, context, position) ||
        node.availability !== 'online'}
      {@const probeStale =
        !context || observationIsStale(node.portal_probe_captured_at_ms, context, position)}
      <article class="context-card">
        <div class="context-title">
          <strong>{label}</strong><span
            class:bad={node.availability === 'offline'}
            class="status-label">{node.availability}{stale ? ' · stale' : ''}</span
          >
        </div>
        <dl>
          <div>
            <dt>CPU</dt>
            <dd>
              {node.cpu_percent === null || node.cpu_percent === undefined
                ? '—'
                : `${node.cpu_percent}%`}
            </dd>
          </div>
          <div>
            <dt>Busiest core</dt>
            <dd>
              {node.cpu_per_core_percent?.length
                ? `${Math.max(...node.cpu_per_core_percent)}%`
                : '—'}
            </dd>
          </div>
          <div>
            <dt>Memory</dt>
            <dd>
              {node.ram_percent === null || node.ram_percent === undefined
                ? '—'
                : `${node.ram_percent}%`}
            </dd>
          </div>
          <div>
            <dt>Portal → node HTTP</dt>
            <dd>
              {milliseconds(node.portal_latency_ms)}{node.portal_latency_ms != null && probeStale
                ? ' · stale'
                : ''}
            </dd>
          </div>
        </dl>
        <small class="muted"
          >Host: {node.captured_at_ms ? new Date(node.captured_at_ms).toLocaleTimeString() : '—'} · Probe:
          {node.portal_probe_captured_at_ms
            ? new Date(node.portal_probe_captured_at_ms).toLocaleTimeString()
            : '—'}</small
        >
      </article>
    {/each}
  </div>
{:else}
  <p class="empty-state">
    {position < 0 || position > recording.duration_ms
      ? 'Outside this recording.'
      : context?.error
        ? 'Server sample unavailable.'
        : 'Waiting for server sample.'}
  </p>
{/if}
<p class="caption">Host usage · Portal → node HTTP. Shared hosts are not independent references.</p>
