import type * as L from 'leaflet';
import type { Coordinate, World } from './types';

export function isCoordinate(value: unknown): value is Coordinate {
  return (
    value !== null &&
    typeof value === 'object' &&
    'x' in value &&
    'z' in value &&
    typeof value.x === 'number' &&
    typeof value.z === 'number' &&
    Number.isFinite(value.x) &&
    Number.isFinite(value.z) &&
    Math.abs(value.x) <= Number.MAX_SAFE_INTEGER &&
    Math.abs(value.z) <= Number.MAX_SAFE_INTEGER
  );
}

export function isBounds(value: unknown): value is [Coordinate, Coordinate] {
  return Array.isArray(value) && value.length === 2 && value.every(isCoordinate);
}

export const toLatLng = (world: World, point: Coordinate): L.LatLngTuple => [
  (world.north_positive ? point.z : -point.z) * world.scale,
  point.x * world.scale,
];

export const toPoint = (world: World, point: L.LatLng): Coordinate => ({
  x: point.lng / world.scale,
  z: (world.north_positive ? point.lat : -point.lat) / world.scale,
});

export function formatCoordinates(world: Pick<World, 'north_positive'>, point: Coordinate): string {
  const north = Math.round(world.north_positive ? point.z : -point.z);
  const east = Math.round(point.x);
  return `${Math.abs(north)} ${north < 0 ? 'S' : 'N'} · ${Math.abs(east)} ${east < 0 ? 'W' : 'E'}`;
}

export function snapLinePoint(anchor: Coordinate, point: Coordinate): Coordinate {
  const x = point.x - anchor.x;
  const z = point.z - anchor.z;
  if (Math.abs(x) > Math.abs(z) * 2) return { x: point.x, z: anchor.z };
  if (Math.abs(z) > Math.abs(x) * 2) return { x: anchor.x, z: point.z };
  const diagonal = Math.max(Math.abs(x), Math.abs(z));
  return { x: anchor.x + Math.sign(x) * diagonal, z: anchor.z + Math.sign(z) * diagonal };
}

export interface SavedView extends Coordinate {
  zoom: number;
}

export function readView(key: string, maximumZoom: number): SavedView | null {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(key) ?? 'null');
    if (!isCoordinate(raw) || !('zoom' in raw)) return null;
    if (
      typeof raw.zoom !== 'number' ||
      !Number.isFinite(raw.zoom) ||
      raw.zoom < 0 ||
      raw.zoom > maximumZoom
    )
      return null;
    return { x: raw.x, z: raw.z, zoom: raw.zoom };
  } catch {
    return null;
  } // Remembering a view is optional when browser storage is unavailable.
}

export function writeView(key: string, view: SavedView): void {
  try {
    localStorage.setItem(key, JSON.stringify(view));
  } catch {
    /* Storage can be disabled or full. */
  }
}
