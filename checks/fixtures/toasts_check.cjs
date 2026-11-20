'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(0, 'utf8');

class FakeElement {
  constructor(parent = null) {
    this.parent = parent;
    this.dataset = {};
    this.children = [];
    this.className = '';
    this.hovered = false;
    this.properties = new Map();
    this.style = {setProperty: (key, value) => this.properties.set(key, value)};
  }
  setAttribute() {}
  prepend(child) { child.parent = this; this.children.unshift(child); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  contains(other) {
    return other != null && (other === this || this.contains(other.parent));
  }
  closest() {
    return this.dataset.modToastId ? this : this.parent?.closest() ?? null;
  }
  matches(selector) {
    assert.equal(selector, ':hover');
    return this.hovered;
  }
}

function setup({hidden = false, hoveredOnCreate = false} = {}) {
  let elapsed = 0;
  let nextId = 1;
  const timers = new Map();
  const frames = new Map();
  const notifications = new Map();
  const groups = new Map();
  const listeners = new Map();
  const errors = [];
  const document = {
    hidden,
    activeElement: null,
    addEventListener: (name, callback) => listeners.set(name, callback),
    createElement: () => new FakeElement(),
    querySelector: selector => {
      const id = selector.match(/data-mod-toast-id="(\d+)"/)?.[1];
      const element = notifications.get(id)?.element;
      return selector.endsWith('.mod-toast-progress')
        ? element?.children.find(child => child.className === 'mod-toast-progress') ?? null
        : element ?? null;
    },
  };
  function create(options) {
    const groupKey = options.group === false ? null : `${options.group}|${options.position ?? 'bottom'}`;
    const existing = groupKey === null ? undefined : groups.get(groupKey);
    if (existing !== undefined && options.badgePosition === 'invalid') return false;
    const record = existing ?? {element: new FakeElement(), options, count: 0};
    if (existing === undefined) record.element.hovered = hoveredOnCreate;
    const id = options.attrs['data-mod-toast-id'];
    record.options = options;
    record.count += 1;
    record.element.dataset.modToastId = id;
    notifications.set(id, record);
    if (groupKey !== null) groups.set(groupKey, record);
    return updates => {
      if (!notifications.has(id)) return;
      if (updates !== undefined) {
        assert.equal(groupKey, null, 'Native grouped notifications cannot update');
        record.options = {...record.options, ...updates};
        return;
      }
      notifications.delete(id);
      if (groupKey !== null) groups.delete(groupKey);
      record.options.onDismiss();
    };
  }
  const window = {
    performance: {now: () => elapsed},
    setTimeout: (callback, delay) => {
      const id = nextId++;
      timers.set(id, {callback, at: elapsed + delay});
      return id;
    },
    clearTimeout: id => timers.delete(id),
    requestAnimationFrame: callback => { const id = nextId++; frames.set(id, callback); return id; },
    cancelAnimationFrame: id => frames.delete(id),
    Quasar: {Notify: {create}, lang: {label: {close: 'Close'}}},
  };
  const context = vm.createContext({window, document, Element: FakeElement, HTMLElement: FakeElement,
    console: {error: (...args) => errors.push(args)}});
  vm.runInContext(source, context);
  function advance(milliseconds) {
    const end = elapsed + milliseconds;
    while (true) {
      const pending = [...timers.entries()].filter(([, timer]) => timer.at <= end)
        .sort(([, left], [, right]) => left.at - right.at)[0];
      if (pending === undefined) break;
      const [id, timer] = pending;
      timers.delete(id);
      elapsed = timer.at;
      timer.callback();
    }
    elapsed = end;
    const callbacks = [...frames.values()];
    frames.clear();
    for (const callback of callbacks) callback();
  }
  function interact(name, id, relatedTarget = null, child = null) {
    const element = notifications.get(id).element;
    if (name === 'mouseover') element.hovered = true;
    if (name === 'mouseout') element.hovered = element.contains(relatedTarget);
    if (name === 'focusin') document.activeElement = child ?? element;
    if (name === 'focusout') document.activeElement = relatedTarget;
    listeners.get(name)({target: child ?? element, relatedTarget});
  }
  function setHidden(value) {
    document.hidden = value;
    listeners.get('visibilitychange')();
  }
  return {notify: window.Quasar.Notify.create, notifications, advance, interact, setHidden, frames, errors,
    reinstall: () => vm.runInContext(source, context)};
}

test('visible toasts expire independently', () => {
  const env = setup();
  env.notify({message: 'First'});
  env.advance(1000);
  env.notify({message: 'Second'});
  env.advance(4000);
  assert.deepEqual([...env.notifications.keys()], ['2']);
  env.advance(1000);
  assert.equal(env.notifications.size, 0);
  assert.equal(env.frames.size, 0);
});

test('severity controls defaults and announcements; errors remain dismissible', () => {
  const env = setup();
  const dismiss = env.notify({message: 'Failed', type: 'negative', closeBtn: false});
  env.notify({message: 'Saved', type: 'positive'});
  env.notify({message: 'Warning', type: 'warning'});
  env.notify({message: 'Info', type: 'info'});
  assert.equal(env.notifications.get('1').options.closeBtn, 'Dismiss');
  assert.equal(env.notifications.get('1').options.attrs.role, 'alert');
  assert.equal(env.notifications.get('2').options.attrs.role, 'status');
  env.advance(4000);
  assert.equal(env.notifications.has('2'), false);
  env.advance(1000);
  assert.equal(env.notifications.has('4'), false);
  env.advance(3000);
  assert.deepEqual([...env.notifications.keys()], ['1']);
  env.advance(60000);
  dismiss();
  assert.equal(env.notifications.size, 0);
});

for (const [enter, leave] of [['mouseover', 'mouseout'], ['focusin', 'focusout']]) {
  test(`${enter} pauses only that toast and resumes its remaining time`, () => {
    const env = setup();
    env.notify({message: 'Reading'});
    env.notify({message: 'Other'});
    env.advance(2000);
    env.interact(enter, '1');
    const progress = env.notifications.get('1').element.children[0];
    assert.equal(Number(progress.properties.get('--mod-toast-progress-scale')), 0.6);
    env.advance(10000);
    assert.deepEqual([...env.notifications.keys()], ['1']);
    env.interact(leave, '1');
    env.advance(2999);
    assert.equal(env.notifications.size, 1);
    env.advance(1);
    assert.equal(env.notifications.size, 0);
  });
}

test('mouse exit does not resume while keyboard focus remains inside', () => {
  const env = setup();
  env.notify({message: 'Reading'});
  env.interact('mouseover', '1');
  env.interact('focusin', '1');
  env.interact('mouseout', '1');
  env.advance(20000);
  assert.equal(env.notifications.size, 1);
  env.interact('focusout', '1');
  env.advance(5000);
  assert.equal(env.notifications.size, 0);
});

test('moving between descendants preserves the pause', () => {
  const env = setup();
  env.notify({message: 'Reading'});
  const element = env.notifications.get('1').element;
  const child = new FakeElement(element);
  env.interact('focusin', '1');
  env.interact('focusout', '1', child);
  env.advance(10000);
  assert.equal(env.notifications.size, 1);
});

test('hidden tabs preserve time, including notifications received while hidden', () => {
  const env = setup();
  env.notify({message: 'Existing'});
  env.advance(2000);
  env.setHidden(true);
  env.notify({message: 'New'});
  env.advance(60000);
  assert.equal(env.notifications.size, 2);
  env.setHidden(false);
  env.advance(3000);
  assert.deepEqual([...env.notifications.keys()], ['2']);
  env.advance(2000);
  assert.equal(env.notifications.size, 0);
});

test('returning to a tab reconciles pointer and focus changes made while hidden', () => {
  const env = setup();
  env.notify({message: 'Reading'});
  env.interact('mouseover', '1');
  env.setHidden(true);
  env.notifications.get('1').element.hovered = false;
  env.setHidden(false);
  env.advance(5000);
  assert.equal(env.notifications.size, 0);
});

test('a toast appearing under the pointer starts paused without a mouse event', () => {
  const env = setup({hoveredOnCreate: true});
  env.notify({message: 'Reading'});
  env.advance(10000);
  assert.equal(env.notifications.size, 1);
  env.interact('mouseout', '1');
  env.advance(5000);
  assert.equal(env.notifications.size, 0);
});

test('operation updates preserve the handle and invoke dismissal only once', () => {
  const env = setup();
  let dismissals = 0;
  const control = env.notify({message: 'Starting', type: 'ongoing', group: false, onDismiss: () => dismissals++});
  env.advance(60000);
  control({message: 'Started', type: 'positive', timeout: 4000, spinner: false, closeBtn: false});
  assert.equal(dismissals, 0);
  assert.equal(env.notifications.size, 1);
  assert.equal(env.notifications.get('1').options.message, 'Started');
  assert.equal(env.notifications.get('1').options.attrs.role, 'status');
  env.advance(1000);
  control({message: 'Failed', type: 'negative', timeout: 0});
  assert.equal(env.notifications.get('1').options.attrs.role, 'alert');
  assert.equal(env.notifications.get('1').element.children.length, 0);
  env.advance(60000);
  assert.equal(env.notifications.size, 1);
  control();
  control();
  control({message: 'Ignored'});
  assert.equal(dismissals, 1);
  assert.equal(env.notifications.size, 0);
});

test('an updated success expires without restoring its pending message', () => {
  const env = setup();
  const control = env.notify({message: 'Pending', type: 'ongoing', group: false});
  control({message: 'Complete', type: 'positive', timeout: 4000});
  env.advance(4000);
  assert.equal(env.notifications.size, 0);
});

test('multiline settings keep distinct notifications; actual duplicates reset their timer', () => {
  const env = setup();
  env.notify({message: 'Same', multiLine: false});
  env.notify({message: 'Same', multiLine: true});
  env.advance(4000);
  env.notify({message: 'Same', multiLine: false});
  assert.equal(env.notifications.size, 2);
  assert.equal(env.notifications.get('1').count, 2);
  env.advance(1000);
  assert.deepEqual([...env.notifications.keys()], ['1']);
  env.advance(4000);
  assert.equal(env.notifications.size, 0);
});

test('explicit timeout and dismissal label override defaults', () => {
  const env = setup();
  env.notify({message: 'Temporary failure', type: 'negative', timeout: 1000, closeBtn: 'Understood'});
  assert.equal(env.notifications.get('1').options.closeBtn, 'Understood');
  env.advance(1000);
  assert.equal(env.notifications.size, 0);
});

test('rejecting a duplicate does not stop the original timer or replace its callback', () => {
  const env = setup();
  let dismissals = 0;
  env.notify({message: 'Same', onDismiss: () => dismissals++});
  env.advance(1000);
  assert.equal(env.notify({message: 'Same', badgePosition: 'invalid'}), false);
  env.advance(4000);
  assert.equal(env.notifications.size, 0);
  assert.equal(dismissals, 1);
});

test('installation is idempotent', () => {
  const env = setup();
  env.reinstall();
  env.notify({message: 'Once'});
  assert.equal(env.notifications.size, 1);
  env.advance(5000);
  assert.equal(env.notifications.size, 0);
});
