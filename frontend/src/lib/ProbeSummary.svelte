<script lang="ts">
  import type { Recording } from './types';
  import { milliseconds, statistics } from './metrics';
  let { recording }: { recording: Recording } = $props();
  const summaries = $derived(
    recording.targets.map((target) => ({
      target,
      stats: statistics(recording.probes.filter((probe) => probe.target_id === target.id)),
    })),
  );
</script>

<div class="table-scroll">
  <table>
    <caption class="sr-only">Browser to endpoint HTTP measurements</caption>
    <thead
      ><tr
        ><th>Browser → endpoint</th><th>Median</th><th>95th percentile</th><th>Maximum</th><th
          >RTT variation</th
        ><th>Failed probes</th><th>Browser delayed</th></tr
      ></thead
    >
    <tbody
      >{#each summaries as { target, stats }}
        <tr
          ><th
            ><span class="node-dot"></span>{target.label}<small
              >{target.kind === 'portal'
                ? 'Portal health endpoint'
                : 'Direct node health endpoint'}</small
            ></th
          >
          <td>{milliseconds(stats.median)}</td><td>{milliseconds(stats.p95)}</td><td
            >{milliseconds(stats.maximum)}</td
          ><td>{milliseconds(stats.variation)}</td>
          <td class:bad={stats.failed > 0}
            >{stats.failed} <small>/ {stats.successful + stats.failed} requests</small></td
          ><td>{stats.delayed}</td></tr
        >
      {/each}</tbody
    >
  </table>
</div>
<p class="caption">
  Variation is the mean absolute change between consecutive successful RTTs. Failed probes are HTTP
  observations, not a game packet-loss rate.
</p>
