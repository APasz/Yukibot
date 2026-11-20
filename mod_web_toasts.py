from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Final, Literal


type ModWebNotificationType = Literal["positive", "negative", "warning", "info", "ongoing"]


@dataclass(frozen=True, slots=True)
class ModWebNotificationPolicy:
    timeout_milliseconds: int
    close_button: bool
    role: Literal["status", "alert"]


_NOTIFICATION_POLICIES: Final[dict[ModWebNotificationType, ModWebNotificationPolicy]] = {
    "positive": ModWebNotificationPolicy(4_000, False, "status"),
    "negative": ModWebNotificationPolicy(0, True, "alert"),
    "warning": ModWebNotificationPolicy(8_000, False, "alert"),
    "info": ModWebNotificationPolicy(5_000, False, "status"),
    "ongoing": ModWebNotificationPolicy(0, True, "status"),
}


def notification_policy(tone: ModWebNotificationType | None) -> ModWebNotificationPolicy:
    """Return the shared policy used by server and browser notifications."""
    return _NOTIFICATION_POLICIES["info" if tone is None else tone]


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
    const policies = __NOTIFICATION_POLICIES__;
    const policyFor = (options) => Object.hasOwn(policies, options.type) ? policies[options.type] : policies.info;
    const recordsById = new Map();
    const recordsByGroup = new Map();
    let nextId = 1;

    const now = () => window.performance.now();
    const notificationForRecord = (record) => document.querySelector(`.q-notification[data-mod-toast-id="${record.id}"]`);
    const progressSelectorFor = (record) => `.q-notification[data-mod-toast-id="${record.id}"] .mod-toast-progress`;
    const syncInteractionState = (record, notification) => {
      record.hovered = notification.matches(':hover');
      record.focused = notification.contains(document.activeElement);
    };
    const progressRatioFor = (record) => (
      record.durationMilliseconds > 0
        ? Math.max(0, Math.min(1, record.remainingMilliseconds / record.durationMilliseconds))
        : 0
    );
    const setProgressRatio = (record, ratio) => {
      const progressElement = record.progressElement;
      if (progressElement === null) {
        return;
      }
      progressElement.style.setProperty('--mod-toast-progress-scale', String(ratio));
    };
    const renderProgress = (record) => {
      if (record.durationMilliseconds <= 0) {
        return;
      }
      const elapsedMilliseconds = record.timerId === null ? 0 : now() - record.startedAt;
      const visibleMilliseconds = Math.max(0, record.remainingMilliseconds - elapsedMilliseconds);
      setProgressRatio(record, visibleMilliseconds / record.durationMilliseconds);
    };
    const stopProgressAnimation = (record) => {
      if (record.progressFrameId === null) {
        return;
      }
      window.cancelAnimationFrame(record.progressFrameId);
      record.progressFrameId = null;
    };
    const detachProgress = (record) => {
      stopProgressAnimation(record);
      const existingProgress = record.progressElement ?? document.querySelector(progressSelectorFor(record));
      if (existingProgress instanceof HTMLElement) {
        existingProgress.remove();
      }
      record.progressElement = null;
    };
    const startProgressAnimation = (record) => {
      if (record.durationMilliseconds <= 0 || record.progressFrameId !== null) {
        return;
      }
      const tick = () => {
        if (record.removed || record.timerId === null) {
          record.progressFrameId = null;
          renderProgress(record);
          return;
        }
        renderProgress(record);
        record.progressFrameId = window.requestAnimationFrame(tick);
      };
      renderProgress(record);
      record.progressFrameId = window.requestAnimationFrame(tick);
    };
    const attachProgress = (record) => {
      if (record.removed || record.durationMilliseconds <= 0) {
        detachProgress(record);
        return;
      }
      const notification = notificationForRecord(record);
      if (!(notification instanceof HTMLElement)) {
        window.setTimeout(() => attachProgress(record), 16);
        return;
      }
      syncInteractionState(record, notification);
      schedule(record);
      const existingProgress = document.querySelector(progressSelectorFor(record));
      if (existingProgress instanceof HTMLElement) {
        record.progressElement = existingProgress;
        setProgressRatio(record, progressRatioFor(record));
        return;
      }
      const progressElement = document.createElement('div');
      progressElement.className = 'mod-toast-progress';
      progressElement.setAttribute('aria-hidden', 'true');
      notification.prepend(progressElement);
      record.progressElement = progressElement;
      setProgressRatio(record, progressRatioFor(record));
    };
    const pause = (record) => {
      if (record.timerId === null) {
        return;
      }
      stopProgressAnimation(record);
      window.clearTimeout(record.timerId);
      record.timerId = null;
      record.remainingMilliseconds = Math.max(0, record.remainingMilliseconds - (now() - record.startedAt));
      record.startedAt = 0;
      setProgressRatio(record, progressRatioFor(record));
    };
    const schedule = (record) => {
      if (record.removed || record.durationMilliseconds <= 0) {
        return;
      }
      if (record.hovered || record.focused || document.hidden) {
        pause(record);
        return;
      }
      if (record.timerId !== null) {
        return;
      }
      if (record.remainingMilliseconds <= 0) {
        dismiss(record);
        return;
      }
      record.startedAt = now();
      record.timerId = window.setTimeout(() => {
        stopProgressAnimation(record);
        record.timerId = null;
        record.startedAt = 0;
        record.remainingMilliseconds = 0;
        setProgressRatio(record, 0);
        dismiss(record);
      }, record.remainingMilliseconds);
      startProgressAnimation(record);
    };
    const remove = (record) => {
      if (record.removed) {
        return;
      }
      pause(record);
      stopProgressAnimation(record);
      record.removed = true;
      recordsById.delete(record.id);
      if (record.groupKey !== null && recordsByGroup.get(record.groupKey) === record) {
        recordsByGroup.delete(record.groupKey);
      }
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
      if ([undefined, null].includes(options.timeout)) {
        return policyFor(options).timeout_milliseconds;
      }
      const timeout = options.timeout;
      if (typeof timeout !== 'number' || !Number.isFinite(timeout) || timeout < 0) {
        return null;
      }
      return timeout;
    };
    const normaliseOptions = (input) => {
      const options = input !== null && typeof input === 'object' ? {...input} : {message: input};
      const policy = policyFor(options);
      // NiceGUI sends closeBtn=false even when the caller uses the default.
      options.closeBtn ||= policy.close_button ? 'Dismiss' : false;
      if (options.timeout === 0 && !options.closeBtn && !(options.actions?.length > 0)) {
        options.closeBtn = 'Dismiss';
      }
      return options;
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
      const multiLine = options.multiLine ?? actionKeys.length > 1;
      const nativeGroup = [options.message, options.caption, multiLine, ...actionKeys].join('|');
      return {groupKey: `${nativeGroup}|${position}`, nativeGroup};
    };
    const classesFor = (classes) => {
      if (Array.isArray(classes)) {
        return classes.join(' ');
      }
      return typeof classes === 'string' ? classes : '';
    };
    const managedControl = (record) => (updates) => {
      if (record.removed) {
        return;
      }
      if (updates === undefined) {
        dismiss(record);
        return;
      }
      if (record.groupKey !== null) {
        console.error('Notify: trying to update a grouped one which is forbidden', updates);
        return;
      }
      const options = normaliseOptions({...record.options, ...updates, group: false, position: record.position});
      const durationMilliseconds = durationFor(options);
      if (durationMilliseconds === null) {
        console.error('Notify: wrong timeout', updates);
        return;
      }
      pause(record);
      record.options = options;
      record.durationMilliseconds = durationMilliseconds;
      record.remainingMilliseconds = durationMilliseconds;
      record.nativeDismiss(nativeOptionsFor(record));
      attachProgress(record);
      schedule(record);
    };
    const managedCreate = (input) => {
      const options = normaliseOptions(input);
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
          focused: false,
          removed: false,
          nativeDismiss: null,
          progressElement: null,
          progressFrameId: null,
        };
      }

      const nativeDismiss = originalCreate({...nativeOptionsFor(record, options), group: nativeGroup});
      if (nativeDismiss === false) {
        return false;
      }
      pause(record);
      record.options = options;
      record.durationMilliseconds = durationMilliseconds;
      record.remainingMilliseconds = durationMilliseconds;
      record.nativeDismiss = nativeDismiss;
      attachProgress(record);
      if (isNewRecord) {
        recordsById.set(record.id, record);
        if (groupKey !== null) {
          recordsByGroup.set(groupKey, record);
        }
      }
      schedule(record);
      return managedControl(record);
    };

    const nativeOptionsFor = (record, options = record.options) => ({
      ...options,
      group: false,
      timeout: 0,
      progress: false,
      classes: `${classesFor(options.classes)} mod-toast-managed`.trim(),
      attrs: {
        role: policyFor(options).role,
        ...options.attrs,
        'data-mod-toast-id': record.id,
      },
      onDismiss: (...args) => {
        remove(record);
        record.options.onDismiss?.(...args);
      },
    });

    const notificationForEvent = (event) => {
      const target = event.target;
      return target instanceof Element ? target.closest('.q-notification[data-mod-toast-id]') : null;
    };
    /** @param {MouseEvent | FocusEvent} event
     * @param {'hovered' | 'focused'} field
     * @param {boolean} value */
    const setInteraction = (event, field, value) => {
      const notification = notificationForEvent(event);
      if (notification === null || notification.contains(event.relatedTarget)) {
        return;
      }
      const record = recordsById.get(notification.dataset.modToastId);
      if (record === undefined) {
        return;
      }
      record[field] = value;
      schedule(record);
    };
    document.addEventListener('mouseover', (event) => setInteraction(event, 'hovered', true));
    document.addEventListener('mouseout', (event) => setInteraction(event, 'hovered', false));
    document.addEventListener('focusin', (event) => setInteraction(event, 'focused', true));
    document.addEventListener('focusout', (event) => setInteraction(event, 'focused', false));
    document.addEventListener('visibilitychange', () => {
      for (const record of recordsById.values()) {
        if (!document.hidden) {
          const notification = notificationForRecord(record);
          if (notification instanceof HTMLElement) {
            syncInteractionState(record, notification);
          }
        }
        schedule(record);
      }
    });

    Object.assign(managedCreate, currentCreate);
    managedCreate.modWebManagedToastQueue = true;
    notify.create = managedCreate;
  };

  install();
})();
""".strip().replace(
    "__NOTIFICATION_POLICIES__",
    json.dumps({tone: asdict(policy) for tone, policy in _NOTIFICATION_POLICIES.items()}),
)

MOD_WEB_TOAST_VERSION = hashlib.sha256(MOD_WEB_TOAST_JAVASCRIPT.encode("utf-8")).hexdigest()[:12]
