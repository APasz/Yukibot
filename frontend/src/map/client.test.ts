// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import L, { MAP_PANES } from './leaflet';
import type * as Leaflet from 'leaflet';
import { MapController } from './client';
import { MAP_REQUEST_TIMEOUT_MS } from './api';
import { formatCoordinates, readView, snapLinePoint, toPoint } from './coordinates';
import type { Annotation, Manifest, MapConfig, PlayerResponse, WorldSettings } from './types';

const config: MapConfig = {
  containerId: 'map',
  canvasId: 'canvas',
  canvasFrameId: 'frame',
  worldSelectId: 'world',
  colorInputId: 'color',
  snapToggleId: 'snap',
  statusId: 'status',
  noticeId: 'notice',
  playersId: 'players',
  coordinatesId: 'coordinates',
  promptId: 'prompt',
  promptTitleId: 'prompt-title',
  promptFormId: 'prompt-form',
  promptInputId: 'label',
  promptSubmitId: 'save',
  refreshButtonId: 'refresh',
  markerButtonId: 'point',
  lineButtonId: 'line',
  appName: 'sevendays',
  nodeName: 'erin',
  mapApiUrl: '/map',
  clientErrorUrl: '/errors',
  publicMapUrl: null,
  canWrite: true,
};
const summary = { name: 'save-one', display_name: 'Save one', world_type: 'normal', order: 0 };
let manifest: Manifest;
const initialSettings: WorldSettings = {
  zoom: { max: 4, def: 2, extra: 2 },
  spawn: { x: 0, z: 0 },
  tile_size: 128,
  north_positive: true,
  player_tracker: { enabled: true, max_age_seconds: 15 },
  bounds: [
    { x: -1024, z: -1024 },
    { x: 1024, z: 1024 },
  ],
  marker_update_interval: 5,
  tiles_update_interval: 15,
};
let settings: WorldSettings;
let players: PlayerResponse;
let annotations: Annotation[];
let controller: MapController;
let annotationDelay: Promise<Response> | null;
let annotationFailure: Error | null;
let mutation: (options: RequestInit) => Promise<Response>;
let fetcher: ReturnType<typeof vi.fn>;
let visible: boolean;
let canvasSize: { width: number; height: number };
let resized: () => void;

function response(payload: unknown): Response {
  return new Response(JSON.stringify(payload));
}
const element = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const action = (name: string) =>
  document.querySelector<HTMLButtonElement>(`[data-map-action="${name}"]`)!;
async function settle(): Promise<void> {
  for (let index = 0; index < 30; index++) await Promise.resolve();
}
async function advance(ms: number): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
  await settle();
}

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  settings = structuredClone(initialSettings);
  visible = true;
  canvasSize = { width: 800, height: 600 };
  Object.assign(L.Browser, { canvas: false, svg: true });
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(callback: () => void) {
        resized = callback;
      }
      observe() {}
      disconnect() {}
    },
  );
  vi.spyOn(console, 'error').mockImplementation(() => {});
  document.body.innerHTML = `<div id="map"><div id="status">Loading map…</div><div id="notice"></div>
    <select id="world"></select><input id="color" type="color" value="#22c55e"><label class="mod-map-toggle"><input id="snap" type="checkbox" checked></label>
    <button id="refresh"></button><button id="point"></button><button id="line"></button>
    <div id="players"></div><output id="coordinates"></output><div id="frame"><div id="canvas"></div>
    <div id="prompt" hidden><div id="prompt-title"></div><form id="prompt-form"><input id="label" maxlength="80"><button id="save">Save</button>
    <button type="button" data-map-action="cancel-label">Cancel</button></form></div></div>
    <button data-map-action="fit"></button>
    <div class="mod-map-draft-actions" hidden><button data-map-action="finish"></button><button data-map-action="undo"></button><button data-map-action="cancel"></button></div>
    <form class="mod-map-coordinate-form"><input name="north" value="-123"><input name="east" value="456"><button data-map-action="go"></button></form></div>`;
  Object.defineProperty(element('map'), 'offsetParent', {
    get: () => (visible ? document.body : null),
  });
  Object.defineProperties(element('canvas'), {
    clientWidth: { get: () => canvasSize.width },
    clientHeight: { get: () => canvasSize.height },
  });
  manifest = {
    app_name: 'sevendays',
    app_friendly: '7D2D',
    node_name: 'erin',
    public_map_url: null,
    icon_base_url: './assets',
    initial_world_name: summary.name,
    provider: 'sevendays',
    readiness: 'ready',
    supports_tile_index: true,
    worlds: [summary],
  };
  players = {
    players: [{ uuid: '177', name: 'Zoë <3', world: summary.name, x: -100, z: 250 }],
    max_age_seconds: 15,
  };
  annotations = [];
  annotationDelay = null;
  annotationFailure = null;
  mutation = async () => response({});
  fetcher = vi.fn((url: string, options: RequestInit = {}) => {
    const path = new URL(url, location.origin).pathname;
    if (path === '/errors') return Promise.resolve(response({}));
    if (options.method === 'POST') return mutation(options);
    if (path.endsWith('/manifest')) return Promise.resolve(response(manifest));
    if (path.endsWith('/settings')) return Promise.resolve(response(settings));
    if (path.endsWith('/players')) return Promise.resolve(response(players));
    if (path.endsWith('/annotations')) {
      if (annotationFailure) return Promise.reject(annotationFailure);
      return annotationDelay ?? Promise.resolve(response({ annotations }));
    }
    if (path.endsWith('/markers')) return Promise.resolve(response([]));
    if (path.endsWith('/tiles/index'))
      return Promise.resolve(
        response({
          zoom: Number(new URL(url).searchParams.get('zoom')),
          bounds: settings.bounds,
          tiles: [],
        }),
      );
    return Promise.reject(new Error(`Unexpected request: ${url}`));
  });
  vi.stubGlobal('fetch', fetcher);
});
afterEach(() => {
  controller?.destroy();
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

async function mount(): Promise<void> {
  controller = new MapController(config);
  await settle();
  await advance(150);
}
function mapClick(x: number, z: number): void {
  controller.map.fire('click', {
    latlng: L.latLng(z / 16, x / 16),
    originalEvent: new MouseEvent('click', { detail: 1 }),
  });
}
async function refresh(): Promise<void> {
  element('refresh').click();
  await settle();
  await advance(150);
}

async function expectAnnotationReadRecovery(): Promise<void> {
  annotationFailure = new Error('Annotations unavailable');
  await advance(16000);
  expect(element('status').textContent).toBe('Annotations unavailable');
  annotationFailure = null;
  await advance(16000);
  expect(element('status').textContent).toBe('');
}

it('waits for the map DOM when initialization arrives before the tab renders', async () => {
  const container = element('map');
  container.remove();
  window.modWebMap!.mount(config);
  window.modWebMap!.mount(config);
  await settle();
  expect(fetcher).not.toHaveBeenCalled();
  document.body.append(container);
  await settle();
  await advance(150);
  expect(element<HTMLSelectElement>('world').value).toBe(summary.name);
  expect(element('status').textContent).toBe('');
  expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/manifest'))).toHaveLength(1);
  container.remove();
  await advance(1000);
  expect(vi.getTimerCount()).toBe(0);
});

it('cleans up and reports an initialization wait if the tab never renders', async () => {
  const container = element('map');
  container.remove();
  window.modWebMap!.mount(config);
  await advance(MAP_REQUEST_TIMEOUT_MS);
  expect(fetcher).toHaveBeenCalledWith(
    config.clientErrorUrl,
    expect.objectContaining({ body: expect.stringContaining('"context":"mount"') }),
  );
  expect(vi.getTimerCount()).toBe(0);
  document.body.append(container);
  await settle();
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/manifest'))).toBe(false);
});

it('clears loading feedback once ready and keeps a successful refresh quiet', async () => {
  expect(element('status').textContent).toBe('Loading map…');
  expect(element('notice').textContent).toBe('');
  await mount();
  expect(element('status').textContent).toBe('');
  expect(element('notice').textContent).toBe('');
  expect(element<HTMLSelectElement>('world').selectedOptions[0]?.textContent).toBe(
    summary.display_name,
  );
  element('refresh').click();
  expect(element('status').textContent).toBe('Refreshing map data…');
  await settle();
  expect(element('status').textContent).toBe('');
  expect(element('notice').textContent).toBe('');
});

it('preserves a concurrent annotation error when a metadata refresh finishes', async () => {
  await mount();
  const original = fetcher.getMockImplementation()!;
  let release!: (response: Response) => void;
  fetcher.mockImplementation((url, options) => {
    if (String(url).endsWith('/settings'))
      return new Promise<Response>((resolve) => {
        release = resolve;
      });
    if (String(url).endsWith('/annotations'))
      return Promise.reject(new Error('Annotations offline'));
    return original(url, options);
  });
  await advance(1000);
  element('refresh').click();
  await advance(14000);
  expect(element('status').textContent).toBe('Annotations offline');
  release(response(settings));
  await settle();
  expect(element('status').textContent).toBe('Annotations offline');
});

it.each(['/manifest', '/settings', '/players', '/annotations', '/markers', '/tiles/index'])(
  'clears a recovered %s error during automatic polling',
  async (suffix) => {
    if (suffix === '/markers') manifest.provider = 'squaremap';
    await mount();
    const original = fetcher.getMockImplementation()!;
    let offline = true;
    fetcher.mockImplementation((url, options) =>
      offline && new URL(String(url), location.origin).pathname.endsWith(suffix)
        ? Promise.resolve(
            new Response(JSON.stringify({ detail: 'Erin is unavailable' }), { status: 503 }),
          )
        : original(url, options),
    );
    await advance(16000);
    expect(element('status').textContent).toBe('Erin is unavailable');
    offline = false;
    await advance(16000);
    expect(element('status').textContent).toBe('');
    expect(element('notice').textContent).toBe('');
  },
);

it.each(['/manifest', '/settings'])(
  'retries a failed Squaremap %s refresh automatically until it recovers',
  async (suffix) => {
    manifest.provider = 'squaremap';
    await mount();
    const original = fetcher.getMockImplementation()!;
    let offline = true;
    fetcher.mockImplementation((url, options) =>
      offline && String(url).endsWith(suffix)
        ? Promise.reject(new Error('Metadata unavailable'))
        : original(url, options),
    );
    await refresh();
    expect(element('status').textContent).toBe('Metadata unavailable');
    offline = false;
    await advance(16000);
    expect(element('status').textContent).toBe('');
    const metadataRequests = fetcher.mock.calls.filter(([url]) =>
      String(url).endsWith('/manifest'),
    ).length;
    await advance(16000);
    expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/manifest'))).toHaveLength(
      metadataRequests,
    );
  },
);

it('restores an unresolved player error when annotations recover', async () => {
  settings.marker_update_interval = 60;
  await mount();
  const original = fetcher.getMockImplementation()!;
  let playersOffline = true;
  let annotationsOffline = true;
  fetcher.mockImplementation((url, options) => {
    if (playersOffline && String(url).endsWith('/players'))
      return Promise.reject(new Error('Players unavailable'));
    if (annotationsOffline && String(url).endsWith('/annotations'))
      return Promise.reject(new Error('Annotations unavailable'));
    return original(url, options);
  });
  await refresh();
  expect(element('status').textContent).toBe('Annotations unavailable');
  annotationsOffline = false;
  await advance(16000);
  expect(element('status').textContent).toBe('Players unavailable');
  playersOffline = false;
  await advance(60000);
  expect(element('status').textContent).toBe('');
});

it('recovers automatically when the initial manifest request fails', async () => {
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementationOnce(() => Promise.reject(new Error('Erin is unavailable')));
  await mount();
  expect(element('status').textContent).toBe('Erin is unavailable');
  fetcher.mockImplementation(original);
  await advance(16000);
  expect(element('status').textContent).toBe('');
  expect(element('players').textContent).toContain('Zoë <3');
});

it.each(['retry', 'cancel'] as const)('keeps a failed save until %s retires it', async (action) => {
  await mount();
  mutation = async () => {
    throw new Error('Could not save point');
  };
  element('point').click();
  mapClick(10, 20);
  element<HTMLInputElement>('label').value = 'Retry this point';
  element('prompt-form').dispatchEvent(new Event('submit', { cancelable: true }));
  await settle();
  expect(element('status').textContent).toBe('Could not save point');
  await advance(16000);
  expect(element('status').textContent).toBe('Could not save point');
  expect(element('prompt').hidden).toBe(false);
  mutation = async () => response({});
  if (action === 'retry')
    element('prompt-form').dispatchEvent(new Event('submit', { cancelable: true }));
  else
    element('prompt').querySelector<HTMLButtonElement>('[data-map-action="cancel-label"]')!.click();
  await settle();
  expect(element('prompt').hidden).toBe(true);
  await expectAnnotationReadRecovery();
});

it.each(['retry', 'switch saves'] as const)(
  'retires a failed deletion after %s',
  async (action) => {
    await mount();
    mutation = async () => {
      throw new Error('Could not delete point');
    };
    await controller.deleteAnnotation('point-one');
    expect(element('status').textContent).toBe('Could not delete point');
    mutation = async () => response({});
    if (action === 'retry') await controller.deleteAnnotation('point-one');
    else {
      manifest.worlds = [{ ...summary, name: 'save-two' }];
      manifest.initial_world_name = 'save-two';
      await refresh();
    }
    await expectAnnotationReadRecovery();
  },
);

it('renders terrain from an older native node and adopts indexing after its upgrade', async () => {
  delete (manifest as Partial<Manifest>).supports_tile_index;
  const images: HTMLImageElement[] = [];
  vi.stubGlobal('Image', function () {
    const image = document.createElement('img');
    images.push(image);
    return image;
  });
  await mount();
  expect(images.some((image) => image.src.includes('/worlds/save-one/tiles/'))).toBe(true);
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/tiles/index'))).toBe(false);
  manifest.supports_tile_index = true;
  await advance(15000);
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/tiles/index'))).toBe(true);
});

it('updates players and terrain while annotations are stalled, and reuses player markers', async () => {
  let release!: (value: Response) => void;
  annotationDelay = new Promise((resolve) => {
    release = resolve;
  });
  await mount();
  expect(element('players').textContent).toContain('Zoë <3');
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/tiles/index'))).toBe(true);
  const label = document.querySelector('.mod-map-player-label');
  expect(label?.textContent).toBe('Zoë <3');
  players.players[0]!.x = 200;
  await advance(5000);
  expect(document.querySelector('.mod-map-player-label')).toBe(label);
  release(response({ annotations: [] }));
  await settle();
});

it('annotation failures preserve terrain and do not clear fresh players', async () => {
  await mount();
  annotationDelay = Promise.reject(new Error('Annotations offline'));
  await refresh();
  expect(element('players').textContent).toContain('Zoë <3');
  expect(element('status').textContent).toContain('Annotations offline');
  expect(
    fetcher.mock.calls.filter(([url]) => String(url).includes('/tiles/index')).length,
  ).toBeGreaterThan(1);
});

it('expires disconnected player positions and removes them on an empty successful poll', async () => {
  players.max_age_seconds = 0.2;
  await mount();
  await advance(1000);
  expect(element('players').textContent).toBe('No live player positions.');
  players.max_age_seconds = 15;
  await refresh();
  expect(element('players').textContent).toContain('Zoë <3');
  players.players = [];
  await advance(6000);
  expect(element('players').textContent).toBe('No live player positions.');
});

it('removes departed players from their layer group without retaining old markers', async () => {
  await mount();
  let playersGroup: Leaflet.LayerGroup | undefined;
  controller.map.eachLayer((layer) => {
    if (
      layer instanceof L.LayerGroup &&
      layer.getLayers().some((child) => child instanceof L.CircleMarker)
    )
      playersGroup = layer;
  });
  expect(playersGroup?.getLayers()).toHaveLength(1);
  players.players = [];
  await advance(5000);
  expect(playersGroup?.getLayers()).toHaveLength(0);
});

it.each([false, undefined] as const)(
  'applies tracker setting %s for the same world without discarding the view or draft',
  async (enabled) => {
    await mount();
    const center = controller.map.getCenter();
    element('point').click();
    mapClick(10, 10);
    element<HTMLInputElement>('label').value = 'Keep this draft';
    settings.player_tracker = enabled === undefined ? undefined : { enabled };
    settings.marker_update_interval = 20;
    await refresh();
    expect(element('players').hidden).toBe(true);
    expect(document.querySelector('.mod-map-player-label')).toBeNull();
    expect(element('prompt').hidden).toBe(false);
    expect(element<HTMLInputElement>('label').value).toBe('Keep this draft');
    expect(controller.map.getCenter()).toEqual(center);
    const polls = fetcher.mock.calls.filter(([url]) => String(url).endsWith('/players')).length;
    await advance(20000);
    expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/players'))).toHaveLength(
      polls,
    );
  },
);

it('keeps the active provider when refreshed world settings fail to load', async () => {
  await mount();
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation((url, options) =>
    String(url).endsWith('/settings')
      ? Promise.reject(new Error('Settings unavailable'))
      : original(url, options),
  );
  manifest.provider = 'squaremap';
  await refresh();
  expect(element('status').textContent).toContain('Settings unavailable');
  const indexes = fetcher.mock.calls.filter(([url]) => String(url).includes('/tiles/index')).length;
  await advance(15000);
  expect(
    fetcher.mock.calls.filter(([url]) => String(url).includes('/tiles/index')).length,
  ).toBeGreaterThan(indexes);
  expect(fetcher.mock.calls.filter(([url]) => String(url).endsWith('/markers'))).toHaveLength(0);
});

it('restores the selected world when a world switch fails', async () => {
  const next = { ...summary, name: 'save-two', display_name: 'Save two' };
  manifest.worlds = [summary, next];
  await mount();
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation((url, options) =>
    String(url).includes('/worlds/save-two/settings')
      ? Promise.reject(new Error('Settings unavailable'))
      : original(url, options),
  );
  const select = element<HTMLSelectElement>('world');
  select.value = next.name;
  select.dispatchEvent(new Event('change'));
  await settle();
  expect(select.value).toBe(summary.name);
  expect(element('status').textContent).toContain('Settings unavailable');
  expect(element('players').textContent).toContain('Zoë <3');
});

it('does not resurrect players when tracking is disabled during a pending poll', async () => {
  await mount();
  const original = fetcher.getMockImplementation()!;
  let release!: (response: Response) => void;
  fetcher.mockImplementation((url, options) =>
    String(url).endsWith('/players')
      ? new Promise<Response>((resolve) => {
          release = resolve;
        })
      : original(url, options),
  );
  await advance(5000);
  settings.player_tracker = { enabled: false };
  await refresh();
  release(response(players));
  await settle();
  expect(element('players').hidden).toBe(true);
  expect(document.querySelector('.mod-map-player-label')).toBeNull();
});

it('does not redraw unchanged annotations and treats their labels as text', async () => {
  annotations = [
    {
      annotation_id: 'one',
      world_name: summary.name,
      shape: 'marker',
      points: [{ x: 0, z: 0 }],
      label: '<img src=x onerror=alert(1)>',
      color_hex: '#22C55E',
      created_at_unix_ms: 1,
      created_by_user_id: 42,
      created_by_name: '<script>name</script>',
    },
  ];
  await mount();
  const label = document.querySelector('.mod-map-annotation-label');
  expect(label?.textContent).toBe(annotations[0]!.label);
  expect(label?.querySelector('img')).toBeNull();
  for (let index = 0; index < 5; index++) await refresh();
  expect(document.querySelector('.mod-map-annotation-label')).toBe(label);
});

it('has contextual touch drawing controls, undo and Escape cancellation', async () => {
  await mount();
  element('line').click();
  expect(element('line').getAttribute('aria-pressed')).toBe('true');
  expect(action('finish').disabled).toBe(true);
  mapClick(0, 0);
  mapClick(128, 128);
  expect(action('finish').disabled).toBe(false);
  action('undo').click();
  expect(action('finish').disabled).toBe(true);
  mapClick(128, 128);
  action('finish').click();
  expect(element('prompt').hidden).toBe(false);
  element('label').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  expect(element('prompt').hidden).toBe(true);
  expect(element('line').getAttribute('aria-pressed')).toBe('false');
});

it('keeps overlay backgrounds click-through when Canvas is supported', async () => {
  Object.assign(L.Browser, { canvas: true });
  const context = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(() => {
    throw new Error('Interactive overlays must allow clicks through their empty area.');
  });
  await mount();
  element('point').click();
  mapClick(0, 0);
  action('cancel-label').click();
  for (const pane of [MAP_PANES.players, MAP_PANES.preview]) {
    const root = controller.map.getPane(pane.name)!.querySelector('svg');
    expect(root?.getAttribute('pointer-events')).toBe('none');
  }
  const pan = vi.spyOn(controller.map, 'panTo').mockReturnValue(controller.map);
  controller.map
    .getPane(MAP_PANES.players.name)!
    .querySelector('path')!
    .dispatchEvent(new MouseEvent('click', { bubbles: true }));
  expect(pan).toHaveBeenCalledOnce();
  expect(context).not.toHaveBeenCalled();
});

it('renders interactive Squaremap icons above source geometry', async () => {
  manifest.provider = 'squaremap';
  manifest.supports_tile_index = false;
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation((url, options) =>
    String(url).endsWith('/markers')
      ? Promise.resolve(
          response([
            {
              markers: {
                icon: { type: 'icon', icon: 'base', point: { x: 0, z: 0 }, popup: 'Source base' },
                line: {
                  type: 'polyline',
                  points: [
                    { x: 0, z: 0 },
                    { x: 100, z: 100 },
                  ],
                },
              },
            },
          ]),
        )
      : original(url, options),
  );
  await mount();
  const source = controller.map.getPane(MAP_PANES.source.name)!;
  const icons = controller.map.getPane(MAP_PANES.sourceIcons.name)!;
  expect(source.querySelector('path')).not.toBeNull();
  expect(Number(icons.style.zIndex)).toBeGreaterThan(Number(source.style.zIndex));
  icons.querySelector('img')!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  expect(document.querySelector('.leaflet-popup-content')?.textContent).toBe('Source base');
});

it('retains a failed annotation label and draft, and can retry successfully', async () => {
  await mount();
  element('point').click();
  mapClick(256, 512);
  element<HTMLInputElement>('label').value = 'My base';
  mutation = async () => {
    throw new Error('Save failed');
  };
  element('prompt-form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  expect(element('prompt').hidden).toBe(false);
  expect(element<HTMLInputElement>('label').value).toBe('My base');
  expect(element<HTMLButtonElement>('save').disabled).toBe(false);
  const saved: unknown[] = [];
  mutation = async (options) => {
    saved.push(JSON.parse(String(options.body)));
    return response({});
  };
  element('prompt-form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  expect(saved).toEqual([
    expect.objectContaining({
      label: 'My base',
      world_name: summary.name,
      points: [{ x: 256, z: 512 }],
    }),
  ]);
  expect(element('prompt').hidden).toBe(true);
});

it('releases saved draft controls while the subsequent annotation read is stalled', async () => {
  await mount();
  element('point').click();
  mapClick(10, 20);
  element<HTMLInputElement>('label').value = 'Saved';
  let release!: (result: Response) => void;
  annotationDelay = new Promise((resolve) => {
    release = resolve;
  });
  element('prompt-form').dispatchEvent(new Event('submit', { cancelable: true }));
  await settle();
  expect(element('prompt').hidden).toBe(true);
  element('point').click();
  mapClick(30, 40);
  expect(element('prompt').hidden).toBe(false);
  expect(element<HTMLInputElement>('label').disabled).toBe(false);
  release(response({ annotations: [] }));
  await settle();
});

it('ignores an older annotation read failure after the post-save read succeeds', async () => {
  let reject!: (error: Error) => void;
  annotationDelay = new Promise((_resolve, fail) => {
    reject = fail;
  });
  await mount();
  annotationDelay = null;
  element('point').click();
  mapClick(10, 20);
  element<HTMLInputElement>('label').value = 'Saved';
  element('prompt-form').dispatchEvent(new Event('submit', { cancelable: true }));
  await settle();
  expect(element('status').textContent).toBe('Added Saved.');
  reject(new Error('Obsolete annotation request failed'));
  await settle();
  expect(element('status').textContent).toBe('Added Saved.');
  expect(fetcher.mock.calls.filter(([url]) => url === '/errors')).toHaveLength(0);
});

it('reports a follow-up read failure separately from a successful annotation save', async () => {
  await mount();
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation((url, options) =>
    String(url).endsWith('/annotations') && options?.method !== 'POST'
      ? Promise.reject(new Error('Read unavailable'))
      : original(url, options),
  );
  element('point').click();
  mapClick(10, 20);
  element<HTMLInputElement>('label').value = 'Saved';
  element('prompt-form').dispatchEvent(new Event('submit', { cancelable: true }));
  await settle();
  const reports = fetcher.mock.calls.filter(([url]) => url === '/errors');
  expect(reports).toHaveLength(1);
  expect(JSON.parse(String(reports[0]![1]!.body))).toMatchObject({ context: 'annotations' });
  expect(element('prompt').hidden).toBe(true);
});

it('fits explored terrain once the initially hidden map has a usable size', async () => {
  visible = false;
  canvasSize = { width: 0, height: 0 };
  await mount();
  visible = true;
  canvasSize = { width: 800, height: 600 };
  resized();
  await advance(150);
  expect(controller.map.getZoom()).toBe(2);
  expect(controller.map.getCenter().equals(L.latLng(0, 0))).toBe(true);
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('/tiles/index'))).toBe(true);
});

it('uses the latest player position after a click during zoom, and discards expired focus', async () => {
  await mount();
  const pan = vi.spyOn(controller.map, 'panTo').mockReturnValue(controller.map);
  controller.map.fire('zoomstart');
  document.querySelector<HTMLButtonElement>('.mod-map-player-button')!.click();
  expect(pan).not.toHaveBeenCalled();
  players.players[0]!.x = 32;
  players.players[0]!.z = 48;
  await advance(5000);
  controller.map.fire('zoomend');
  expect(pan).toHaveBeenLastCalledWith([3, 2]);
  pan.mockClear();
  players.max_age_seconds = 0.1;
  await refresh();
  controller.map.fire('zoomstart');
  document.querySelector<HTMLButtonElement>('.mod-map-player-button')!.click();
  await advance(1000);
  controller.map.fire('zoomend');
  expect(pan).not.toHaveBeenCalled();
});

it('a late save response cannot reset the next save’s draft', async () => {
  await mount();
  element('point').click();
  mapClick(10, 20);
  let release!: (response: Response) => void;
  mutation = () =>
    new Promise((resolve) => {
      release = resolve;
    });
  element<HTMLInputElement>('label').value = 'Old world';
  element('prompt-form').dispatchEvent(new Event('submit', { cancelable: true }));
  await settle();
  const next = { ...summary, name: 'save-two' };
  manifest.worlds = [next];
  manifest.initial_world_name = next.name;
  await advance(15000);
  element('point').click();
  mapClick(30, 40);
  element<HTMLInputElement>('label').value = 'Next world';
  release(response({}));
  await settle();
  expect(element('prompt').hidden).toBe(false);
  expect(element<HTMLInputElement>('label').value).toBe('Next world');
  expect(element<HTMLButtonElement>('save').disabled).toBe(false);
});

it('double-click finishing does not add a duplicate line endpoint', async () => {
  await mount();
  element('line').click();
  mapClick(0, 0);
  mapClick(16, 16);
  mapClick(16, 16);
  controller.map.fire('dblclick');
  element<HTMLInputElement>('label').value = 'Line';
  const submitted: unknown[] = [];
  mutation = async (options) => {
    submitted.push(JSON.parse(String(options.body)));
    return response({});
  };
  element('prompt-form').dispatchEvent(new Event('submit', { cancelable: true }));
  await settle();
  expect(submitted).toEqual([
    expect.objectContaining({
      points: [
        { x: 0, z: 0 },
        { x: 16, z: 16 },
      ],
    }),
  ]);
});

it.each(['disabled', 'restart_required', 'waiting_for_tiles'] as const)(
  'automatically recovers from %s without reloading the page',
  async (readiness) => {
    manifest.readiness = readiness;
    manifest.worlds = [];
    await mount();
    expect(element('notice').textContent).not.toBe('');
    expect(element('status').textContent).toBe('');
    expect(element<HTMLButtonElement>('point').disabled).toBe(true);
    manifest.readiness = 'ready';
    manifest.worlds = [summary];
    await advance(15000);
    expect(element<HTMLSelectElement>('world').options[0]?.textContent).toBe(summary.display_name);
    expect(element<HTMLButtonElement>('point').disabled).toBe(false);
    expect(element('players').textContent).toContain('Zoë <3');
    expect(element('notice').textContent).toBe('');
    expect(element('status').textContent).toBe('');
  },
);

it('clears an old world draft and player positions after save selection changes', async () => {
  await mount();
  element('point').click();
  mapClick(10, 10);
  const next = { ...summary, name: 'save-two', display_name: 'Save two' };
  manifest.worlds = [next];
  manifest.initial_world_name = next.name;
  await advance(15000);
  expect(element('prompt').hidden).toBe(true);
  expect(element('players').textContent).toBe('No live player positions.');
  expect(element<HTMLSelectElement>('world').value).toBe(next.name);
});

it('polls the newly selected world immediately after aborting an old player request', async () => {
  await mount();
  const original = fetcher.getMockImplementation()!;
  let delayed = false;
  fetcher.mockImplementation((url, options) => {
    if (String(url).endsWith('/players') && !delayed) {
      delayed = true;
      return new Promise<Response>((_resolve, reject) => {
        options!.signal!.addEventListener('abort', () => reject(options!.signal!.reason));
      });
    }
    return original(url, options);
  });
  await advance(5000);
  const next = { ...summary, name: 'save-two' };
  manifest.worlds = [next];
  manifest.initial_world_name = next.name;
  players.players = [{ name: 'New world player', uuid: 'new', world: next.name, x: 10, z: 20 }];
  await refresh();
  expect(element('players').textContent).toContain('New world player');
});

it('supports coordinate navigation and restores a view per save', async () => {
  await mount();
  vi.spyOn(controller.map, 'panTo').mockImplementation((point) =>
    controller.map.setView(point, controller.map.getZoom(), { animate: false }),
  );
  document
    .querySelector('form.mod-map-coordinate-form')!
    .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await advance(500);
  const world = { ...summary, ...settings, scale: 1 / 16 };
  expect(toPoint(world, controller.map.getCenter()).x).toBe(456);
  expect(Math.abs(toPoint(world, controller.map.getCenter()).z + 123)).toBeLessThanOrEqual(2);
  controller.map.setZoom(5);
  const saved = toPoint(world, controller.map.getCenter());
  controller.destroy();
  controller = new MapController(config);
  await settle();
  expect(toPoint(world, controller.map.getCenter())).toEqual(saved);
  expect(controller.map.getZoom()).toBe(5);
  expect(readView('missing', 6)).toBeNull();
});

it.each(['', '1e20'])(
  'rejects unsafe coordinate navigation %s and unsafe remembered views',
  async (north) => {
    await mount();
    const pan = vi.spyOn(controller.map, 'panTo').mockReturnValue(controller.map);
    document.querySelector<HTMLInputElement>('[name="north"]')!.value = north;
    document
      .querySelector('form.mod-map-coordinate-form')!
      .dispatchEvent(new Event('submit', { cancelable: true }));
    expect(pan).not.toHaveBeenCalled();
    expect(element('status').textContent).toContain('Enter valid');
    localStorage.setItem('invalid-view', JSON.stringify({ x: 1e20, z: 0, zoom: 4 }));
    expect(readView('invalid-view', 6)).toBeNull();
  },
);

it('leaves Escape available while panning and removes timers and handlers on destruction', async () => {
  await mount();
  const escape = new KeyboardEvent('keydown', {
    key: 'Escape',
    bubbles: true,
    cancelable: true,
  });
  element('map').dispatchEvent(escape);
  expect(escape.defaultPrevented).toBe(false);
  controller.destroy();
  const count = fetcher.mock.calls.length;
  element('refresh').click();
  await advance(30000);
  expect(fetcher.mock.calls).toHaveLength(count);
});

it('formats provider coordinates and snaps lines in each direction', () => {
  expect(formatCoordinates({ north_positive: true }, { x: -12.4, z: 34.4 })).toBe('34 N · 12 W');
  expect(formatCoordinates({ north_positive: false }, { x: 12.4, z: 34.4 })).toBe('34 S · 12 E');
  expect(formatCoordinates({ north_positive: true }, { x: -0.1, z: -0.1 })).toBe('0 N · 0 E');
  expect(snapLinePoint({ x: 0, z: 0 }, { x: -3, z: 2 })).toEqual({ x: -3, z: 3 });
});
