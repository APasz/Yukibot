'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

// Exercise the shipped functions; Leaflet rendering is checked in Chrome separately.
const bundle = fs.readFileSync(0, 'utf8');
const publicReturn = 'return { mount: (config) => waitForLeaflet(() => void init(config)), deleteAnnotation };';
assert.ok(bundle.includes(publicReturn));
const source = bundle.slice(bundle.indexOf('if (!window.modWebMap) {')).replace(
  publicReturn,
  'return { fetchJsonDetailed, loadPlayerSnapshot, refreshAll, bindAnnotationLayer, formatCoordinates, runMapSync, focusPlayer, finishPlayerZoom, clearPlayers };',
);

class Layer {
  constructor() { this.layers = []; this.tooltip = null; }
  addTo(target) { target.layers.push(this); this.parent = target; return this; }
  remove() { this.parent.layers = this.parent.layers.filter(layer => layer !== this); }
  clearLayers() { this.layers = []; }
  eachLayer(callback) { this.layers.forEach(callback); }
  bindTooltip(content) { this.tooltip = content; return this; }
  bindPopup() { return this; }
  on() { return this; }
}

function setup(fetchResponse) {
  let now = 1000;
  let nextTimer = 1;
  const timers = new Map();
  const window = {
    location: {origin: 'http://map.test'},
    L: {layerGroup: () => new Layer(), circleMarker: () => new Layer()},
    setTimeout: (callback, delay) => {
      const id = nextTimer++;
      timers.set(id, {callback, delay});
      return id;
    },
    clearTimeout: id => timers.delete(id),
  };
  const context = vm.createContext({window, AbortController, URL, URLSearchParams, console,
    fetch: fetchResponse, Date: {now: () => now}});
  vm.runInContext(source, context);
  const state = {
    config: {mapApiUrl: '/map', canWrite: false},
    manifest: {provider: 'sevendays'},
    currentWorld: {name: 'save-one', scale: 1 / 16, north_positive: true,
      player_tracker: {enabled: true, max_age_seconds: 15}, tiles_update_interval: 15},
    map: new Layer(), playerLayer: new Layer(), annotationLayer: new Layer(),
    squaremapLayer: new Layer(), previewLayerGroup: new Layer(), toolButtons: {},
    visiblePlayers: [], playersExpireAt: null, syncPromise: null,
    zooming: false, pendingPlayerFocus: null,
    tool: 'pan', squaremapSource: 'live', lastTileRefreshAt: now,
  };
  for (const key of ['playerLayer', 'annotationLayer', 'squaremapLayer', 'previewLayerGroup']) {
    state[key].addTo(state.map);
  }
  state.map.panTo = point => { state.lastPan = point; };
  return {client: window.modWebMap, state, timers, setNow: value => { now = value; }};
}

function response(payload) {
  return {ok: true, headers: {get: () => null}, json: async () => payload};
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
