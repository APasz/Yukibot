// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import L from './leaflet';
import type * as Leaflet from 'leaflet';
import { MapTileLayer } from './tiles';
import type { TileIndex, World } from './types';

const world: World = {
  name: 'save-one',
  display_name: 'Save one',
  world_type: 'normal',
  order: 0,
  scale: 1 / 16,
  zoom: { max: 4, def: 4, extra: 2 },
  spawn: { x: 0, z: 0 },
  tile_size: 128,
};
let map: Leaflet.Map;
let images: HTMLImageElement[];
let index: TileIndex;
let unchanged = false;
let fail = false;
let requests: { url: string; options: RequestInit }[];

beforeEach(() => {
  images = [];
  requests = [];
  unchanged = fail = false;
  index = { zoom: 4, bounds: null, tiles: [{ x: 0, y: 0, revision: 'one' }] };
  const canvas = document.createElement('div');
  document.body.append(canvas);
  Object.defineProperties(canvas, { clientWidth: { value: 800 }, clientHeight: { value: 600 } });
  map = L.map(canvas, { crs: L.CRS.Simple, zoomAnimation: false, fadeAnimation: false }).setView(
    [0, 0],
    4,
  );
  vi.stubGlobal('Image', function () {
    const image = document.createElement('img');
    images.push(image);
    return image;
  });
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, options: RequestInit) => {
      requests.push({ url, options });
      if (fail) return Promise.reject(new Error('Index unavailable'));
      return Promise.resolve(
        unchanged
          ? new Response(null, { status: 304 })
          : new Response(JSON.stringify(index), { headers: { ETag: '"index-one"' } }),
      );
    }),
  );
});
afterEach(() => {
  map.remove();
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function layer() {
  const health = vi.fn();
  const layer = new MapTileLayer('/map', world, true, vi.fn(), health).addTo(map);
  return { layer, health };
}

it('requests only a bounded viewport index and skips every unexplored image', async () => {
  const { layer: tiles } = layer();
  await tiles.refresh(map);
  expect(requests).toHaveLength(1);
  expect(new URL(requests[0]!.url).pathname).toBe('/map/worlds/save-one/tiles/index');
  expect(images).toHaveLength(1);
  expect(images[0]!.src).toContain('/tiles/4/0_0.png?revision=one');
  expect(new URL(requests[0]!.url).searchParams.get('zoom')).toBe('4');
});

it('does not allocate or download tiles for unchanged indexes', async () => {
  const { layer: tiles } = layer();
  await tiles.refresh(map);
  const image = images[0]!;
  image.dispatchEvent(new Event('load'));
  unchanged = true;
  for (let index = 0; index < 20; index++) await tiles.refresh(map);
  expect(images).toHaveLength(1);
  expect(new Headers(requests.at(-1)!.options.headers).get('If-None-Match')).toBe('"index-one"');
  expect(image.isConnected).toBe(true);
});

it('waits for indexed images before completing Leaflet tile loading', async () => {
  vi.useFakeTimers();
  const { layer: tiles } = layer();
  await tiles.refresh(map);
  const good = images[0]!;
  await vi.advanceTimersByTimeAsync(40);
  expect(tiles.isLoading()).toBe(true);
  expect(good.isConnected).toBe(false);
  good.dispatchEvent(new Event('load'));
  expect(tiles.isLoading()).toBe(false);
  expect(good.isConnected).toBe(true);
  expect(good.parentElement?.classList.contains('leaflet-tile-loaded')).toBe(true);
});

it('makes a tile visible when its initial image fails and a retry succeeds', async () => {
  const { layer: tiles } = layer();
  await tiles.refresh(map);
  images[0]!.dispatchEvent(new Event('error'));
  await tiles.refresh(map);
  const good = images.at(-1)!;
  good.dispatchEvent(new Event('load'));
  expect(good.parentElement?.classList.contains('leaflet-tile-loaded')).toBe(true);
});

it('rejects an invalid index atomically and keeps its last valid revalidation state', async () => {
  const { layer: tiles } = layer();
  await tiles.refresh(map);
  const good = images[0]!;
  good.dispatchEvent(new Event('load'));
  index.tiles = [
    { x: 0, y: 0, revision: 'two' },
    { x: 0.5, y: 0, revision: 'invalid' },
  ];
  await expect(tiles.refresh(map)).rejects.toThrow('Tile index is invalid');
  unchanged = true;
  await tiles.refresh(map);
  expect(images).toHaveLength(1);
  expect(good.isConnected).toBe(true);
  expect(tiles.isLoading()).toBe(false);
});

it('retains the good tile through image failure, timeout, and index failure, then retries', async () => {
  vi.useFakeTimers();
  const { layer: tiles, health } = layer();
  await tiles.refresh(map);
  const good = images[0]!;
  good.dispatchEvent(new Event('load'));
  index.tiles[0]!.revision = 'two';
  await tiles.refresh(map);
  expect(good.isConnected).toBe(true);
  images[1]!.dispatchEvent(new Event('error'));
  expect(good.isConnected).toBe(true);
  expect(health).toHaveBeenLastCalledWith(true);
  await tiles.refresh(map);
  await vi.advanceTimersByTimeAsync(15000);
  expect(good.isConnected).toBe(true);
  fail = true;
  await expect(tiles.refresh(map)).rejects.toThrow('Index unavailable');
  expect(good.isConnected).toBe(true);
  fail = false;
  await tiles.refresh(map);
  const replacement = images.at(-1)!;
  replacement.dispatchEvent(new Event('load'));
  expect(good.isConnected).toBe(false);
  expect(replacement.isConnected).toBe(true);
  expect(health).toHaveBeenLastCalledWith(false);
});

it('drops removed tiles and ignores image completions after the layer is removed', async () => {
  const { layer: tiles } = layer();
  await tiles.refresh(map);
  const image = images[0]!;
  index.tiles = [];
  await tiles.refresh(map);
  image.dispatchEvent(new Event('load'));
  expect(image.isConnected).toBe(false);
  index.tiles = [{ x: 0, y: 0, revision: 'two' }];
  await tiles.refresh(map);
  const late = images.at(-1)!;
  tiles.remove();
  late.dispatchEvent(new Event('load'));
  expect(late.isConnected).toBe(false);
});

it('overzoom uses the native tile level and aborts superseded viewport requests', async () => {
  const { layer: tiles } = layer();
  map.setZoom(6);
  await tiles.refresh(map);
  expect(new URL(requests.at(-1)!.url).searchParams.get('zoom')).toBe('4');
  let oldSignal!: AbortSignal;
  vi.stubGlobal(
    'fetch',
    vi.fn(
      (_url: string, options: RequestInit) =>
        new Promise((_resolve, reject) => {
          oldSignal = options.signal!;
          oldSignal.addEventListener('abort', () => reject(oldSignal.reason));
        }),
    ),
  );
  const pending = tiles.refresh(map);
  tiles.remove();
  await pending;
  expect(oldSignal.aborted).toBe(true);
});

it('Squaremap shares per-tile retention without requesting a native index', async () => {
  vi.useFakeTimers();
  const tiles = new MapTileLayer('/map', world, false, vi.fn(), vi.fn()).addTo(map);
  const first = images[0]!;
  first.dispatchEvent(new Event('load'));
  await vi.advanceTimersByTimeAsync(15000);
  const before = images.length;
  await tiles.refresh(map);
  expect(images.length).toBeGreaterThan(before);
  expect(requests).toHaveLength(0);
  expect(first.isConnected).toBe(true);
  for (const image of images.slice(before)) image.dispatchEvent(new Event('error'));
  expect(first.isConnected).toBe(true);
});
