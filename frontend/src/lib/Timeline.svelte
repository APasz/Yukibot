<script lang="ts">
  import type { Recording } from './types';
  import { milliseconds } from './metrics';

  let {
    recordings,
    targetId,
    onseek,
  }: {
    recordings: { label: string; recording: Recording }[];
    targetId: string;
    onseek: (offset: number | null) => void;
  } = $props();
  const colours = ['#a99aff', '#65d4c5', '#f4bd72', '#83b4ff', '#e99dce', '#b7d983'];
  let cursor = $state<number | null>(null);
  const start = $derived(Math.min(...recordings.map(({ recording }) => recording.started_at_ms)));
  const end = $derived(
    Math.max(
      start + 1000,
      ...recordings.map(({ recording }) => recording.started_at_ms + recording.duration_ms),
    ),
  );
  const series = $derived(
    recordings.flatMap(({ label, recording }, reportIndex) =>
      recording.targets
        .filter((target) => !targetId || target.id === targetId)
        .map((target, targetIndex) => ({
          label: `${recordings.length > 1 ? `${label} · ` : ''}${target.label}`,
          recording,
          target,
          colour: colours[(reportIndex * 3 + targetIndex) % colours.length] ?? '#a99aff',
          probes: recording.probes.filter((probe) => probe.target_id === target.id),
        })),
    ),
  );
  const ceiling = $derived(
    Math.max(
      100,
      ...series.flatMap((item) =>
        item.probes.map((probe) => (probe.status === 'ok' ? probe.rtt_ms : 0)),
      ),
    ) * 1.15,
  );
  const x = (timestamp: number) => 62 + ((timestamp - start) / (end - start)) * 900;
  const y = (rtt: number) => 186 - (rtt / ceiling) * 154;

  function segments(item: (typeof series)[number]): string[] {
    const paths: string[] = [];
    let current = '';
    let previousOffset: number | null = null;
    for (const probe of item.probes) {
      if (
        probe.status !== 'ok' ||
        (previousOffset !== null && probe.offset_ms - previousOffset > 15000)
      ) {
        if (current) paths.push(current);
        current = '';
      }
      if (probe.status === 'ok') {
        current += `${current ? ' L' : 'M'}${x(item.recording.started_at_ms + probe.offset_ms)},${y(probe.rtt_ms)}`;
      }
      previousOffset = probe.offset_ms;
    }
    if (current) paths.push(current);
    return paths;
  }

  function seek(event: PointerEvent): void {
    const bounds =
      event.currentTarget instanceof SVGElement
        ? event.currentTarget.getBoundingClientRect()
        : null;
    if (!bounds) return;
    cursor = Math.max(
      0,
      Math.min(1, (event.clientX - bounds.left - bounds.width * 0.062) / (bounds.width * 0.9)),
    );
    const timestamp = start + cursor * (end - start);
    onseek(timestamp - (recordings[0]?.recording.started_at_ms ?? start));
  }

  const ticks = $derived(
    [0, 1, 2, 3, 4].map((index) => ({
      value: (index / 4) * ceiling,
      position: y((index / 4) * ceiling),
    })),
  );
</script>

<div class="timeline-wrap">
  <div class="chart-legend">
    {#each series as item}<span><i style={`background:${item.colour}`}></i>{item.label}</span
      >{/each}
    <span><i class="failure-key"></i>Failed request</span>
  </div>
  <svg
    class="timeline"
    viewBox="0 0 1000 226"
    role="img"
    aria-label="HTTP round-trip latency over time"
    onpointermove={seek}
    onpointerleave={() => {
      cursor = null;
      onseek(null);
    }}
  >
    <title
      >HTTP round-trip latency; red marks indicate failed requests, grey marks indicate browser
      scheduling delays.</title
    >
    {#each ticks as tick}
      <line x1="62" x2="962" y1={tick.position} y2={tick.position} class="chart-grid" />
      <text x="49" y={tick.position + 4} text-anchor="end" class="chart-text"
        >{Math.round(tick.value)}</text
      >
    {/each}
    <text x="18" y="17" class="chart-text">ms</text>
    {#each series as item}
      {#each segments(item) as path}<path
          d={path}
          stroke={item.colour}
          fill="none"
          stroke-width="2"
        />{/each}
      {#each item.probes as probe}
        {#if probe.status === 'ok'}
          <circle
            cx={x(item.recording.started_at_ms + probe.offset_ms)}
            cy={y(probe.rtt_ms)}
            r="2"
            fill={item.colour}><title>{item.label}: {milliseconds(probe.rtt_ms)}</title></circle
          >
        {:else}
          <line
            x1={x(item.recording.started_at_ms + probe.offset_ms)}
            x2={x(item.recording.started_at_ms + probe.offset_ms)}
            y1="17"
            y2="26"
            stroke={probe.status === 'browser_delayed' ? '#8994a6' : '#f18087'}
            stroke-width="3"><title>{item.label}: {probe.status}</title></line
          >
        {/if}
      {/each}
      {#each item.recording.markers as marker}
        <line
          x1={x(item.recording.started_at_ms + marker.offset_ms)}
          x2={x(item.recording.started_at_ms + marker.offset_ms)}
          y1="30"
          y2="188"
          stroke={item.colour}
          stroke-dasharray="3 5"
          opacity="0.5"><title>{marker.label}</title></line
        >
      {/each}
    {/each}
    {#if cursor !== null}<line
        x1={62 + cursor * 900}
        x2={62 + cursor * 900}
        y1="15"
        y2="189"
        stroke="#ccd3e0"
        stroke-dasharray="2 3"
      />{/if}
    {#each [0, 0.25, 0.5, 0.75, 1] as fraction}
      <text x={62 + fraction * 900} y="214" text-anchor="middle" class="chart-text"
        >{new Date(start + fraction * (end - start)).toLocaleTimeString([], {
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        })}</text
      >
    {/each}
  </svg>
  <p class="caption">
    {cursor === null
      ? 'Hover to inspect server context at that time. Event markers are shown as dashed lines.'
      : new Date(start + cursor * (end - start)).toLocaleString()} · Missing observations remain gaps.
  </p>
</div>
