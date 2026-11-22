<script lang="ts">
  import type { BandwidthTest } from './types';
  import { bandwidthStatistics, dataSize, milliseconds } from './metrics';

  let { test }: { test: BandwidthTest } = $props();
  const rows = $derived(bandwidthStatistics(test));
  const baseline = $derived(rows[0]?.statistics.median ?? null);
  const labels = { idle: 'Idle', download: 'Download', upload: 'Upload' } as const;
</script>

<div class="bandwidth-result">
  <div class="section-heading">
    <h3>{test.target.label}</h3>
    <span class:bad={test.state === 'failed'} class="muted">
      {test.state === 'completed'
        ? 'Complete'
        : test.state === 'cancelled'
          ? 'Cancelled · partial'
          : 'Failed · partial'}
    </span>
  </div>
  {#if test.error}<p class="caption bad">{test.error}</p>{/if}
  <div class="table-scroll">
    <table>
      <thead
        ><tr><th>Phase</th><th>Speed</th><th>Latency</th><th>+ Idle</th><th>Probes</th></tr></thead
      >
      <tbody>
        {#each rows as row}
          <tr>
            <th
              >{labels[row.phase.phase]}{#if row.phase.ended_by === 'byte_limit'}
                <span class="muted">{' · data cap'}</span>{/if}</th
            >
            <td>{row.mbps === null ? '—' : `${row.mbps.toFixed(1)} Mbps`}</td>
            <td>{milliseconds(row.statistics.median)}</td>
            <td
              >{row.phase.phase === 'idle' || baseline === null || row.statistics.median === null
                ? '—'
                : milliseconds(row.statistics.median - baseline)}</td
            >
            <td
              >{row.statistics.successful} ok{#if row.statistics.failed}
                · <span class="bad">{row.statistics.failed} failed</span
                >{/if}{#if row.statistics.delayed}
                · {row.statistics.delayed} delayed{/if}</td
            >
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
  <p class="caption">
    {dataSize(
      test.phases.reduce((total, phase) => total + phase.transferred_bytes, 0),
    )}{#if test.max_bytes_per_direction}
      {' · '}{dataSize(test.max_bytes_per_direction)}{#if test.transfer_duration_ms}
        {' / '}{test.transfer_duration_ms / 1000} s{/if} per direction{/if} · HTTP median latency · acknowledged
    upload
  </p>
</div>
