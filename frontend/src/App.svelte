<script lang="ts">
  import { onMount } from 'svelte';
  import AppShell from './lib/AppShell.svelte';
  import Timeline from './lib/Timeline.svelte';
  import ProbeSummary from './lib/ProbeSummary.svelte';
  import ServerContext from './lib/ServerContext.svelte';
  import ProcessTelemetry from './lib/ProcessTelemetry.svelte';
  import BandwidthResults from './lib/BandwidthResults.svelte';
  import { runBandwidthTest, type BandwidthProgress } from './lib/bandwidth';
  import { api, ApiError, errorMessage, requireData } from './lib/api';
  import { readDraft, writeDraft } from './lib/drafts';
  import { bandwidthSpeedGuide, dataSize, duration, probeLabel } from './lib/metrics';
  import { Recorder } from './lib/recorder';
  import type {
    AppList,
    MonitoredApp,
    Configuration,
    Identity,
    Recording,
    Report,
    ReportSummary,
    BandwidthTest,
    BandwidthSize,
    BandwidthDuration,
  } from './lib/types';

  let configuration = $state.raw<Configuration | null>(null);
  let loading = $state(true);
  let running = $state(false);
  let recording = $state.raw<Recording | null>(null);
  let comparison = $state.raw<Report | null>(null);
  let reports = $state<ReportSummary[]>([]);
  let selectedIds = $state<string[]>([]);
  let title = $state('Game connection check');
  let notes = $state('');
  let markerLabel = $state('Interaction delayed');
  let targetId = $state('');
  let selectedOffset = $state<number | null>(null);
  let pending = $state<'save' | 'load' | 'refresh' | null>(null);
  let notice = $state<{ tone: 'info' | 'error'; text: string } | null>(null);
  let needsLogin = $state(false);
  let saved = $state(false);
  let owner = $state<Pick<Identity, 'user_id' | 'display_name'> | null>(null);
  let apps = $state.raw<AppList>({ apps: [], unavailable_nodes: [] });
  let appsLoading = $state(false);
  let appsError = $state<string | null>(null);
  let selectedAppKey = $state('');
  let includeBandwidth = $state(false);
  let bandwidthTargetId = $state('');
  let bandwidthSize = $state<BandwidthSize | null>(null);
  let bandwidthDuration = $state<BandwidthDuration | null>(null);
  let bandwidthProgress = $state<BandwidthProgress | null>(null);
  let bandwidthResult = $state.raw<BandwidthTest | null>(null);
  let bandwidthError = $state<string | null>(null);
  let bandwidthAbort: AbortController | null = null;
  let recorder: Recorder | null = null;
  let draftPending: Promise<void> = Promise.resolve();
  let detailsTimer: ReturnType<typeof setTimeout> | undefined;

  const plotted = $derived(
    recording
      ? [
          { label: owner?.display_name ?? title, recording },
          ...(comparison
            ? [{ label: comparison.summary.owner_name, recording: comparison.recording }]
            : []),
        ]
      : [],
  );
  const visibleProbes = $derived(
    recording?.probes
      .filter((probe) => !targetId || probe.target_id === targetId)
      .slice(-100)
      .reverse() ?? [],
  );
  const owned = $derived(owner === null || owner.user_id === configuration?.identity.user_id);
  const selectedApp = $derived(apps.apps.find((app) => appKey(app) === selectedAppKey) ?? null);
  const bandwidthTargets = $derived(
    configuration?.targets.filter((target) =>
      configuration?.bandwidth_target_ids.includes(target.id),
    ) ?? [],
  );

  function appKey(app: MonitoredApp): string {
    return JSON.stringify([app.target_id, app.app_name]);
  }

  async function refreshApps(): Promise<void> {
    if (appsLoading || running) return;
    appsLoading = true;
    try {
      apps = requireData(
        await api.GET('/api/network/apps', { signal: AbortSignal.timeout(15000) }),
      );
      appsError = null;
    } catch (error) {
      appsError = errorMessage(error);
      if (error instanceof ApiError && error.status === 401) fail(error);
    } finally {
      appsLoading = false;
    }
  }

  function fail(error: unknown): void {
    notice = { tone: 'error', text: errorMessage(error) };
    if (error instanceof ApiError && error.status === 401) needsLogin = true;
  }

  function keepDraft(): void {
    if (!configuration || !recording || !owned) return;
    const userId = configuration.identity.user_id;
    const copy = structuredClone({ ...recording, title: title.trim() || recording.title, notes });
    draftPending = draftPending
      .then(() => writeDraft(userId, copy))
      .catch((error: unknown) => {
        notice = {
          tone: 'error',
          text: `Draft save failed: ${errorMessage(error)}. Export to keep this recording.`,
        };
      });
  }

  function updateDetails(field: 'title' | 'notes', value: string): void {
    if (field === 'title') title = value;
    else notes = value;
    saved = false;
    clearTimeout(detailsTimer);
    detailsTimer = setTimeout(keepDraft, 500);
  }

  async function fetchReports(): Promise<void> {
    reports = requireData(
      await api.GET('/api/network/reports', { signal: AbortSignal.timeout(15000) }),
    ).reports;
  }

  async function refreshReports(): Promise<void> {
    if (pending !== null) return;
    pending = 'refresh';
    try {
      await fetchReports();
    } catch (error) {
      fail(error);
    } finally {
      pending = null;
    }
  }

  onMount(() => {
    let destroyed = false;
    void (async () => {
      try {
        const config = requireData(
          await api.GET('/api/network/config', { signal: AbortSignal.timeout(15000) }),
        );
        if (destroyed) return;
        configuration = config;
        selectedIds = config.targets.map((target) => target.id);
        bandwidthTargetId = config.bandwidth_target_ids[0] ?? '';
        bandwidthSize =
          config.bandwidth_sizes.find(
            (size) => size === config.bandwidth_limits.max_bytes_per_direction,
          ) ??
          config.bandwidth_sizes[0] ??
          null;
        bandwidthDuration =
          config.bandwidth_durations.find(
            (duration) => duration === config.bandwidth_limits.transfer_duration_ms,
          ) ??
          config.bandwidth_durations[0] ??
          null;
        void refreshApps();
        try {
          const draft = await readDraft(config.identity.user_id);
          if (destroyed) return;
          if (draft) {
            recording = draft;
            owner = config.identity;
            title = draft.title;
            notes = draft.notes ?? '';
            selectedAppKey = draft.monitored_app ? appKey(draft.monitored_app) : '';
            notice = {
              tone: 'info',
              text: 'Draft restored.',
            };
          }
        } catch (error) {
          fail(error);
        }
        await refreshReports();
      } catch (error) {
        fail(error);
      } finally {
        if (!destroyed) loading = false;
      }
    })();
    const timer = setInterval(() => {
      if (running) keepDraft();
    }, 5000);
    const appsTimer = setInterval(() => {
      if (configuration && !destroyed) void refreshApps();
    }, 10000);
    return () => {
      destroyed = true;
      clearInterval(timer);
      clearInterval(appsTimer);
      clearTimeout(detailsTimer);
      keepDraft();
      bandwidthAbort?.abort();
      if (recorder) void recorder.stop();
    };
  });

  function start(): void {
    if (!configuration || running || pending !== null || bandwidthProgress) return;
    const targets = configuration.targets.filter((target) => selectedIds.includes(target.id));
    if (!targets.length || !title.trim() || (selectedAppKey && !selectedApp)) return;
    const app = selectedApp;
    comparison = null;
    selectedOffset = null;
    targetId = '';
    notice = null;
    saved = false;
    if (!owned) notes = '';
    owner = configuration.identity;
    running = true;
    recorder = new Recorder(
      targets,
      title.trim(),
      configuration.limits,
      (next) => {
        recording = next;
      },
      async (signal) =>
        requireData(
          await api.GET('/api/network/context', {
            signal,
            params: { query: app ? { node_name: app.target_id, app_name: app.app_name } : {} },
          }),
        ),
      () => {
        running = false;
        keepDraft();
      },
      app,
    );
    keepDraft();
    if (includeBandwidth) void speedTest(recorder);
  }

  async function speedTest(activeRecorder: Recorder | null): Promise<void> {
    if (!configuration || bandwidthProgress || bandwidthSize === null || bandwidthDuration === null)
      return;
    const target = bandwidthTargets.find((item) => item.id === bandwidthTargetId);
    if (!target) return;
    try {
      activeRecorder?.requireBandwidthCapacity(configuration.bandwidth_limits);
    } catch (error) {
      bandwidthError = errorMessage(error);
      return;
    }
    const controller = new AbortController();
    bandwidthAbort = controller;
    const signal = AbortSignal.any([
      controller.signal,
      ...(activeRecorder ? [activeRecorder.signal] : []),
    ]);
    bandwidthProgress = { phase: 'connecting', transferred_bytes: 0 };
    bandwidthResult = null;
    bandwidthError = null;
    try {
      const endpoint = requireData(
        await api.POST('/api/network/bandwidth/{target_id}', {
          params: { path: { target_id: target.id } },
          body: { size_bytes: bandwidthSize, duration_ms: bandwidthDuration },
          signal: AbortSignal.any([signal, AbortSignal.timeout(10000)]),
        }),
        {
          409: 'This server is already being tested.',
          429: 'Wait a minute before testing this server again.',
          503: 'Speed test unavailable. This server may need updating.',
          422: 'Unsupported test limits. Refresh this page and try again.',
        },
      );
      signal.throwIfAborted();
      const onProgress = (progress: BandwidthProgress) => {
        bandwidthProgress = progress;
      };
      const result = activeRecorder
        ? await activeRecorder.measureBandwidth(target, endpoint, signal, onProgress)
        : await runBandwidthTest({
            target,
            endpoint,
            signal,
            timeoutMs: configuration.limits.timeout_ms,
            onProgress,
          });
      if (activeRecorder) {
        saved = false;
        keepDraft();
      } else bandwidthResult = result;
    } catch (error) {
      if (!signal.aborted) {
        if (error instanceof ApiError && error.status === 401) fail(error);
        else bandwidthError = errorMessage(error);
      }
    } finally {
      bandwidthProgress = null;
      bandwidthAbort = null;
    }
  }

  function exportJson(data: object, filename: string): void {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function save(): Promise<void> {
    if (!recording || running || pending !== null || !owned || saved) return;
    pending = 'save';
    notice = null;
    try {
      const report = requireData(
        await api.POST('/api/network/reports', {
          body: { ...recording, title: title.trim() || recording.title, notes },
          signal: AbortSignal.timeout(15000),
        }),
      );
      recording = report.recording;
      saved = true;
      keepDraft();
      await fetchReports();
    } catch (error) {
      fail(error);
    } finally {
      pending = null;
    }
  }

  async function loadReport(summary: ReportSummary, compare = false): Promise<void> {
    if (running || pending !== null) return;
    pending = 'load';
    try {
      const report = requireData(
        await api.GET('/api/network/reports/{report_id}', {
          params: { path: { report_id: summary.id } },
          signal: AbortSignal.timeout(15000),
        }),
      );
      if (compare) comparison = report;
      else {
        recording = report.recording;
        title = recording.title;
        notes = recording.notes ?? '';
        owner = { user_id: report.summary.owner_id, display_name: report.summary.owner_name };
        selectedAppKey = recording.monitored_app ? appKey(recording.monitored_app) : '';
        comparison = null;
        targetId = '';
        selectedOffset = null;
        saved = true;
      }
    } catch (error) {
      fail(error);
    } finally {
      pending = null;
    }
  }

  function exportReport(): void {
    if (!recording) return;
    const data = { ...recording, title: title.trim() || recording.title, notes };
    exportJson(data, `yukibot-network-${recording.id}.json`);
  }

  function mark(): void {
    try {
      recorder?.mark(markerLabel);
    } catch (error) {
      fail(error);
    }
  }
</script>

<AppShell identity={configuration?.identity ?? null}>
  <section class="page-intro">
    <h1>Network diagnostics</h1>
    <div class="session-status">
      <span class:recording={running} class="status-dot"></span>{running
        ? 'Recording'
        : recording
          ? 'Review'
          : 'Ready'}{#if recording}<strong>{duration(recording.duration_ms)}</strong>{/if}
    </div>
  </section>

  {#if notice}<div
      class:error={notice.tone === 'error'}
      class="notice"
      role={notice.tone === 'error' ? 'alert' : 'status'}
    >
      {notice.text}{#if needsLogin}
        <a href="/auth/login?next_path=%2Fmod-web%2Fnetwork">Sign in again</a>{/if}
    </div>{/if}

  {#if loading}<div class="panel empty-state">Loading…</div>
  {:else if configuration}
    <section class="panel session-panel" aria-label="Recording controls">
      <div class="section-heading">
        <h2>Session</h2>
        <span class="badge"
          >{Math.round(configuration.limits.max_duration_ms / 60000)} min limit</span
        >
      </div>
      <div class="control-grid">
        <label
          >Session name<input
            value={title}
            oninput={(event) => updateDetails('title', event.currentTarget.value)}
            maxlength="120"
            disabled={running || pending !== null}
          /></label
        >
        <div class="app-field">
          <label
            >{recording && !running ? 'App for next recording' : 'App to monitor'}
            <select bind:value={selectedAppKey} disabled={running || pending !== null}>
              <option value="">Network only</option>
              {#if selectedAppKey && !selectedApp}<option value={selectedAppKey} disabled
                  >{recording?.monitored_app?.label ?? 'Selected app'} · unavailable</option
                >{/if}
              {#each apps.apps as app}<option value={appKey(app)}
                  >{app.label} · {app.node_label}</option
                >{/each}
            </select>
          </label>
          {#if appsError}<p class="caption bad">
              App list unavailable: {appsError}
              <button
                class="text-button"
                disabled={appsLoading || running || pending !== null}
                onclick={() => void refreshApps()}>Retry</button
              >
            </p>
          {:else if apps.unavailable_nodes.length}<p class="caption">
              Unavailable: {apps.unavailable_nodes.join(', ')}.
            </p>
          {:else if !appsLoading && !apps.apps.length}<p class="caption">No running apps.</p>{/if}
        </div>
        <div class="target-picker">
          <span class="field-label">Destinations</span>
          <div>
            {#each configuration.targets as target}<label class="check-label"
                ><input
                  type="checkbox"
                  bind:group={selectedIds}
                  value={target.id}
                  disabled={running || pending !== null}
                />{target.label}</label
              >{/each}
          </div>
        </div>
        <button
          class="primary"
          class:stop={running}
          disabled={!running &&
            (pending !== null ||
              bandwidthProgress !== null ||
              !selectedIds.length ||
              !title.trim() ||
              Boolean(selectedAppKey && !selectedApp))}
          onclick={() => (running ? void recorder?.stop() : start())}
          >{running ? 'Stop' : recording ? 'New recording' : 'Record'}</button
        >
      </div>
      <label class="check-label bandwidth-opt-in"
        ><input
          type="checkbox"
          bind:checked={includeBandwidth}
          disabled={running ||
            bandwidthProgress !== null ||
            pending !== null ||
            !bandwidthTargets.length}
        />Include speed test at start</label
      >
      {#if !recording || running}<p class="caption">Keep this tab open while playing.</p>
      {:else if owned && !saved}<p class="caption">
          Unsaved draft · a new recording replaces it.
        </p>{/if}
      {#if running}<div class="marker-controls">
          <label class="sr-only" for="marker-label">Event description</label><input
            id="marker-label"
            bind:value={markerLabel}
            maxlength="200"
            placeholder="Describe the problem"
          /><button onclick={mark}>Mark event</button>
        </div>{/if}
    </section>

    <section class="panel" aria-label="Speed test">
      <div class="section-heading">
        <h2>Speed test</h2>
        <span class="muted">Browser ↔ server</span>
      </div>
      <div class="bandwidth-controls">
        <label
          >Destination<select bind:value={bandwidthTargetId} disabled={bandwidthProgress !== null}>
            {#each bandwidthTargets as target}<option value={target.id}>{target.label}</option
              >{/each}
          </select></label
        >
        <label
          >Data cap / direction<select
            bind:value={bandwidthSize}
            disabled={bandwidthProgress !== null}
          >
            {#each configuration.bandwidth_sizes as size}<option value={size}
                >{dataSize(size)} · ~{bandwidthSpeedGuide(
                  size,
                  bandwidthDuration ?? configuration.bandwidth_limits.transfer_duration_ms,
                )} Mbps</option
              >{/each}
          </select></label
        >
        <label
          >Time cap / direction<select
            bind:value={bandwidthDuration}
            disabled={bandwidthProgress !== null}
          >
            {#each configuration.bandwidth_durations as duration}<option value={duration}
                >{duration / 1000} s</option
              >{/each}
          </select></label
        >
        <button
          disabled={!bandwidthProgress &&
            (!bandwidthTargetId ||
              bandwidthSize === null ||
              bandwidthDuration === null ||
              pending !== null)}
          onclick={() =>
            bandwidthProgress ? bandwidthAbort?.abort() : void speedTest(running ? recorder : null)}
          >{bandwidthProgress ? 'Cancel test' : 'Run speed test'}</button
        >
      </div>
      <p class="caption">
        ~{Math.ceil(
          (configuration.bandwidth_limits.idle_duration_ms + 2 * (bandwidthDuration ?? 0)) / 1000,
        )} s · up to {dataSize(2 * (bandwidthSize ?? 0))} total · loads connection
      </p>
      {#if bandwidthProgress}<p class="caption" role="status">
          {(
            {
              connecting: 'Connecting…',
              idle: 'Measuring idle latency…',
              download: 'Downloading…',
              upload: 'Uploading…',
            } as const
          )[bandwidthProgress.phase]}{#if bandwidthProgress.transferred_bytes}
            {dataSize(bandwidthProgress.transferred_bytes)}{/if}
        </p>{/if}
      {#if bandwidthError}<p class="caption bad" role="alert">{bandwidthError}</p>{/if}
      {#if bandwidthResult}
        <BandwidthResults test={bandwidthResult} />
        <button
          onclick={() =>
            bandwidthResult &&
            exportJson(bandwidthResult, `yukibot-speed-${bandwidthResult.started_at_ms}.json`)}
          >Export test</button
        >
      {/if}
    </section>

    {#if recording}
      <section class="panel">
        <div class="section-heading">
          <h2>Timeline</h2>
          <label class="compact-label"
            ><span class="sr-only">Destination</span>
            <select bind:value={targetId}>
              <option value="">All destinations</option>
              {#each recording.targets as target}<option value={target.id}>{target.label}</option
                >{/each}
            </select>
          </label>
        </div>
        {#if comparison}<div class="comparison-note">
            {comparison.summary.owner_name} · {comparison.summary.title} · aligned by clock time
            <button class="text-button" onclick={() => (comparison = null)}>Remove</button>
          </div>{/if}
        <Timeline recordings={plotted} {targetId} onseek={(offset) => (selectedOffset = offset)} />
        <ProbeSummary {recording} />
        <p class="caption">HTTP probes · failures are not game packet loss.</p>
        {#each recording.bandwidth_tests ?? [] as item}
          <details class="bandwidth-recorded">
            <summary>Speed test · {item.test.target.label} · +{duration(item.offset_ms)}</summary
            ><BandwidthResults test={item.test} />
          </details>
        {/each}
        <details class="individual-probes">
          <summary>Individual probes <span class="muted">· latest 100</span></summary>
          <div class="table-scroll probe-table">
            <table>
              <thead
                ><tr><th>Time</th><th>Destination</th><th>Result</th><th>Browser state</th></tr
                ></thead
              >
              <tbody>
                {#each visibleProbes as probe}<tr>
                    <td>+{duration(probe.offset_ms)}</td>
                    <td
                      >{recording.targets.find((target) => target.id === probe.target_id)?.label ??
                        probe.target_id}</td
                    >
                    <td class:bad={probe.status !== 'ok' && probe.status !== 'browser_delayed'}
                      >{probeLabel(probe)}</td
                    >
                    <td
                      >{probe.hidden ? 'Background' : 'Visible'}{probe.browser_delay_ms > 0
                        ? ` · ${probe.browser_delay_ms} ms late`
                        : ''}</td
                    >
                  </tr>{/each}
              </tbody>
            </table>
          </div>
        </details>
      </section>

      {#if recording.monitored_app}<section class="panel" aria-label="Game process telemetry">
          <ProcessTelemetry {recording} offset={selectedOffset} />
        </section>{/if}
      <section class="panel"><ServerContext {recording} offset={selectedOffset} /></section>

      <section class="panel">
        <div class="section-heading">
          <h2>Events &amp; notes</h2>
          <span class="muted"
            >{owner?.display_name ?? configuration.identity.display_name} · {new Date(
              recording.started_at_ms,
            ).toLocaleString()}</span
          >
        </div>
        {#if recording.markers.length}<div class="event-list">
            {#each recording.markers as marker}<button
                class="event-pill"
                onclick={() => (selectedOffset = marker.offset_ms)}
              >
                <time>+{duration(marker.offset_ms)}</time>{marker.label}</button
              >{/each}
          </div>{/if}
        <label class="notes-field"
          ><span class="sr-only">Session notes</span><textarea
            value={notes}
            maxlength="2000"
            rows="3"
            disabled={!owned || pending !== null}
            placeholder="Notes"
            oninput={(event) => updateDetails('notes', event.currentTarget.value)}
          ></textarea></label
        >
        <div class="actions">
          {#if owned}<button
              class="primary"
              aria-live="polite"
              disabled={running || pending !== null || !title.trim() || saved}
              onclick={() => void save()}
              >{pending === 'save' ? 'Saving…' : saved ? 'Saved' : 'Save report'}</button
            >{/if}
          <button disabled={running} onclick={exportReport}>Export JSON</button>
          <span class="muted">Owner &amp; admins · {configuration.limits.retention_days} days</span>
        </div>
      </section>
    {/if}

    <section class="panel report-library">
      <div class="section-heading">
        <h2>{configuration.identity.is_admin ? 'Player sessions' : 'Saved sessions'}</h2>
        <button disabled={pending !== null} onclick={() => void refreshReports()}>Refresh</button>
      </div>
      {#if reports.length}<div class="table-scroll">
          <table>
            <thead
              ><tr
                ><th>Session</th>{#if configuration.identity.is_admin}<th>Player</th>{/if}<th
                  >Started</th
                ><th>Duration</th><th>Probes</th><th>Review</th></tr
              ></thead
            >
            <tbody>
              {#each reports as report}<tr>
                  <th>{report.title}</th>{#if configuration.identity.is_admin}<td
                      >{report.owner_name}</td
                    >{/if}
                  <td>{new Date(report.started_at_ms).toLocaleString()}</td><td
                    >{duration(report.duration_ms)}</td
                  ><td>{report.probe_count}</td>
                  <td
                    ><div class="row-actions">
                      <button
                        disabled={running || pending !== null}
                        onclick={() => void loadReport(report)}>Open</button
                      >
                      {#if recording}<button
                          disabled={running || pending !== null || report.id === recording.id}
                          onclick={() => void loadReport(report, true)}>Compare</button
                        >{/if}
                    </div></td
                  >
                </tr>{/each}
            </tbody>
          </table>
        </div>{:else}<p class="empty-state small">No saved sessions.</p>{/if}
    </section>

    <details class="panel interpretation">
      <summary>About measurements</summary>
      <dl class="measurement-guide">
        <div>
          <dt>Timeline</dt>
          <dd>
            Dashed lines mark events; missing RTTs stay blank. Browser scheduling delays are
            excluded from latency statistics.
          </dd>
        </div>
        <div>
          <dt>Variation</dt>
          <dd>Mean absolute change between consecutive successful RTTs.</dd>
        </div>
        <div>
          <dt>Processes</dt>
          <dd>
            Totals include child processes. 100% CPU = one logical core. RSS may double-count shared
            memory; disk I/O is OS-reported.
          </dd>
        </div>
        <div>
          <dt>Limits</dt>
          <dd>
            Shared hosts are one reference. These measurements cannot locate ISP faults or establish
            game tick health.
          </dd>
        </div>
      </dl>
    </details>
  {/if}
</AppShell>
