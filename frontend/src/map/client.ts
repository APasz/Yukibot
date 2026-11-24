import type * as Leaflet from 'leaflet';
import L, { MAP_PANES } from './leaflet';
import { apiUrl, fetchJson, LatestTask } from './api';
import {
  formatCoordinates,
  isBounds,
  isCoordinate,
  readView,
  snapLinePoint,
  toLatLng,
  toPoint,
  writeView,
} from './coordinates';
import { markerLayer } from './markers';
import type { MarkerGroup } from './markers';
import { MapTileLayer } from './tiles';
import type {
  Annotation,
  AnnotationDraft,
  AnnotationList,
  Coordinate,
  Manifest,
  MapConfig,
  Player,
  PlayerResponse,
  Tone,
  Tool,
  World,
  WorldSettings,
} from './types';

const instances = new Map<string, MapController>();
const playerName = (player: Player) => player.display_name || player.name;
const playerKey = (player: Player, index: number) =>
  player.uuid ?? `${playerName(player)}:${index}`;
const escaped = (value: string) =>
  value.replace(
    /[&<>"']/g,
    (char) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char] ?? char,
  );
const updateMessage = (element: HTMLElement, text: string, tone: Tone) => {
  if (element.textContent !== text) element.textContent = text;
  if (element.dataset.tone !== tone) element.dataset.tone = tone;
};
const readinessMessages: Record<Manifest['readiness'], string> = {
  ready: 'Showing shared explored terrain. Unexplored areas remain blank.',
  disabled: 'Map Rendering is disabled. Enable it in Settings, then restart the game server.',
  restart_required:
    'Restart the game server to apply Map Rendering. Then join and explore to generate tiles.',
  waiting_for_tiles:
    'Waiting for explored terrain. Start or restart the game server with Map Rendering enabled, then join and explore. This page checks automatically.',
};

interface PlayerMarker {
  player: Player;
  marker: Leaflet.CircleMarker;
}
interface Draft {
  worldName: string;
  revision: number;
  shape: AnnotationDraft['shape'];
  points: Coordinate[];
  phase: 'drawing' | 'labeling' | 'saving';
}

function required<T extends HTMLElement>(id: string, elementType: { new (): T }): T {
  const element = document.getElementById(id);
  if (!(element instanceof elementType)) throw new Error(`Map element is missing: ${id}`);
  return element;
}

export class MapController {
  readonly container: HTMLElement;
  readonly map: Leaflet.Map;
  private readonly lifetime = new AbortController();
  private worldLifetime = new AbortController();
  private readonly metadataTask = new LatestTask();
  private readonly playerTask = new LatestTask();
  private readonly annotationTask = new LatestTask();
  private readonly markerTask = new LatestTask();
  private readonly status: HTMLElement;
  private readonly notice: HTMLElement;
  private readonly coordinates: HTMLElement;
  private readonly mode: HTMLElement;
  private readonly worldSelect: HTMLSelectElement;
  private readonly playersEl: HTMLElement;
  private readonly prompt: HTMLElement;
  private readonly labelInput: HTMLInputElement;
  private readonly labelSubmit: HTMLButtonElement;
  private readonly worldLayers = L.layerGroup();
  private readonly playersLayer = L.layerGroup();
  private readonly annotationsLayer = L.layerGroup();
  private readonly previewLayer = L.layerGroup();
  private readonly sourceRenderer =
    L.canvas({ pane: MAP_PANES.source.name }) ?? L.svg({ pane: MAP_PANES.source.name });
  private readonly playerMarkers = new Map<string, PlayerMarker>();
  private readonly resize: ResizeObserver;
  private readonly timer: number;
  private tileLayer: MapTileLayer | null = null;
  private manifest: Manifest | null = null;
  private world: World | null = null;
  private revision = 0;
  private tool: Tool = 'pan';
  private draft: Draft | null = null;
  private preview: Leaflet.Polyline | Leaflet.CircleMarker | null = null;
  private annotationSignature: string | null = null;
  private annotationRequest = 0;
  private markerSignature: string | null = null;
  private playerSignature: string | null = null;
  private playersExpireAt: number | null = null;
  private pendingFocus: string | null = null;
  private zooming = false;
  private lastMetadata = -Infinity;
  private lastPlayers = -Infinity;
  private lastAnnotations = -Infinity;
  private lastTiles = -Infinity;
  private tileTimer: number | null = null;
  private viewTimer: number | null = null;
  private destroyed = false;
  private source: 'live' | 'stale' = 'live';
  private terrainFailed = false;
  private pendingFit = false;
  private readonly errorReports = new Map<string, number>();

  constructor(readonly config: MapConfig) {
    this.container = required(config.containerId, HTMLElement);
    this.status = required(config.statusId, HTMLElement);
    this.notice = required(config.noticeId, HTMLElement);
    this.coordinates = required(config.coordinatesId, HTMLElement);
    this.mode = required(config.modeId, HTMLElement);
    this.worldSelect = required(config.worldSelectId, HTMLSelectElement);
    this.playersEl = required(config.playersId, HTMLElement);
    this.prompt = required(config.promptId, HTMLElement);
    this.labelInput = required(config.promptInputId, HTMLInputElement);
    this.labelSubmit = required(config.promptSubmitId, HTMLButtonElement);
    this.map = L.map(required(config.canvasId, HTMLElement), {
      crs: L.CRS.Simple,
      center: [0, 0],
      zoom: 0,
      minZoom: 0,
      maxZoom: 8,
      attributionControl: false,
      doubleClickZoom: false,
      preferCanvas: false,
    });
    for (const pane of Object.values(MAP_PANES)) {
      this.map.createPane(pane.name).style.zIndex = String(pane.zIndex);
    }
    this.worldLayers.addTo(this.map);
    this.playersLayer.addTo(this.map);
    this.annotationsLayer.addTo(this.map);
    this.previewLayer.addTo(this.map);
    this.bindControls();
    this.resize = new ResizeObserver(() => {
      if (this.visible) {
        this.map.invalidateSize({ animate: false });
        if (this.pendingFit) this.fitExplored();
      }
    });
    this.resize.observe(this.map.getContainer());
    this.timer = window.setInterval(() => this.tick(), 1000);
    this.setTool('pan');
    void this.metadataTask
      .run(() => this.bootstrap())
      .catch((error) => this.failure(error, 'bootstrap'));
  }

  private get visible(): boolean {
    return (
      !this.destroyed &&
      document.visibilityState === 'visible' &&
      this.container.offsetParent !== null
    );
  }

  private get signal(): AbortSignal {
    return AbortSignal.any([this.lifetime.signal, this.worldLifetime.signal]);
  }
  private isCurrent(revision: number): boolean {
    return !this.destroyed && revision === this.revision;
  }
  private url(suffix: string): string {
    return apiUrl(this.config.mapApiUrl, suffix);
  }
  private message(text: string, tone: Tone = 'info'): void {
    if (this.destroyed) return;
    updateMessage(this.status, text, tone);
  }
  private setNotice(text: string, tone: Tone = 'info'): void {
    if (this.destroyed) return;
    updateMessage(this.notice, text, tone);
  }

  private updateNotice(): void {
    if (this.manifest && this.manifest.readiness !== 'ready') {
      this.setNotice(readinessMessages[this.manifest.readiness], 'warning');
    } else if (this.terrainFailed) {
      this.setNotice(
        'Some terrain tiles could not be updated. Keeping previously loaded terrain; retrying automatically.',
        'warning',
      );
    } else if (this.source === 'stale') {
      this.setNotice(
        'Map source is offline. Showing cached metadata and the last loaded terrain.',
        'warning',
      );
    } else {
      this.setNotice(
        this.manifest?.provider === 'sevendays'
          ? readinessMessages.ready
          : 'Squaremap is live. Shared dashboard annotations are available.',
        'success',
      );
    }
  }

  private failure(error: unknown, context: string, revision = this.revision): void {
    if (!this.isCurrent(revision) || this.signal.aborted) return;
    const message = error instanceof Error ? error.message : String(error);
    this.message(message, 'error');
    const key = `${context}:${message}`;
    const now = performance.now();
    if (now - (this.errorReports.get(key) ?? -Infinity) < 30000) return;
    if (this.errorReports.size >= 32) this.errorReports.clear();
    this.errorReports.set(key, now);
    console.error('[mod-map]', context, error);
    void fetch(this.config.clientErrorUrl, {
      method: 'POST',
      keepalive: true,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        context,
        message,
        stack: error instanceof Error ? error.stack : null,
        page_path: window.location.pathname,
        app_name: this.config.appName,
        node_name: this.config.nodeName,
        map_api_url: this.config.mapApiUrl,
        public_map_url: this.config.publicMapUrl,
      }),
    }).catch(() => {});
  }

  private tick(force = false, refreshMetadata = true): void {
    if (!this.container.isConnected) {
      this.destroy();
      return;
    }
    if (this.playersExpireAt !== null && performance.now() >= this.playersExpireAt)
      this.clearPlayers();
    if (!this.visible) return;
    if (this.pendingFit) this.fitExplored();
    const now = performance.now();
    if (
      refreshMetadata &&
      (force || (this.manifest?.provider !== 'squaremap' && now - this.lastMetadata >= 15000))
    ) {
      this.lastMetadata = now;
      void this.metadataTask
        .run(() => this.bootstrap())
        .catch((error) => this.failure(error, 'metadata'));
    }
    if (!this.world) return;
    const revision = this.revision;
    if (force || now - this.lastPlayers >= (this.world.marker_update_interval ?? 5) * 1000) {
      this.lastPlayers = now;
      void this.playerTask
        .run(() => this.refreshPlayers(), force)
        .catch((error) => this.failure(error, 'players', revision));
    }
    if (force || now - this.lastAnnotations >= 15000) {
      this.lastAnnotations = now;
      void this.annotationTask
        .run(() => this.refreshAnnotations(), force)
        .catch((error) => this.failure(error, 'annotations', revision));
    }
    if (force || now - this.lastTiles >= (this.world.tiles_update_interval ?? 15) * 1000) {
      this.lastTiles = now;
      this.scheduleTiles();
      if (this.manifest?.provider === 'squaremap') {
        void this.markerTask
          .run(() => this.refreshMarkers(), force)
          .catch((error) => this.failure(error, 'markers', revision));
      }
    }
  }

  private async bootstrap(worldName = this.worldSelect.value): Promise<void> {
    const result = await fetchJson<Manifest>(this.url('/manifest'), {
      signal: this.lifetime.signal,
    });
    if (!result || this.destroyed) return;
    const manifest = result.data;
    // Older Squaremap nodes predate these manifest fields.
    manifest.readiness ??= 'ready';
    manifest.provider ??= 'squaremap';
    if (manifest.provider !== 'sevendays' && manifest.provider !== 'squaremap')
      throw new Error('Map provider is invalid.');
    if (
      !Object.hasOwn(readinessMessages, manifest.readiness) ||
      !Array.isArray(manifest.worlds) ||
      (manifest.supports_tile_index !== undefined &&
        typeof manifest.supports_tile_index !== 'boolean')
    )
      throw new Error('Map manifest is invalid.');
    this.lastMetadata = performance.now();
    if (manifest.readiness !== 'ready') {
      this.manifest = manifest;
      this.clearWorld();
      this.worldSelect.replaceChildren();
      delete this.worldSelect.dataset.signature;
      this.worldSelect.disabled = true;
      this.setNotice(readinessMessages[manifest.readiness], 'warning');
      this.message('Checking map readiness automatically.');
      this.mode.textContent = 'Map not ready';
      return;
    }
    const summary =
      manifest.worlds.find((world) => world.name === worldName) ??
      manifest.worlds.find((world) => world.name === manifest.initial_world_name) ??
      manifest.worlds[0];
    if (!summary) throw new Error('Map manifest contains no worlds.');
    const settings = await fetchJson<WorldSettings>(
      this.url(`/worlds/${encodeURIComponent(summary.name)}/settings`),
      { signal: this.lifetime.signal },
    );
    if (!settings || this.destroyed) return;
    const data = settings.data;
    if (
      !data.zoom ||
      !Number.isInteger(data.zoom.max) ||
      !Number.isInteger(data.zoom.def) ||
      !Number.isInteger(data.zoom.extra) ||
      data.zoom.max < 0 ||
      data.zoom.max > 16 ||
      data.zoom.extra < 0 ||
      data.zoom.max + data.zoom.extra > 24 ||
      data.zoom.def < 0 ||
      data.zoom.def > data.zoom.max + data.zoom.extra ||
      !isCoordinate(data.spawn) ||
      (data.bounds != null && !isBounds(data.bounds)) ||
      (data.tile_size !== undefined &&
        (!Number.isInteger(data.tile_size) || data.tile_size < 16 || data.tile_size > 2048)) ||
      (data.north_positive !== undefined && typeof data.north_positive !== 'boolean') ||
      (data.player_tracker !== undefined &&
        (typeof data.player_tracker?.enabled !== 'boolean' ||
          (data.player_tracker.max_age_seconds !== undefined &&
            (!Number.isFinite(data.player_tracker.max_age_seconds) ||
              data.player_tracker.max_age_seconds < 0)))) ||
      [data.marker_update_interval, data.tiles_update_interval].some(
        (interval) => interval !== undefined && (!Number.isFinite(interval) || interval <= 0),
      )
    )
      throw new Error('Map world settings are invalid.');
    const world: World = {
      ...summary,
      ...data,
      scale: 1 / 2 ** data.zoom.max,
      bounds: data.bounds,
      player_tracker: data.player_tracker,
      marker_update_interval: data.marker_update_interval,
      tiles_update_interval: data.tiles_update_interval,
    };
    const changed =
      !this.world ||
      this.manifest?.provider !== manifest.provider ||
      (this.manifest?.supports_tile_index ?? false) !== (manifest.supports_tile_index ?? false) ||
      this.world.name !== world.name ||
      this.world.zoom.max !== world.zoom.max ||
      this.world.zoom.extra !== world.zoom.extra ||
      this.world.tile_size !== world.tile_size ||
      this.world.north_positive !== world.north_positive;
    const trackingChanged = this.world?.player_tracker?.enabled !== world.player_tracker?.enabled;
    this.manifest = manifest;
    const signature = JSON.stringify(manifest.worlds);
    if (this.worldSelect.dataset.signature !== signature) {
      this.worldSelect.replaceChildren(
        ...manifest.worlds.map((world) => new Option(world.display_name, world.name)),
      );
      this.worldSelect.dataset.signature = signature;
    }
    this.worldSelect.disabled = manifest.worlds.length < 2;
    if (changed) {
      this.clearWorld();
      this.world = world;
      this.map.setMaxZoom(world.zoom.max + world.zoom.extra);
      this.map.setMinZoom(0);
      this.tileLayer = new MapTileLayer(
        this.config.mapApiUrl,
        world,
        manifest.supports_tile_index ?? false,
        (index) => {
          if (this.world === world) this.world.bounds = index.bounds;
        },
        (failed) => {
          if (this.world !== world) return;
          this.terrainFailed = failed;
          this.updateNotice();
        },
      ).addTo(this.map);

      const saved = readView(this.viewKey(), world.zoom.max + world.zoom.extra);
      if (saved) this.map.setView(toLatLng(world, saved), saved.zoom, { animate: false });
      else if (world.bounds) this.fitExplored();
      else this.map.setView(toLatLng(world, world.spawn), world.zoom.def, { animate: false });
      this.setTool('pan');
    } else if (this.world) Object.assign(this.world, world);
    if (trackingChanged) {
      this.lastPlayers = -Infinity;
      if (!world.player_tracker?.enabled) this.clearPlayers();
    }
    this.worldSelect.value = world.name;
    this.source = result.source === 'stale' || settings.source === 'stale' ? 'stale' : 'live';
    this.updateNotice();
    this.coordinatesAt();
    if (changed) {
      this.lastPlayers = this.lastAnnotations = this.lastTiles = -Infinity;
      this.tick(true, false);
      this.message(`Loaded ${world.display_name}.`, 'success');
    } else if (trackingChanged) this.tick(false, false);
  }

  private clearWorld(): void {
    this.saveView();
    this.worldLifetime.abort();
    this.worldLifetime = new AbortController();
    this.revision++;
    this.tileLayer?.remove();
    this.tileLayer = null;
    this.worldLayers.clearLayers();
    this.annotationsLayer.clearLayers();
    this.clearPlayers();
    this.annotationSignature = this.markerSignature = null;
    this.terrainFailed = false;
    this.pendingFit = false;
    this.draft = null;
    this.labelInput.disabled = this.labelSubmit.disabled = false;
    this.cancelDraft();
    this.world = null;
    this.playersEl.hidden = true;
    this.coordinatesAt();
    this.updateControls();
  }

  private clearPlayers(): void {
    this.playersLayer.clearLayers();
    this.playerMarkers.clear();
    this.playersExpireAt = null;
    this.pendingFocus = null;
    this.updatePlayerList([]);
  }

  private async refreshPlayers(): Promise<void> {
    const world = this.world;
    if (!world?.player_tracker?.enabled) {
      this.clearPlayers();
      return;
    }
    const revision = this.revision;
    const requestedAt = performance.now();
    try {
      const result = await fetchJson<Player[] | PlayerResponse>(this.url('/players'), {
        signal: this.signal,
      });
      if (!result || !this.isCurrent(revision) || !world.player_tracker?.enabled) return;
      const payload = result.data;
      const players = Array.isArray(payload) ? payload : payload?.players;
      if (!Array.isArray(players)) throw new Error('Player positions are invalid.');
      const maxAge = Array.isArray(payload)
        ? world.player_tracker.max_age_seconds
        : (payload.max_age_seconds ?? world.player_tracker.max_age_seconds);
      if (maxAge !== undefined && (!Number.isFinite(maxAge) || maxAge < 0))
        throw new Error('Player position freshness is invalid.');
      const expires = maxAge === undefined ? null : requestedAt + maxAge * 1000;
      if (result.source === 'stale' || (expires !== null && performance.now() >= expires)) {
        this.clearPlayers();
        return;
      }
      const visible = players.filter(
        (player) =>
          player &&
          player.world === world.name &&
          typeof playerName(player) === 'string' &&
          isCoordinate(player),
      );
      const keys = new Set<string>();
      visible.forEach((player, index) => {
        const key = playerKey(player, index);
        keys.add(key);
        let entry = this.playerMarkers.get(key);
        if (!entry) {
          const marker = L.circleMarker(toLatLng(world, player), {
            radius: 6,
            color: '#0f172a',
            fillColor: '#22c55e',
            fillOpacity: 0.95,
            weight: 2,
            bubblingMouseEvents: false,
            pane: MAP_PANES.players.name,
          }).addTo(this.playersLayer);
          entry = { player, marker };
          this.playerMarkers.set(key, entry);
          marker.bindTooltip(escaped(playerName(player)), {
            permanent: this.manifest?.provider === 'sevendays',
            direction: 'top',
            className: 'mod-map-player-label',
          });
          marker.on('click', () => this.focusPlayer(key));
        } else {
          if (entry.player.x !== player.x || entry.player.z !== player.z)
            entry.marker.setLatLng(toLatLng(world, player));
          if (playerName(entry.player) !== playerName(player))
            entry.marker.setTooltipContent(escaped(playerName(player)));
          entry.player = player;
        }
      });
      for (const [key, entry] of this.playerMarkers)
        if (!keys.has(key)) {
          this.playersLayer.removeLayer(entry.marker);
          this.playerMarkers.delete(key);
        }
      this.playersExpireAt = expires;
      this.updatePlayerList(visible);
    } catch (error) {
      if (this.isCurrent(revision)) this.clearPlayers();
      throw error;
    }
  }

  private updatePlayerList(players: Player[]): void {
    this.playersEl.hidden = !this.world?.player_tracker?.enabled;
    const signature = JSON.stringify(
      players.map((player, index) => [playerKey(player, index), playerName(player)]),
    );
    if (signature === this.playerSignature) return;
    this.playerSignature = signature;
    this.playersEl.replaceChildren();
    if (!players.length) {
      this.playersEl.textContent = 'No live player positions.';
      return;
    }
    const caption = document.createElement('span');
    caption.textContent = 'Players:';
    this.playersEl.append(caption);
    players.forEach((player, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'mod-list-button secondary mod-map-player-button';
      button.textContent = playerName(player);
      const key = playerKey(player, index);
      button.onclick = () => this.focusPlayer(key);
      this.playersEl.append(button);
    });
  }

  private focusPlayer(key: string): void {
    if (this.playersExpireAt !== null && performance.now() >= this.playersExpireAt) {
      this.clearPlayers();
      return;
    }
    if (!this.world || this.destroyed) return;
    if (this.zooming) {
      this.pendingFocus = key;
      return;
    }
    const player = this.playerMarkers.get(key)?.player;
    if (player) {
      this.map.panTo(toLatLng(this.world, player));
      this.message(`Centred on ${playerName(player)}.`, 'success');
    }
  }

  private async refreshAnnotations(): Promise<void> {
    const revision = this.revision;
    const world = this.world;
    if (!world) return;
    const request = ++this.annotationRequest;
    const result = await fetchJson<AnnotationList>(this.url('/annotations'), {
      signal: this.signal,
    });
    if (!result || !this.isCurrent(revision) || request !== this.annotationRequest) return;
    if (!Array.isArray(result.data.annotations)) throw new Error('Map annotations are invalid.');
    const annotations = result.data.annotations.filter(
      (annotation) => annotation.world_name === world.name,
    );
    const signature = JSON.stringify(annotations);
    if (signature === this.annotationSignature) return;
    const layers = annotations.map((annotation) => this.annotationLayer(world, annotation));
    this.annotationsLayer.clearLayers();
    layers.forEach((layer) => layer.addTo(this.annotationsLayer));
    this.annotationSignature = signature;
  }

  private refreshAnnotationsAfterMutation(revision: number): void {
    void this.refreshAnnotations().catch((error) => this.failure(error, 'annotations', revision));
  }

  private annotationLayer(world: World, annotation: Annotation): Leaflet.Path {
    const style = {
      color: annotation.color_hex,
      fillColor: annotation.color_hex,
      weight: 2,
      fillOpacity: 0.95,
      pane: MAP_PANES.annotations.name,
      bubblingMouseEvents: false,
    };
    const point = annotation.points[0];
    if (!point) throw new Error('Annotation has no points.');
    const layer =
      annotation.shape === 'marker'
        ? L.circleMarker(toLatLng(world, point), { ...style, radius: 7 })
        : L.polyline(
            annotation.points.map((point) => toLatLng(world, point)),
            { ...style, weight: 4, dashArray: '12 8' },
          );
    const content = document.createElement('div');
    content.className = 'mod-map-annotation-popup';
    const label = document.createElement('strong');
    label.textContent = annotation.label;
    content.append(label);
    if (annotation.created_by_name) {
      const author = document.createElement('div');
      author.className = 'mod-map-annotation-meta';
      author.textContent = `By ${annotation.created_by_name}`;
      content.append(author);
    }
    if (this.config.canWrite) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = 'Delete';
      remove.onclick = () => void this.deleteAnnotation(annotation.annotation_id);
      content.append(remove);
    }
    layer.bindPopup(content);
    layer.bindTooltip(escaped(annotation.label), {
      permanent: annotation.shape === 'marker',
      sticky: annotation.shape !== 'marker',
      direction: 'top',
      className: 'mod-map-annotation-label',
      offset: annotation.shape === 'marker' ? [0, -8] : [0, 0],
    });
    return layer;
  }

  private async refreshMarkers(): Promise<void> {
    const world = this.world;
    const revision = this.revision;
    if (!world || this.manifest?.provider !== 'squaremap') return;
    const result = await fetchJson<MarkerGroup[]>(
      this.url(`/worlds/${encodeURIComponent(world.name)}/markers`),
      { signal: this.signal },
    );
    if (!result || !this.isCurrent(revision)) return;
    if (!Array.isArray(result.data)) throw new Error('Map markers are invalid.');
    this.source = result.source;
    this.updateNotice();
    const signature = JSON.stringify(result.data);
    if (signature === this.markerSignature) return;
    const next = L.layerGroup();
    for (const group of result.data) {
      if (group.hide || !group.markers) continue;
      for (const marker of Object.values(group.markers)) {
        const layer = markerLayer(world, marker, this.config.mapApiUrl, this.sourceRenderer);
        if (!layer) continue;
        if (marker.tooltip)
          layer.bindTooltip(marker.tooltip, {
            sticky: true,
            direction: marker.tooltip_direction ?? 'top',
          });
        if (marker.popup) layer.bindPopup(marker.popup);
        layer.addTo(next);
      }
    }
    this.worldLayers.clearLayers();
    next.addTo(this.worldLayers);
    this.markerSignature = signature;
  }

  private scheduleTiles(): void {
    if (this.tileTimer !== null) window.clearTimeout(this.tileTimer);
    const revision = this.revision;
    this.tileTimer = window.setTimeout(() => {
      this.tileTimer = null;
      if (!this.visible || !this.world || !this.isCurrent(revision)) return;
      if (this.tileLayer)
        void this.tileLayer.refresh(this.map).catch((error) => {
          if (!this.isCurrent(revision)) return;
          this.terrainFailed = true;
          this.updateNotice();
          this.failure(error, 'terrain', revision);
        });
    }, 120);
  }

  private viewKey(): string {
    return `yukibot-map-view:${this.config.nodeName}:${this.config.appName}:${this.world?.name}`;
  }
  private saveView(): void {
    if (this.world && !this.pendingFit)
      writeView(this.viewKey(), {
        ...toPoint(this.world, this.map.getCenter()),
        zoom: this.map.getZoom(),
      });
  }
  private fitExplored(): void {
    const world = this.world;
    if (!world) return;
    const size = this.map.getSize();
    this.pendingFit = !this.visible || size.x <= 0 || size.y <= 0;
    if (this.pendingFit) return;
    if (world.bounds)
      this.map.fitBounds(
        world.bounds.map((point) => toLatLng(world, point)) as Leaflet.LatLngBoundsLiteral,
        { padding: [24, 24], maxZoom: world.zoom.max, animate: false },
      );
    else this.map.setView(toLatLng(world, world.spawn), world.zoom.def);
  }
  private coordinatesAt(latlng?: Leaflet.LatLng): void {
    if (!this.world) {
      this.coordinates.textContent = 'Centre: —';
      return;
    }
    const text = `${latlng ? 'Cursor' : 'Centre'}: ${formatCoordinates(this.world, toPoint(this.world, latlng ?? this.map.getCenter()))}`;
    if (this.coordinates.textContent !== text) this.coordinates.textContent = text;
  }

  private setTool(tool: Tool): void {
    this.tool = tool;
    this.mode.textContent =
      tool === 'line'
        ? `Line tool${this.draft ? `: ${this.draft.points.length} points` : ''}`
        : tool === 'marker'
          ? 'Point tool'
          : 'Pan mode';
    for (const [name, id] of [
      ['marker', this.config.markerButtonId],
      ['line', this.config.lineButtonId],
    ] as const) {
      const button = document.getElementById(id);
      button?.classList.toggle('mod-map-button-active', name === tool);
      button?.setAttribute('aria-pressed', String(name === tool));
    }
    const snap = document.getElementById(this.config.snapToggleId);
    if (snap instanceof HTMLInputElement) {
      snap.disabled = tool !== 'line';
      snap
        .closest<HTMLElement>('.mod-map-toggle')
        ?.setAttribute('data-disabled', String(snap.disabled));
    }
    this.updateControls();
  }

  private action(name: string): HTMLButtonElement | null {
    return this.container.querySelector(`[data-map-action="${name}"]`);
  }
  private updateControls(): void {
    const line = this.draft?.shape === 'polyline' && this.draft.phase === 'drawing';
    const controls = this.container.querySelector<HTMLElement>('.mod-map-draft-actions');
    if (controls) controls.hidden = this.tool !== 'line' && this.tool !== 'marker';
    const count = this.draft?.points.length ?? 0;
    const finish = this.action('finish');
    if (finish) {
      finish.hidden = this.tool !== 'line';
      finish.disabled = !line || count < 2;
    }
    const undo = this.action('undo');
    if (undo) {
      undo.hidden = this.tool !== 'line';
      undo.disabled = !line || count === 0;
    }
    for (const name of ['fit', 'go']) {
      const button = this.action(name);
      if (button) button.disabled = this.world === null;
    }
    for (const id of [this.config.markerButtonId, this.config.lineButtonId]) {
      const button = document.getElementById(id);
      if (button instanceof HTMLButtonElement)
        button.disabled = this.world === null || this.draft?.phase === 'saving';
    }
    const saving = this.draft?.phase === 'saving';
    for (const name of ['cancel', 'cancel-label']) {
      const button = this.action(name);
      if (button) button.disabled = saving;
    }
    this.labelSubmit.textContent = saving ? 'Saving…' : 'Save';
    this.prompt.setAttribute('aria-busy', String(saving));
  }

  private cancelDraft(): void {
    if (this.draft?.phase === 'saving') return;
    this.draft = null;
    this.previewLayer.clearLayers();
    this.preview = null;
    this.prompt.hidden = true;
    this.labelInput.value = '';
    this.setTool('pan');
  }

  private drawPreview(point?: Coordinate): void {
    const world = this.world;
    const draft = this.draft;
    if (!world || !draft) return;
    const values = point ? [...draft.points, point] : draft.points;
    if (!values.length) {
      if (this.preview) this.previewLayer.removeLayer(this.preview);
      this.preview = null;
      return;
    }
    if (!this.preview)
      this.preview =
        draft.shape === 'marker'
          ? L.circleMarker(toLatLng(world, values[0]!), {
              radius: 8,
              color: this.color(),
              pane: MAP_PANES.preview.name,
            }).addTo(this.previewLayer)
          : L.polyline(
              values.map((point) => toLatLng(world, point)),
              { color: this.color(), weight: 3, dashArray: '8 6', pane: MAP_PANES.preview.name },
            ).addTo(this.previewLayer);
    else if (this.preview instanceof L.Polyline)
      this.preview.setLatLngs(values.map((point) => toLatLng(world, point)));
    this.preview.setStyle({ color: this.color() });
  }

  private color(): string {
    const input = document.getElementById(this.config.colorInputId);
    return input instanceof HTMLInputElement ? input.value.toUpperCase() : '#22C55E';
  }

  private labelDraft(): void {
    const draft = this.draft;
    if (
      !draft ||
      draft.phase === 'saving' ||
      (draft.shape === 'polyline' && draft.points.length < 2)
    )
      return;
    draft.phase = 'labeling';
    required(this.config.promptTitleId, HTMLElement).textContent =
      `Label ${draft.shape === 'marker' ? 'point' : 'line'}`;
    this.prompt.hidden = false;
    this.labelSubmit.disabled = false;
    this.labelInput.focus();
    this.updateControls();
  }

  private async saveDraft(): Promise<void> {
    const draft = this.draft;
    if (!draft || draft.phase === 'saving' || !this.isCurrent(draft.revision)) return;
    const label = this.labelInput.value.trim();
    if (!label || label.length > this.labelInput.maxLength) {
      this.message(`Enter a label of 1–${this.labelInput.maxLength} characters.`, 'error');
      this.labelInput.focus();
      return;
    }
    draft.phase = 'saving';
    this.labelSubmit.disabled = true;
    this.labelInput.disabled = true;
    this.updateControls();
    try {
      await fetchJson(this.url('/annotations'), {
        method: 'POST',
        signal: this.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          world_name: draft.worldName,
          shape: draft.shape,
          label,
          color_hex: this.color(),
          points: draft.points,
        } satisfies AnnotationDraft),
      });
      if (!this.isCurrent(draft.revision)) return;
      draft.phase = 'labeling';
      this.cancelDraft();
      this.message(`Added ${label}.`, 'success');
      this.refreshAnnotationsAfterMutation(draft.revision);
    } catch (error) {
      if (this.isCurrent(draft.revision)) {
        draft.phase = 'labeling';
        this.failure(error, 'save-annotation', draft.revision);
      }
    } finally {
      if (this.isCurrent(draft.revision)) {
        this.labelSubmit.disabled = false;
        this.labelInput.disabled = false;
        this.updateControls();
      }
    }
  }

  async deleteAnnotation(id: string): Promise<void> {
    const revision = this.revision;
    try {
      await fetchJson(this.url(`/annotations/${encodeURIComponent(id)}/delete`), {
        method: 'POST',
        signal: this.signal,
      });
      if (!this.isCurrent(revision)) return;
      this.message('Annotation deleted.', 'success');
      this.refreshAnnotationsAfterMutation(revision);
    } catch (error) {
      this.failure(error, 'delete-annotation', revision);
    }
  }

  private bindControls(): void {
    const options = { signal: this.lifetime.signal };
    this.worldSelect.addEventListener(
      'change',
      () => {
        const name = this.worldSelect.value;
        void this.metadataTask
          .run(() => this.bootstrap(name), true)
          .catch((error) => {
            this.worldSelect.value = this.world?.name ?? '';
            this.failure(error, 'world-selection');
          });
      },
      options,
    );
    required(this.config.refreshButtonId, HTMLButtonElement).addEventListener(
      'click',
      () => {
        this.message('Refreshing map data…');
        const name = this.worldSelect.value;
        void this.metadataTask
          .run(() => this.bootstrap(name), true)
          .then(() => this.tick(true, false))
          .catch((error) => this.failure(error, 'refresh'));
      },
      options,
    );
    for (const [tool, id] of [
      ['marker', this.config.markerButtonId],
      ['line', this.config.lineButtonId],
    ] as const) {
      document.getElementById(id)?.addEventListener(
        'click',
        () => {
          const next = this.tool === tool ? 'pan' : tool;
          this.cancelDraft();
          this.setTool(next);
          if (next === 'line')
            this.message('Click or tap to add points. Finish saves the line; Escape cancels.');
        },
        options,
      );
    }
    required(this.config.promptFormId, HTMLFormElement).addEventListener(
      'submit',
      (event) => {
        event.preventDefault();
        void this.saveDraft();
      },
      options,
    );
    this.action('fit')?.addEventListener('click', () => this.fitExplored(), options);
    this.action('finish')?.addEventListener('click', () => this.labelDraft(), options);
    this.action('undo')?.addEventListener(
      'click',
      () => {
        if (this.draft?.phase !== 'drawing') return;
        this.draft.points.pop();
        this.drawPreview();
        this.setTool('line');
      },
      options,
    );
    for (const name of ['cancel', 'cancel-label'])
      this.action(name)?.addEventListener('click', () => this.cancelDraft(), options);
    const expand = this.action('expand');
    expand?.addEventListener(
      'click',
      () => {
        const expanded = this.container.classList.toggle('mod-map-expanded');
        expand.setAttribute('aria-pressed', String(expanded));
        expand.textContent = expanded ? 'Exit expanded view' : 'Expand map';
        this.map.invalidateSize({ animate: false });
      },
      options,
    );
    const coordinateForm = this.container.querySelector<HTMLFormElement>(
      '.mod-map-coordinate-form',
    );
    coordinateForm?.addEventListener(
      'submit',
      (event) => {
        event.preventDefault();
        if (!this.world) return;
        const form = new FormData(coordinateForm);
        const northText = form.get('north');
        const eastText = form.get('east');
        const north = Number(northText);
        const east = Number(eastText);
        if (
          typeof northText !== 'string' ||
          !northText.trim() ||
          typeof eastText !== 'string' ||
          !eastText.trim() ||
          !isCoordinate({ x: east, z: north })
        ) {
          this.message('Enter valid north/east coordinates.', 'error');
          return;
        }
        this.map.panTo(
          toLatLng(this.world, { x: east, z: this.world.north_positive ? north : -north }),
        );
        this.message(
          `Centred on ${Math.abs(north)} ${north < 0 ? 'S' : 'N'} · ${Math.abs(east)} ${east < 0 ? 'W' : 'E'}.`,
          'success',
        );
      },
      options,
    );
    this.container.addEventListener(
      'keydown',
      (event) => {
        if (event.key !== 'Escape') return;
        event.preventDefault();
        const hadDraft = this.tool !== 'pan';
        this.cancelDraft();
        if (!hadDraft && this.container.classList.contains('mod-map-expanded')) expand?.click();
      },
      options,
    );
    document.addEventListener(
      'visibilitychange',
      () => {
        if (this.visible) this.tick(true);
      },
      options,
    );
    this.map.on('click', (event: Leaflet.LeafletMouseEvent) => {
      if (
        !this.config.canWrite ||
        !this.world ||
        this.tool === 'pan' ||
        this.draft?.phase === 'labeling' ||
        this.draft?.phase === 'saving' ||
        event.originalEvent.detail > 1
      )
        return;
      const point = toPoint(this.world, event.latlng);
      this.draft ??= {
        worldName: this.world.name,
        revision: this.revision,
        shape: this.tool === 'marker' ? 'marker' : 'polyline',
        points: [],
        phase: 'drawing',
      };
      const anchor = this.draft.points.at(-1);
      const snap = document.getElementById(this.config.snapToggleId);
      const nextPoint =
        anchor && snap instanceof HTMLInputElement && snap.checked
          ? snapLinePoint(anchor, point)
          : point;
      if (anchor && anchor.x === nextPoint.x && anchor.z === nextPoint.z) return;
      this.draft.points.push(nextPoint);
      this.drawPreview();
      if (this.tool === 'marker') this.labelDraft();
      else this.setTool('line');
    });
    this.map.on('mousemove', (event: Leaflet.LeafletMouseEvent) => {
      this.coordinatesAt(event.latlng);
      if (!this.world || this.draft?.phase !== 'drawing' || this.draft.shape !== 'polyline') return;
      const anchor = this.draft.points.at(-1);
      if (!anchor) return;
      const raw = toPoint(this.world, event.latlng);
      const snap = document.getElementById(this.config.snapToggleId);
      this.drawPreview(
        snap instanceof HTMLInputElement && snap.checked ? snapLinePoint(anchor, raw) : raw,
      );
    });
    this.map.on('dblclick', () => {
      if (this.tool === 'line') this.labelDraft();
    });
    this.map.on('contextmenu', (event: Leaflet.LeafletMouseEvent) => {
      if (this.tool === 'pan') return;
      event.originalEvent.preventDefault();
      this.cancelDraft();
    });
    this.map.on('zoomstart', () => {
      this.zooming = true;
    });
    this.map.on('zoomend', () => {
      this.zooming = false;
      const key = this.pendingFocus;
      this.pendingFocus = null;
      if (key) this.focusPlayer(key);
    });
    this.map.on('moveend', () => {
      this.coordinatesAt();
      this.scheduleTiles();
      if (this.viewTimer !== null) window.clearTimeout(this.viewTimer);
      this.viewTimer = window.setTimeout(() => {
        this.viewTimer = null;
        this.saveView();
      }, 250);
    });
    this.map.on('mouseout', () => this.coordinatesAt());
  }

  destroy(): void {
    if (this.destroyed) return;
    this.saveView();
    this.destroyed = true;
    this.lifetime.abort();
    this.worldLifetime.abort();
    this.metadataTask.cancel();
    this.playerTask.cancel();
    this.annotationTask.cancel();
    this.markerTask.cancel();
    this.resize.disconnect();
    window.clearInterval(this.timer);
    if (this.tileTimer !== null) window.clearTimeout(this.tileTimer);
    if (this.viewTimer !== null) window.clearTimeout(this.viewTimer);
    this.map.remove();
    if (instances.get(this.config.containerId) === this) instances.delete(this.config.containerId);
  }
}

window.modWebMap = {
  mount(config) {
    if (!document.getElementById(config.containerId)) return;
    instances.get(config.containerId)?.destroy();
    instances.set(config.containerId, new MapController(config));
  },
  async deleteAnnotation(containerId, id) {
    await instances.get(containerId)?.deleteAnnotation(id);
  },
};
window.dispatchEvent(new Event('yukibot-map-ready'));
