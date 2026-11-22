import Ajv from 'ajv/dist/2020';
import reportSchema from '../generated/report.schema.json';
import type { Recording } from './types';

const ajv = new Ajv({ strict: false, validateFormats: false });
const validateRecording = ajv.compile<Recording>(reportSchema);
let database: Promise<IDBDatabase> | undefined;

function openDatabase(): Promise<IDBDatabase> {
  return (database ??= new Promise((resolve, reject) => {
    const request = indexedDB.open('yukibot-network-diagnostics', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('drafts');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error('Browser storage is blocked by another open tab.'));
  }));
}

export async function readDraft(userId: string): Promise<Recording | null> {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const request = db.transaction('drafts').objectStore('drafts').get(userId);
    request.onsuccess = () => {
      const value: unknown = request.result;
      if (value === undefined) return resolve(null);
      if (!validateRecording(value))
        return reject(new Error('The saved browser draft is invalid or from an older version.'));
      resolve(value);
    };
    request.onerror = () => reject(request.error);
  });
}

export async function writeDraft(userId: string, recording: Recording): Promise<void> {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('drafts', 'readwrite');
    transaction.objectStore('drafts').put(recording, userId);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () =>
      reject(transaction.error ?? new Error('Browser storage write was cancelled.'));
  });
}
