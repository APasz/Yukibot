import type * as Leaflet from 'leaflet';
import L from './leaflet';
import { apiUrl, fetchJson, MAP_REQUEST_TIMEOUT_MS } from './api';
import { isBounds } from './coordinates';
import type { TileIndex, World } from './types';

interface VisibleTile {
  element: HTMLElement;
  coords: Leaflet.Coords;
  revision: string | null;
  pending: HTMLImageElement | null;
  desiredRevision: string | null;
  failed: boolean;
  timer: number | null;
  ready: Leaflet.DoneCallback | null;
}

interface TileArea {
  zoom: number;
  min_x: number;
  max_x: number;
  min_y: number;
  max_y: number;
}

function contains(area: TileArea, coords: { x: number; y: number; z: number }): boolean {
  return (
    coords.z === area.zoom &&
    coords.x >= area.min_x &&
    coords.x <= area.max_x &&
    coords.y >= area.min_y &&
    coords.y <= area.max_y
  );
}

const tileKey = (coords: { x: number; y: number; z: number }) =>
  `${coords.z}/${coords.x}_${coords.y}`;

/** Keep each last good image until its revision's replacement has loaded. */
export class MapTileLayer extends L.GridLayer {
  private readonly visible = new Map<string, VisibleTile>();
  private revisions = new Map<string, string>();
  private readonly request = new AbortController();
  private legacyRevision: string;
  private failedTiles = 0;
  private pendingRequest: AbortController | null = null;
  private lastIndex: { url: string; etag: string | null; data: TileIndex; area: TileArea } | null =
    null;

  constructor(
    private readonly baseUrl: string,
    private readonly world: World,
    private readonly indexed: boolean,
    private readonly onIndex: (index: TileIndex) => void,
    private readonly onHealth: (failed: boolean) => void,
  ) {
    super({
      tileSize: world.tile_size ?? 512,
      minZoom: 0,
      maxNativeZoom: world.zoom.max,
      maxZoom: world.zoom.max + world.zoom.extra,
      noWrap: true,
      updateWhenZooming: false,
      keepBuffer: 2,
    });
    this.legacyRevision = String(
      Math.floor(Date.now() / ((world.tiles_update_interval ?? 15) * 1000)),
    );
    this.on('tileunload', (event: Leaflet.TileEvent) => {
      const key = tileKey(event.coords);
      const tile = this.visible.get(key);
      if (tile?.element === event.tile) {
        this.cancelImage(tile);
        this.setTileFailed(tile, false);
        this.visible.delete(key);
        this.reportHealth();
      }
    });
  }

  override createTile(coords: Leaflet.Coords, done: Leaflet.DoneCallback): HTMLElement {
    const element = document.createElement('div');
    const tile: VisibleTile = {
      element,
      coords,
      revision: null,
      pending: null,
      desiredRevision: null,
      failed: false,
      timer: null,
      ready: done,
    };
    this.visible.set(tileKey(coords), tile);
    this.updateTile(tile);
    if (
      this.lastIndex &&
      contains(this.lastIndex.area, coords) &&
      !this.revisions.has(tileKey(coords))
    ) {
      // Leaflet registers the tile after createTile returns.
      queueMicrotask(() => this.completeTile(tile));
    }
    return element;
  }

  private completeTile(tile: VisibleTile, error?: Error): void {
    if (this.request.signal.aborted || this.visible.get(tileKey(tile.coords)) !== tile) return;
    const done = tile.ready;
    if (!error) tile.ready = null;
    done?.(error, tile.element);
  }

  override onRemove(map: Leaflet.Map): this {
    this.request.abort();
    this.pendingRequest?.abort();
    for (const tile of this.visible.values()) this.cancelImage(tile);
    this.visible.clear();
    this.failedTiles = 0;
    return super.onRemove(map);
  }

  private cancelImage(tile: VisibleTile): void {
    if (tile.timer !== null) {
      window.clearTimeout(tile.timer);
      tile.timer = null;
    }
    if (!tile.pending) return;
    tile.pending.onload = tile.pending.onerror = null;
    tile.pending.removeAttribute('src');
    tile.pending = null;
    tile.desiredRevision = null;
  }

  private reportHealth(): void {
    if (!this.request.signal.aborted) this.onHealth(this.failedTiles > 0);
  }

  private setTileFailed(tile: VisibleTile, failed: boolean): void {
    if (tile.failed === failed) return;
    tile.failed = failed;
    this.failedTiles += failed ? 1 : -1;
  }

  private updateTile(tile: VisibleTile): void {
    const revision = this.indexed ? this.revisions.get(tileKey(tile.coords)) : this.legacyRevision;
    if (!revision || revision === tile.revision || revision === tile.desiredRevision) return;
    this.cancelImage(tile);
    const image = new Image();
    tile.pending = image;
    tile.desiredRevision = revision;
    image.alt = '';
    image.style.width = image.style.height = '100%';
    image.onload = () => {
      if (this.request.signal.aborted || tile.pending !== image) return;
      tile.element.replaceChildren(image);
      tile.revision = revision;
      if (tile.timer !== null) window.clearTimeout(tile.timer);
      tile.timer = null;
      tile.pending = null;
      tile.desiredRevision = null;
      image.onload = image.onerror = null;
      this.setTileFailed(tile, false);
      this.completeTile(tile);
      this.reportHealth();
    };
    image.onerror = () => {
      if (this.request.signal.aborted || tile.pending !== image) return;
      this.cancelImage(tile);
      this.setTileFailed(tile, this.indexed || tile.revision !== null);
      this.completeTile(tile, new Error('Map tile could not be loaded.'));
      this.reportHealth();
    };
    tile.timer = window.setTimeout(() => {
      this.cancelImage(tile);
      this.setTileFailed(tile, this.indexed || tile.revision !== null);
      this.completeTile(tile, new Error('Map tile request timed out.'));
      this.reportHealth();
    }, MAP_REQUEST_TIMEOUT_MS);
    image.src = apiUrl(
      this.baseUrl,
      `/worlds/${encodeURIComponent(this.world.name)}/tiles/${tile.coords.z}/${tile.coords.x}_${tile.coords.y}.png`,
      { revision },
    );
  }

  async refresh(map: Leaflet.Map): Promise<void> {
    if (!this.indexed) {
      this.legacyRevision = String(
        Math.floor(Date.now() / ((this.world.tiles_update_interval ?? 15) * 1000)),
      );
      for (const tile of this.visible.values()) this.updateTile(tile);
      return;
    }
    const zoom = Math.min(this.world.zoom.max, Math.max(0, Math.round(map.getZoom())));
    const tileSize = this.getTileSize();
    const center = map.project(map.getCenter(), zoom);
    const halfSize = map.getSize().multiplyBy(map.getZoomScale(zoom, map.getZoom()) / 2);
    const bounds = L.bounds(center.subtract(halfSize), center.add(halfSize));
    if (!bounds.min || !bounds.max) return;
    const area = {
      zoom,
      min_x: Math.floor(bounds.min.x / tileSize.x) - 2,
      max_x: Math.floor((bounds.max.x - 1) / tileSize.x) + 2,
      min_y: Math.floor(bounds.min.y / tileSize.y) - 2,
      max_y: Math.floor((bounds.max.y - 1) / tileSize.y) + 2,
    };
    const url = apiUrl(
      this.baseUrl,
      `/worlds/${encodeURIComponent(this.world.name)}/tiles/index`,
      area,
    );
    this.pendingRequest?.abort();
    const controller = new AbortController();
    this.pendingRequest = controller;
    try {
      const cached = this.lastIndex?.url === url ? this.lastIndex : null;
      const result = await fetchJson<TileIndex>(url, {
        signal: AbortSignal.any([this.request.signal, controller.signal]),
        headers: cached?.etag ? { 'If-None-Match': cached.etag } : {},
      });
      if (this.request.signal.aborted || this.pendingRequest !== controller) return;
      const index = result?.data ?? cached?.data;
      if (!index) throw new Error('Tile index response is missing.');
      if (result) {
        if (
          index.zoom !== zoom ||
          !Array.isArray(index.tiles) ||
          (index.bounds !== null && !isBounds(index.bounds))
        )
          throw new Error('Tile index is invalid.');
        const revisions = new Map<string, string>();
        for (const tile of index.tiles) {
          if (
            !tile ||
            !Number.isInteger(tile.x) ||
            !Number.isInteger(tile.y) ||
            typeof tile.revision !== 'string' ||
            !tile.revision ||
            !contains(area, { ...tile, z: zoom })
          )
            throw new Error('Tile index is invalid.');
          const key = tileKey({ ...tile, z: zoom });
          if (revisions.has(key)) throw new Error('Tile index contains duplicate tiles.');
          revisions.set(key, tile.revision);
        }
        // Commit only fully validated responses; a bad response must not poison revalidation.
        this.lastIndex = { url, etag: result.etag, data: index, area };
        this.revisions = revisions;
      }
      for (const tile of this.visible.values()) {
        if (!contains(area, tile.coords)) continue;
        if (!this.revisions.has(tileKey(tile.coords))) {
          this.cancelImage(tile);
          tile.element.replaceChildren();
          tile.revision = null;
          this.setTileFailed(tile, false);
          this.completeTile(tile);
        } else this.updateTile(tile);
      }
      this.onIndex(index);
      this.reportHealth();
    } catch (error) {
      if (!controller.signal.aborted && !this.request.signal.aborted) throw error;
    } finally {
      if (this.pendingRequest === controller) this.pendingRequest = null;
    }
  }
}
