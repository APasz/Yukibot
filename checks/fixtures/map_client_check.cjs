'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const {getEventListeners} = require('node:events');

// Exercise the shipped functions; Leaflet rendering is checked in Chrome separately.
const bundle = fs.readFileSync(0, 'utf8');
const publicReturn = 'return { mount: (config) => waitForLeaflet(() => void init(config)), deleteAnnotation };';
assert.ok(bundle.includes(publicReturn));
const source = bundle.slice(bundle.indexOf('if (!window.modWebMap) {')).replace(
  publicReturn,
  'return { fetchJsonDetailed, loadPlayerSnapshot, applyPlayerSnapshot, refreshAnnotations, refreshAll, bindAnnotationLayer, bindControls, formatCoordinates, runMapSync, focusPlayer, finishPlayerZoom, clearPlayers, refreshTiles, resetTileLayers, createMarkerAnnotation, finishLineAnnotation, deleteAnnotation, requestAnnotationLabel, resolveLabelPrompt, cancelPendingAnnotation, destroyInstance, instances };',
);

class Layer {
  constructor(point = null) { this.layers = []; this.tooltip = null; this.point = point; this.handlers = new Map(); }
  addTo(target) { target.layers.push(this); this.parent = target; return this; }
  remove() { if (this.parent) this.parent.layers = this.parent.layers.filter(layer => layer !== this); this.removed = true; }
  removeLayer(layer) { layer.remove(); }
  clearLayers() { this.layers = []; }
  eachLayer(callback) { this.layers.forEach(callback); }
  bindTooltip(content) { this.tooltip = content; return this; }
  setTooltipContent(content) { this.tooltip = content; return this; }
  setLatLng(point) { this.point = point; return this; }
  setOpacity(opacity) { this.opacity = opacity; return this; }
  bindPopup() { return this; }
  on(name, callback) { this.handlers.set(name, callback); return this; }
  once(name, callback) { return this.on(name, callback); }
  off(name, callback) { if (this.handlers.get(name) === callback) this.handlers.delete(name); return this; }
}

function setup(fetchResponse) {
  let now = 1000;
  let nextTimer = 1;
  const timers = new Map();
  const allocations = {markers: 0, groups: 0, reorders: 0};
  const document = {removeEventListener: () => {}};
  const window = {
    location: {origin: 'http://map.test'},
    L: {
      layerGroup: () => { allocations.groups++; return new Layer(); },
      tileLayer: () => new Layer(),
      circleMarker: point => {
        allocations.markers++;
        const marker = new Layer(point);
        marker.bringToFront = () => { allocations.reorders++; };
        return marker;
      },
    },
    setTimeout: (callback, delay) => {
      const id = nextTimer++;
      timers.set(id, {callback, delay});
      return id;
    },
    clearTimeout: id => timers.delete(id),
    clearInterval: id => timers.delete(id),
  };
  const context = vm.createContext({window, document, AbortController, URL, URLSearchParams, console,
    fetch: fetchResponse, Date: {now: () => now}, performance: {now: () => now}});
  vm.runInContext(source, context);
  const state = {
    config: {mapApiUrl: '/map', canWrite: false, containerId: 'test-map'},
    container: {isConnected: true, offsetParent: {}},
    abortController: new AbortController(), destroyed: false,
    manifest: {provider: 'sevendays'},
    currentWorld: {name: 'save-one', scale: 1 / 16, north_positive: true,
      zoom: {max: 4, extra: 2},
      player_tracker: {enabled: true, max_age_seconds: 15}, tiles_update_interval: 15},
    map: new Layer(), playerLayer: new Layer(), annotationLayer: new Layer(),
    squaremapLayer: new Layer(), previewLayerGroup: new Layer(), toolButtons: {},
    visiblePlayers: [], playerMarkers: new Map(), playersExpireAt: null, syncPromise: null, queuedSync: null,
    annotationRequestId: 0, annotationSignature: null, worldRevision: 0,
    zooming: false, pendingPlayerFocus: null, pendingLine: [], labelPromptTimer: null,
    tool: 'pan', squaremapSource: 'live', lastTileRefreshAt: now,
  };
  for (const key of ['playerLayer', 'annotationLayer', 'squaremapLayer', 'previewLayerGroup']) {
    state[key].addTo(state.map);
  }
  state.map.panTo = point => { state.lastPan = point; };
  return {client: window.modWebMap, state, timers, allocations, setNow: value => { now = value; }};
}

function response(payload) {
  return {ok: true, headers: {get: () => null}, json: async () => payload};
}

function enableAnnotationPrompt(state) {
  state.config.canWrite = true;
  state.labelPrompt = {hidden: true};
  state.labelPromptTitle = {};
  state.labelPromptInput = {focus: () => {}, select: () => {}};
}

const player = {uuid: '177', name: 'Zoë <3', world: 'save-one', x: -100, z: 250};

test('a timed-out request releases map synchronization and the next refresh can succeed', async () => {
  let failing = true;
  const env = setup((_url, options) => failing
    ? new Promise((_resolve, reject) => options.signal.addEventListener('abort', () => reject(options.signal.reason)))
    : Promise.resolve(response({players: []})));
  const pending = env.client.runMapSync(env.state, () => env.client.fetchJsonDetailed('/map/players'));
  await Promise.resolve();
  assert.equal(env.timers.size, 1, 'Every request needs a bounded lifetime');
  const [{callback, delay}] = env.timers.values();
  assert.ok(delay > 0 && delay <= 30000);
  callback();
  await assert.rejects(pending, /timed out/i);
  assert.equal(env.timers.size, 0);
  assert.equal(env.state.syncPromise, null);
  failing = false;
  await env.client.runMapSync(env.state, () => env.client.fetchJsonDetailed('/map/players'));
  assert.equal(env.timers.size, 0, 'Successful requests must release their timeout');
});

test('the timeout also covers a response body which never finishes', async () => {
  const env = setup(async (_url, options) => ({...response(null), json: () => new Promise(
    (_resolve, reject) => options.signal.addEventListener('abort', () => reject(options.signal.reason)),
  )}));
  const pending = env.client.fetchJsonDetailed('/map/players');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(env.timers.size, 1);
  const [{callback}] = env.timers.values();
  callback();
  await assert.rejects(pending, /timed out/i);
  assert.equal(env.timers.size, 0);
});

test('caller cancellation is preserved and releases the timeout', async () => {
  const env = setup((_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(options.signal.reason));
  }));
  const controller = new AbortController();
  const pending = env.client.fetchJsonDetailed('/map/players', {signal: controller.signal});
  controller.abort(new Error('Caller cancelled'));
  await assert.rejects(pending, /Caller cancelled/);
  assert.equal(env.timers.size, 0);
});

test('network and invalid JSON failures release the timeout', async () => {
  for (const fetchResponse of [
    async () => { throw new Error('Network failed'); },
    async () => ({...response(null), json: async () => { throw new Error('Invalid JSON'); }}),
  ]) {
    const env = setup(fetchResponse);
    await assert.rejects(env.client.fetchJsonDetailed('/map/players'));
    assert.equal(env.timers.size, 0);
  }
});

test('a slower map layer cannot restore player positions after they expire', async () => {
  let releaseMarkers;
  const markers = new Promise(resolve => { releaseMarkers = resolve; });
  const env = setup(url => {
    if (url.endsWith('/markers')) return markers;
    return Promise.resolve(response(url.endsWith('/players')
      ? {players: [player], max_age_seconds: 0.1} : {annotations: []}));
  });
  env.state.manifest.provider = 'squaremap';
  const pending = env.client.refreshAll(env.state);
  await new Promise(resolve => setImmediate(resolve));
  env.setNow(2000);
  env.state.lastTileRefreshAt = 2000;
  releaseMarkers(response([]));
  await pending;
  assert.equal(env.state.visiblePlayers.length, 0);
  assert.equal(env.state.playerLayer.layers.length, 0);
  assert.equal(env.state.playersExpireAt, null);
});

test('malformed player payloads fail instead of reporting an empty server', async () => {
  for (const payload of [null, {}, {players: null}, {players: 'invalid'}]) {
    const env = setup(async () => response(payload));
    await assert.rejects(env.client.loadPlayerSnapshot(env.state), /player.*invalid/i);
  }
});

test('both player protocols work and stale or invalid positions are never displayed', async () => {
  for (const payload of [[player], {players: [player], max_age_seconds: 2}]) {
    const env = setup(async () => response(payload));
    env.state.currentWorld.player_tracker = {enabled: true};
    const snapshot = await env.client.loadPlayerSnapshot(env.state);
    assert.equal(snapshot.players.length, 1);
    assert.equal(snapshot.players[0].name, player.name);
    assert.equal(snapshot.expiresAt, Array.isArray(payload) ? null : 3000);
  }
  const invalid = {...player, x: Infinity};
  const env = setup(async () => response({players: [invalid]}));
  assert.equal((await env.client.loadPlayerSnapshot(env.state)).players.length, 0);
  const stale = setup(async () => ({...response([player]), headers: {get: () => 'stale'}}));
  assert.equal((await stale.client.loadPlayerSnapshot(stale.state)).players.length, 0);
  for (const max_age_seconds of [-1, NaN, Infinity, '15']) {
    const invalidAge = setup(async () => response({players: [player], max_age_seconds}));
    await assert.rejects(invalidAge.client.loadPlayerSnapshot(invalidAge.state), /freshness.*invalid/i);
  }
});

test('empty or disabled player tracking keeps the existing empty layer', async () => {
  const env = setup(async url => response(url.endsWith('/players')
    ? {players: []} : url.endsWith('/markers') ? [] : {annotations: []}));
  const originalLayer = env.state.playerLayer;
  await env.client.refreshAll(env.state);
  assert.equal(env.state.playerLayer, originalLayer);
  env.state.currentWorld.player_tracker.enabled = false;
  await env.client.refreshAll(env.state);
  assert.equal(env.state.playerLayer, originalLayer);
  assert.equal(env.state.visiblePlayers.length, 0);
});

test('both shared annotation tooltip types escape markup', () => {
  const env = setup();
  const label = '<img src=x onerror="window.injected=true">';
  for (const shape of ['marker', 'polyline']) {
    const layer = new Layer();
    env.client.bindAnnotationLayer(env.state, layer, {shape, label});
    assert.ok(layer.tooltip.includes('&lt;img'));
    assert.ok(!layer.tooltip.includes('<img'));
  }
});

test('coordinate directions follow the map provider', () => {
  const {client} = setup();
  assert.equal(client.formatCoordinates({north_positive: true}, {x: -12.4, z: 34.4}), '34 N · 12 W');
  assert.equal(client.formatCoordinates({north_positive: false}, {x: 12.4, z: 34.4}), '34 S · 12 E');
  assert.equal(client.formatCoordinates({north_positive: true}, {x: -0.1, z: -0.1}), '0 N · 0 E');
});

test('clicking during zoom centres on the latest position after the animation', () => {
  const env = setup();
  env.state.zooming = true;
  env.state.visiblePlayers = [player];
  env.client.focusPlayer(env.state, player);
  assert.equal(env.state.lastPan, undefined);
  env.state.visiblePlayers = [{...player, x: 32, z: 48}];
  env.client.finishPlayerZoom(env.state);
  assert.deepEqual(Array.from(env.state.lastPan), [3, 2]);
  assert.equal(env.state.pendingPlayerFocus, null);
});

test('clearing or expiring positions cancels deferred centring', () => {
  for (const expire of [false, true]) {
    const env = setup();
    env.state.zooming = true;
    env.state.visiblePlayers = [player];
    env.state.playersExpireAt = 1100;
    env.client.focusPlayer(env.state, player);
    if (expire) env.setNow(1200);
    else env.client.clearPlayers(env.state);
    env.client.finishPlayerZoom(env.state);
    assert.equal(env.state.lastPan, undefined);
    assert.equal(env.state.pendingPlayerFocus, null);
  }
});

test('repeated and moving player snapshots reuse markers and labels', () => {
  const env = setup();
  const players = Array.from({length: 64}, (_, index) => ({...player, uuid: String(index), name: `Player ${index}`}));
  const layer = env.state.playerLayer;
  for (let poll = 0; poll < 200; poll++) {
    env.client.applyPlayerSnapshot(env.state, {players, expiresAt: 2000});
  }
  assert.equal(env.allocations.markers, 64);
  assert.equal(env.state.playerLayer, layer);
  const marker = layer.layers[0];
  const moved = players.map(player => ({...player, x: 32, z: 48}));
  env.client.applyPlayerSnapshot(env.state, {players: moved, expiresAt: 2000});
  assert.equal(env.allocations.markers, 64);
  assert.deepEqual(Array.from(marker.point), [3, 2]);
  marker.handlers.get('click')();
  assert.deepEqual(Array.from(env.state.lastPan), [3, 2]);
  env.client.applyPlayerSnapshot(env.state, {players: [{...moved[0], name: 'Renamed <3'}], expiresAt: 2000});
  assert.equal(layer.layers.length, 1);
  assert.equal(marker.tooltip, 'Renamed &lt;3');
  assert.equal(env.state.playerMarkers.size, 1);
  env.client.clearPlayers(env.state);
  assert.equal(env.state.playerMarkers.size, 0);
});

test('unchanged annotations retain their layers and an older response cannot overwrite a newer one', async () => {
  const annotation = {annotation_id: 'one', world_name: 'save-one', shape: 'marker',
    label: 'First', points: [{x: 1, z: 2}], color_hex: '#22C55E'};
  let payload = {annotations: [annotation]};
  let delayed = false;
  let release;
  const env = setup(() => delayed
    ? new Promise(resolve => { release = resolve; }) : Promise.resolve(response(payload)));
  await env.client.refreshAnnotations(env.state);
  const originalLayer = env.state.annotationLayer;
  for (let poll = 0; poll < 20; poll++) await env.client.refreshAnnotations(env.state);
  assert.equal(env.allocations.markers, 1);
  assert.equal(env.state.annotationLayer, originalLayer);
  delayed = true;
  const old = env.client.refreshAnnotations(env.state);
  delayed = false;
  payload = {annotations: [{...annotation, label: 'Latest'}]};
  await env.client.refreshAnnotations(env.state);
  release(response({annotations: [annotation]}));
  await old;
  assert.equal(env.state.annotationLayer.layers[0].tooltip, 'Latest');
});

test('manual work queues only the latest request while automatic polls coalesce', async () => {
  const env = setup();
  const calls = [];
  let release;
  const first = env.client.runMapSync(env.state, () => new Promise(resolve => { release = resolve; calls.push('poll'); }));
  await Promise.resolve();
  const dropped = env.client.runMapSync(env.state, async () => calls.push('extra poll'));
  const earlier = env.client.runMapSync(env.state, async () => calls.push('world B'), {queue: true});
  const latest = env.client.runMapSync(env.state, async () => calls.push('world C'), {queue: true});
  release();
  await Promise.all([first, dropped, earlier, latest]);
  assert.deepEqual(calls, ['poll', 'world C']);
  assert.equal(env.state.syncPromise, null);
  assert.equal(env.state.queuedSync, null);
});

test('manual work queued before synchronization starts is retained', async () => {
  const env = setup();
  const calls = [];
  const first = env.client.runMapSync(env.state, async () => calls.push('poll'));
  const latest = env.client.runMapSync(env.state, async () => calls.push('world C'), {queue: true});
  await Promise.all([first, latest]);
  assert.deepEqual(calls, ['poll', 'world C']);
});

test('a submitted label cannot place old coordinates in a newly selected world', async () => {
  const requests = [];
  const env = setup(async url => { requests.push(url); return response({annotations: []}); });
  enableAnnotationPrompt(env.state);
  const pending = env.client.createMarkerAnnotation(env.state, {x: 1, z: 2});
  env.client.resolveLabelPrompt(env.state, 'Old world point');
  env.state.worldRevision++;
  env.state.currentWorld = {...env.state.currentWorld, name: 'save-two'};
  await pending;
  assert.deepEqual(requests, []);
});

test('a completed save cannot clear another world\'s draft or overwrite its status', async () => {
  let release;
  const requests = [];
  const env = setup((url, options) => {
    requests.push({url, options});
    return options.method === 'POST' ? new Promise(resolve => { release = resolve; })
      : Promise.resolve(response({annotations: []}));
  });
  enableAnnotationPrompt(env.state);
  env.state.pendingLine = [{x: 1, z: 2}, {x: 3, z: 4}];
  const pending = env.client.finishLineAnnotation(env.state);
  env.client.resolveLabelPrompt(env.state, 'Old world line');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(JSON.parse(requests[0].options.body).world_name, 'save-one');
  env.state.worldRevision++;
  env.state.currentWorld = {...env.state.currentWorld, name: 'save-two'};
  const draft = [{x: 5, z: 6}];
  env.state.pendingLine = draft;
  env.state.tool = 'line';
  env.state.statusEl = {textContent: 'Loaded save two.', dataset: {}};
  release(response({}));
  await pending;
  assert.equal(env.state.pendingLine, draft);
  assert.equal(env.state.tool, 'line');
  assert.equal(env.state.statusEl.textContent, 'Loaded save two.');
  assert.equal(requests.length, 1);
});

test('saving a line preserves edits made while the server is still responding', async () => {
  for (const replaceDraft of [false, true]) {
    let release;
    let saved;
    const env = setup((_url, options) => {
      if (options.method !== 'POST') return Promise.resolve(response({annotations: []}));
      saved = JSON.parse(options.body);
      return new Promise(resolve => { release = resolve; });
    });
    enableAnnotationPrompt(env.state);
    env.state.pendingLine = [{x: 1, z: 2}, {x: 3, z: 4}];
    env.state.tool = 'line';
    const pending = env.client.finishLineAnnotation(env.state);
    env.client.resolveLabelPrompt(env.state, 'Line');
    await new Promise(resolve => setImmediate(resolve));
    if (replaceDraft) env.state.pendingLine = [];
    env.state.pendingLine.push({x: 5, z: 6});
    const draft = env.state.pendingLine;
    release(response({}));
    await pending;
    assert.deepEqual(saved.points, [{x: 1, z: 2}, {x: 3, z: 4}]);
    assert.equal(env.state.pendingLine, draft);
    assert.equal(env.state.tool, 'line');
  }
});

test('late deletion failures are reported without changing the newly selected world\'s status', async () => {
  let reject;
  const reports = [];
  const env = setup((url, options) => {
    if (url === '/client-errors') {
      reports.push(JSON.parse(options.body));
      return Promise.resolve(response({}));
    }
    return new Promise((_resolve, fail) => { reject = fail; });
  });
  env.state.config.clientErrorUrl = '/client-errors';
  env.client.instances.set(env.state.config.containerId, env.state);
  const pending = env.client.deleteAnnotation(env.state.config.containerId, 'old-annotation');
  env.state.worldRevision++;
  env.state.statusEl = {textContent: 'Loaded save two.', dataset: {}};
  reject(new Error('Deletion failed'));
  await pending;
  assert.equal(env.state.statusEl.textContent, 'Loaded save two.');
  assert.equal(reports.length, 1);
  assert.equal(reports[0].context, 'delete-annotation');
});

test('closing a prompt cancels its deferred focus and positioning work', async () => {
  const env = setup();
  enableAnnotationPrompt(env.state);
  const pending = env.client.requestAnnotationLabel(env.state, 'point');
  assert.equal(env.timers.size, 1);
  env.client.cancelPendingAnnotation(env.state);
  assert.equal(await pending, null);
  assert.equal(env.timers.size, 0);
});

test('an interrupted tile refresh cannot overwrite a remounted map\'s messages', async () => {
  const env = setup(async url => response(url.endsWith('/players') ? {players: []} : {annotations: []}));
  env.client.instances.set(env.state.config.containerId, env.state);
  env.state.tileLayer = new Layer().addTo(env.state.map);
  env.state.statusEl = {textContent: 'New map status', dataset: {}};
  env.state.noticeEl = {textContent: 'New map notice', dataset: {}};
  const pending = env.client.refreshAll(env.state, {announce: true, forceTiles: true});
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(env.state.pendingTileLayer);
  env.client.destroyInstance(env.state.config.containerId);
  env.state.statusEl.textContent = 'New map status';
  env.state.noticeEl.textContent = 'New map notice';
  await pending;
  assert.equal(env.state.statusEl.textContent, 'New map status');
  assert.equal(env.state.noticeEl.textContent, 'New map notice');
});

test('destroying a map aborts pending requests, resolves prompts and discards queued work', async () => {
  const env = setup((_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(options.signal.reason));
  }));
  env.client.instances.set(env.state.config.containerId, env.state);
  let prompt;
  env.state.pendingLabelPrompt = value => { prompt = value; };
  const pending = env.client.loadPlayerSnapshot(env.state);
  let ranQueued = false;
  env.state.queuedSync = async () => { ranQueued = true; };
  env.client.destroyInstance(env.state.config.containerId);
  await assert.rejects(pending);
  assert.equal(env.state.destroyed, true);
  assert.equal(env.state.map.removed, true);
  assert.equal(env.state.queuedSync, null);
  assert.equal(prompt, null);
  assert.equal(env.timers.size, 0);
  assert.equal(env.client.instances.size, 0);
  assert.equal(ranQueued, false);
});

test('unchanged native refreshes make two requests and do not rebuild or reorder overlays', async () => {
  const requests = [];
  const env = setup(async url => {
    requests.push(url);
    return response(url.endsWith('/players') ? {players: [player]} : {annotations: []});
  });
  await env.client.refreshAll(env.state);
  const allocated = {...env.allocations};
  for (let poll = 0; poll < 20; poll++) await env.client.refreshAll(env.state);
  assert.equal(requests.length, 42);
  assert.ok(!requests.some(url => url.endsWith('/markers')));
  assert.deepEqual(env.allocations, allocated);
});

test('a failed poll still executes queued recovery, while a failed final request propagates', async () => {
  const env = setup();
  let reject;
  const first = env.client.runMapSync(env.state, () => new Promise((_resolve, fail) => { reject = fail; }));
  await Promise.resolve();
  const latest = env.client.runMapSync(env.state, async () => 'Recovered', {queue: true});
  reject(new Error('Old request failed'));
  assert.deepEqual(await Promise.all([first, latest]), ['Recovered', 'Recovered']);
  await assert.rejects(env.client.runMapSync(env.state, async () => { throw new Error('New failure'); }), /New failure/);
  assert.equal(env.state.syncPromise, null);
});

test('a reply for a previous world cannot recreate its annotations', async () => {
  let release;
  const env = setup(() => new Promise(resolve => { release = resolve; }));
  const pending = env.client.refreshAnnotations(env.state);
  env.state.worldRevision++;
  env.state.currentWorld = {...env.state.currentWorld, name: 'save-two'};
  release(response({annotations: [{annotation_id: 'old', world_name: 'save-one', shape: 'marker',
    label: 'Old save', points: [{x: 1, z: 2}], color_hex: '#22C55E'}]}));
  await pending;
  assert.equal(env.state.annotationLayer.layers.length, 0);
  assert.equal(env.allocations.markers, 0);
});

test('destroying or switching a map releases an unfinished tile refresh immediately', async () => {
  for (const destroy of [false, true]) {
    const env = setup();
    env.client.instances.set(env.state.config.containerId, env.state);
    env.state.tileLayer = new Layer().addTo(env.state.map);
    const pending = env.client.refreshTiles(env.state, {force: true});
    const next = env.state.pendingTileLayer;
    assert.equal(env.timers.size, 1);
    if (destroy) env.client.destroyInstance(env.state.config.containerId);
    else env.client.resetTileLayers(env.state);
    await pending;
    assert.equal(env.timers.size, 0);
    assert.equal(env.state.finishTileRefresh, null);
    assert.equal(next.opacity, 0, 'A cancelled refresh must never replace the active tiles');
    assert.equal(next.handlers.size, 0);
  }
});

test('remounting does not leave old handlers attached to persistent controls', () => {
  const env = setup();
  env.client.instances.set(env.state.config.containerId, env.state);
  env.state.worldSelect = new EventTarget();
  env.state.refreshButton = new EventTarget();
  env.client.bindControls(env.state);
  assert.equal(getEventListeners(env.state.refreshButton, 'click').length, 1);
  env.client.destroyInstance(env.state.config.containerId);
  assert.equal(getEventListeners(env.state.refreshButton, 'click').length, 0);
  assert.equal(getEventListeners(env.state.worldSelect, 'change').length, 0);
});

test('a replacement tile refresh settles the older refresh without cancelling the newer one', async () => {
  const env = setup();
  env.state.tileLayer = new Layer().addTo(env.state.map);
  const first = env.client.refreshTiles(env.state, {force: true});
  const old = env.state.pendingTileLayer;
  const latest = env.client.refreshTiles(env.state, {force: true});
  const next = env.state.pendingTileLayer;
  await first;
  assert.equal(env.timers.size, 1);
  assert.equal(old.removed, true);
  env.state.finishTileRefresh();
  await latest;
  assert.equal(env.timers.size, 0);
  assert.equal(env.state.tileLayer, next);
  assert.equal(next.opacity, 1);
});
