from __future__ import annotations

import hashlib


MOD_WEB_TOAST_JAVASCRIPT = r"""
(() => {
  const install = () => {
    const notify = window.Quasar?.Notify;
    const currentCreate = notify?.create;
    if (typeof currentCreate !== 'function') {
      window.setTimeout(install, 50);
      return;
    }
    if (currentCreate.modWebManagedToastQueue === true) {
      return;
    }

    const originalCreate = currentCreate.bind(notify);
    const queuesByPosition = new Map();
    const recordsById = new Map();
    const recordsByGroup = new Map();
    let nextId = 1;

    const now = () => window.performance.now();
    const queueFor = (position) => {
      let queue = queuesByPosition.get(position);
      if (queue === undefined) {
        queue = [];
        queuesByPosition.set(position, queue);
      }
      return queue;
    };
    const pause = (record) => {
      if (record.timerId === null) {
        return;
      }
      window.clearTimeout(record.timerId);
      record.timerId = null;
      record.remainingMilliseconds = Math.max(0, record.remainingMilliseconds - (now() - record.startedAt));
      record.startedAt = 0;
    };
    const activeRecord = (queue) => {
      for (let index = queue.length - 1; index >= 0; index -= 1) {
        if (queue[index].durationMilliseconds > 0) {
          return queue[index];
        }
      }
      return null;
    };
    const schedule = (position) => {
      const queue = queueFor(position);
      const active = activeRecord(queue);
      for (const record of queue) {
        if (record !== active) {
          pause(record);
        }
      }
      if (active === null || active.hovered || active.timerId !== null) {
        return;
      }
      if (active.remainingMilliseconds <= 0) {
        dismiss(active);
        return;
      }
      active.startedAt = now();
      active.timerId = window.setTimeout(() => {
        active.timerId = null;
        active.startedAt = 0;
        active.remainingMilliseconds = 0;
        dismiss(active);
      }, active.remainingMilliseconds);
    };
    const remove = (record) => {
      if (record.removed) {
        return;
      }
      pause(record);
      record.removed = true;
      recordsById.delete(record.id);
      if (record.groupKey !== null && recordsByGroup.get(record.groupKey) === record) {
        recordsByGroup.delete(record.groupKey);
      }
      const queue = queueFor(record.position);
      const index = queue.indexOf(record);
      if (index >= 0) {
        queue.splice(index, 1);
      }
      schedule(record.position);
    };
    const dismiss = (record) => {
      if (record.removed) {
        return;
      }
      const nativeDismiss = record.nativeDismiss;
      remove(record);
      nativeDismiss?.();
    };
    const durationFor = (options) => {
      if (!Object.hasOwn(options, 'timeout')) {
        return options.type === 'ongoing' ? 0 : 5000;
      }
      if ([undefined, null, true, false, ''].includes(options.timeout)) {
        return 5000;
      }
      const timeout = Number(options.timeout);
      if (Number.isNaN(timeout) || timeout < 0) {
        return null;
      }
      return Number.isFinite(timeout) ? timeout : 0;
    };
    const groupFor = (options, position) => {
      if (options.group === false || (!Object.hasOwn(options, 'group') && options.type === 'ongoing')) {
        return {groupKey: null, nativeGroup: false};
      }
      if (options.group !== undefined && options.group !== true) {
        const nativeGroup = String(options.group);
        return {groupKey: `${nativeGroup}|${position}`, nativeGroup};
      }
      const actions = Array.isArray(options.actions) ? options.actions : [];
      const actionKeys = actions.map((action) => `${action?.label}*${action?.icon}`);
      if (options.closeBtn) {
        const closeLabel = typeof options.closeBtn === 'string'
          ? options.closeBtn
          : window.Quasar?.lang?.label?.close;
        actionKeys.push(`${closeLabel}*undefined`);
      }
      const nativeGroup = [options.message, options.caption, options.multiline, ...actionKeys].join('|');
      return {groupKey: `${nativeGroup}|${position}`, nativeGroup};
    };
    const classesFor = (classes) => {
      if (Array.isArray(classes)) {
        return classes.join(' ');
      }
      return typeof classes === 'string' ? classes : '';
    };
    const managedControl = (record) => (updates) => {
      if (updates === undefined) {
        dismiss(record);
        return;
      }
      if (record.groupKey !== null) {
        console.error('Notify: trying to update a grouped one which is forbidden', updates);
        return;
      }
      dismiss(record);
      managedCreate({...record.options, ...updates, group: false, position: record.position});
    };
    const managedCreate = (input) => {
      const options = input !== null && typeof input === 'object' ? {...input} : {message: input};
      const durationMilliseconds = durationFor(options);
      if (durationMilliseconds === null) {
        return originalCreate(input);
      }
      const position = typeof options.position === 'string' ? options.position : 'bottom';
      const {groupKey, nativeGroup} = groupFor(options, position);
      let record = groupKey === null ? undefined : recordsByGroup.get(groupKey);
      const isNewRecord = record === undefined;
      if (record === undefined) {
        record = {
          id: String(nextId++),
          position,
          groupKey,
          options,
          durationMilliseconds,
          remainingMilliseconds: durationMilliseconds,
          timerId: null,
          startedAt: 0,
          hovered: false,
          removed: false,
          nativeDismiss: null,
        };
      } else {
        pause(record);
        record.options = options;
        record.durationMilliseconds = durationMilliseconds;
        record.remainingMilliseconds = durationMilliseconds;
      }

      const originalOnDismiss = options.onDismiss;
      const nativeOptions = {
        ...options,
        group: nativeGroup,
        timeout: 0,
        progress: false,
        classes: `${classesFor(options.classes)} mod-toast-managed`.trim(),
        attrs: {...options.attrs, 'data-mod-toast-id': record.id},
        onDismiss: (...args) => {
          remove(record);
          if (typeof originalOnDismiss === 'function') {
            originalOnDismiss(...args);
          }
        },
      };
      const nativeDismiss = originalCreate(nativeOptions);
      if (nativeDismiss === false) {
        return false;
      }
      record.nativeDismiss = nativeDismiss;
      if (isNewRecord) {
        queueFor(position).push(record);
        recordsById.set(record.id, record);
        if (groupKey !== null) {
          recordsByGroup.set(groupKey, record);
        }
      }
      schedule(position);
      return managedControl(record);
    };

    const notificationForEvent = (event) => {
      const target = event.target;
      return target instanceof Element ? target.closest('.q-notification[data-mod-toast-id]') : null;
    };
    document.addEventListener('mouseover', (event) => {
      const notification = notificationForEvent(event);
      if (notification === null || notification.contains(event.relatedTarget)) {
        return;
      }
      const record = recordsById.get(notification.dataset.modToastId);
      if (record === undefined) {
        return;
      }
      record.hovered = true;
      if (activeRecord(queueFor(record.position)) === record) {
        pause(record);
      }
    });
    document.addEventListener('mouseout', (event) => {
      const notification = notificationForEvent(event);
      if (notification === null || notification.contains(event.relatedTarget)) {
        return;
      }
      const record = recordsById.get(notification.dataset.modToastId);
      if (record === undefined) {
        return;
      }
      record.hovered = false;
      schedule(record.position);
    });

    Object.assign(managedCreate, currentCreate);
    managedCreate.modWebManagedToastQueue = true;
    notify.create = managedCreate;
  };

  install();
})();
""".strip()

MOD_WEB_TOAST_VERSION = hashlib.sha256(MOD_WEB_TOAST_JAVASCRIPT.encode("utf-8")).hexdigest()[:12]
