<script lang="ts">
  import { onMount } from 'svelte';
  import AppShell from './lib/AppShell.svelte';
  import Timeline from './lib/Timeline.svelte';
  import ProbeSummary from './lib/ProbeSummary.svelte';
  import ServerContext from './lib/ServerContext.svelte';
  import ProcessTelemetry from './lib/ProcessTelemetry.svelte';
  import { api, ApiError, errorMessage, requireData } from './lib/api';
  import { readDraft, writeDraft } from './lib/drafts';
  import { duration, probeLabel } from './lib/metrics';
  import { Recorder } from './lib/recorder';
  import type {
    AppList,
    MonitoredApp,
    Configuration,
    Identity,
    Recording,
    Report,
    ReportSummary,
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
              text: 'Browser draft restored. Review, save or export.',
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
      if (recorder) void recorder.stop();
    };
  });

  function start(): void {
    if (!configuration || running || pending !== null) return;
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
  }

  async function save(): Promise<void> {
    if (!recording || running || pending !== null || !owned) return;
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
      notice = {
        tone: 'info',
        text: 'Report saved. Visible to you and admins.',
      };
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
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }),
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = `yukibot-network-${recording.id}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
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
    <div>
      <h1>Network diagnostics</h1>
      <p class="intro-text">Record latency. Mark delays. Compare sessions.</p>
    </div>
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
        <div>
          <h2>Recording</h2>
        </div>
        <span class="badge"
          >{Math.round((configuration.limits?.max_duration_ms ?? 0) / 60000)} min limit</span
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
              !selectedIds.length ||
              !title.trim() ||
              Boolean(selectedAppKey && !selectedApp))}
          onclick={() => (running ? void recorder?.stop() : start())}
          >{running ? 'Stop recording' : 'Start recording'}</button
        >
      </div>
      <div class="recording-details">
        <p>Keep this tab open while playing. Background delays are flagged.</p>
        <p>HTTP probes only · no speed test</p>
      </div>
      {#if running}<div class="marker-controls">
          <label class="sr-only" for="marker-label">Event description</label><input
            id="marker-label"
            bind:value={markerLabel}
            maxlength="200"
            placeholder="Describe the problem"
          /><button onclick={mark}>Mark event</button>
        </div>{/if}
      {#if recording && !running}<p class="caption">
          New recordings replace this draft. Save or export to keep it.
        </p>{/if}
    </section>

    <section class="panel" aria-label="Game process telemetry">
      <div class="section-heading">
        <div>
          <h2>Game process</h2>
        </div>
        <button
          disabled={appsLoading || running || pending !== null}
          onclick={() => void refreshApps()}>{appsLoading ? 'Refreshing…' : 'Refresh apps'}</button
        >
      </div>
      <label class="process-picker"
        >{recording && !running ? 'App for next recording' : 'App to monitor'}
        <select bind:value={selectedAppKey} disabled={running || pending !== null}>
          <option value="">Network only</option>
          {#if selectedAppKey && !selectedApp}<option value={selectedAppKey} disabled
              >{recording?.monitored_app?.label ?? 'Selected app'} · unavailable</option
            >{/if}
          {#each apps.apps as app}<option value={appKey(app)}>{app.label} · {app.node_label}</option
            >{/each}
        </select>
      </label>
      {#if appsError}<p class="caption bad">App list unavailable: {appsError}</p>
      {:else if apps.unavailable_nodes.length}<p class="caption">
          App lists unavailable: {apps.unavailable_nodes.join(', ')}.
        </p>
      {:else if !appsLoading && !apps.apps.length}<p class="caption">No running apps.</p>{/if}
      <ProcessTelemetry {recording} offset={selectedOffset} />
    </section>

    {#if recording}
      <section class="panel">
        <div class="section-heading">
          <div>
            <h2>Connection timeline</h2>
          </div>
          <label class="compact-label"
            >Destination<select bind:value={targetId}
              ><option value="">All destinations</option>{#each recording.targets as target}<option
                  value={target.id}>{target.label}</option
                >{/each}</select
            ></label
          >
        </div>
        {#if comparison}<div class="comparison-note">
            {comparison.summary.owner_name} · {comparison.summary.title} · aligned by clock time
            <button class="text-button" onclick={() => (comparison = null)}>Remove</button>
          </div>{/if}
        <Timeline recordings={plotted} {targetId} onseek={(offset) => (selectedOffset = offset)} />
        <ProbeSummary {recording} />
      </section>

      <section class="panel"><ServerContext {recording} offset={selectedOffset} /></section>

      <section class="panel">
        <div class="section-heading">
          <div>
            <h2>Events &amp; notes</h2>
          </div>
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
                ><time>+{duration(marker.offset_ms)}</time>{marker.label}</button
              >{/each}
          </div>{/if}
        <details>
          <summary>Individual probes <span class="muted">· latest 100</span></summary>
          <div class="table-scroll probe-table">
            <table>
              <thead
                ><tr><th>Time</th><th>Destination</th><th>Result</th><th>Browser state</th></tr
                ></thead
              ><tbody
                >{#each visibleProbes as probe}<tr
                    ><td>+{duration(probe.offset_ms)}</td><td
                      >{recording.targets.find((target) => target.id === probe.target_id)?.label ??
                        probe.target_id}</td
                    ><td class:bad={probe.status !== 'ok' && probe.status !== 'browser_delayed'}
                      >{probeLabel(probe)}</td
                    ><td
                      >{probe.hidden ? 'Background' : 'Visible'}{probe.browser_delay_ms > 0
                        ? ` · ${probe.browser_delay_ms} ms late`
                        : ''}</td
                    ></tr
                  >{/each}</tbody
              >
            </table>
          </div>
        </details>
        <label class="notes-field"
          >Session notes<textarea
            value={notes}
            maxlength="2000"
            rows="3"
            disabled={!owned || pending !== null}
            placeholder="Symptoms, shared traffic, changes tried…"
            oninput={(event) => updateDetails('notes', event.currentTarget.value)}
          ></textarea></label
        >
        <div class="actions">
          {#if owned}<button
              class="primary"
              disabled={running || pending !== null || !title.trim()}
              onclick={() => void save()}
              >{pending === 'save'
                ? 'Saving…'
                : saved
                  ? 'Save report again'
                  : 'Save report'}</button
            >{/if}<button disabled={running} onclick={exportReport}>Export JSON</button><span
            class="muted">Kept {configuration.limits.retention_days} days · you and admins</span
          >
        </div>
      </section>
    {:else}
      <section class="panel empty-state welcome">
        <h2>No recording</h2>
        <p>Start recording, play, then mark any delays.</p>
        <p class="caption">Drafts stay in this browser during connection outages.</p>
      </section>
    {/if}

    <details class="panel interpretation">
      <summary>Reading results</summary>
      <div class="interpretation-grid">
        <p>
          <strong>One destination slows</strong>Compare browser and Portal probes with other players
          at the same time.
        </p>
        <p>
          <strong>Several destinations slow</strong>Check the shared connection or route. Services
          on one host share failure risks.
        </p>
        <p>
          <strong>Game stalls; HTTP stays healthy</strong>Check game/client processing and game
          traffic. HTTP probes do not measure game packet loss.
        </p>
      </div>
      <p class="caption">
        Configured nodes only. These tests cannot locate line or ISP faults, or prove game traffic
        is healthy. Host and process metrics do not measure game tick health.
      </p>
    </details>

    <section class="panel report-library">
      <div class="section-heading">
        <div>
          <h2>{configuration.identity.is_admin ? 'Player sessions' : 'Your saved sessions'}</h2>
        </div>
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
            ><tbody
              >{#each reports as report}<tr
                  ><th>{report.title}</th>{#if configuration.identity.is_admin}<td
                      >{report.owner_name}</td
                    >{/if}<td>{new Date(report.started_at_ms).toLocaleString()}</td><td
                    >{duration(report.duration_ms)}</td
                  ><td>{report.probe_count}</td><td
                    ><div class="row-actions">
                      <button
                        disabled={running || pending !== null}
                        onclick={() => void loadReport(report)}>Open</button
                      >{#if recording}<button
                          disabled={running || pending !== null || report.id === recording.id}
                          onclick={() => void loadReport(report, true)}>Compare</button
                        >{/if}
                    </div></td
                  ></tr
                >{/each}</tbody
            >
          </table>
        </div>{:else}<p class="empty-state small">No saved sessions.</p>{/if}
    </section>
  {/if}
</AppShell>
