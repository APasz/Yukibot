import type * as Leaflet from 'leaflet';
import L, { MAP_PANES } from './leaflet';
import { apiUrl } from './api';
import { toLatLng } from './coordinates';
import type { Coordinate, World } from './types';

type Marker = Leaflet.PathOptions & {
  type: 'icon' | 'polyline' | 'polygon' | 'rectangle' | 'circle' | 'ellipse';
  point?: Coordinate;
  points?: NestedPoints;
  center?: Coordinate;
  radius?: number;
  radiusX?: number;
  radiusZ?: number;
  icon?: string;
  size?: Coordinate;
  anchor?: Coordinate;
  tooltip_anchor?: Coordinate;
  tooltip?: string;
  tooltip_direction?: Leaflet.Direction;
  popup?: string;
};
type NestedPoints = Coordinate[] | NestedPoints[];
export interface MarkerGroup {
  hide?: boolean;
  markers?: Record<string, Marker>;
}

function points(
  world: World,
  values: NestedPoints,
): Leaflet.LatLngExpression[] | Leaflet.LatLngExpression[][] {
  return values.map((value) =>
    Array.isArray(value) ? points(world, value) : toLatLng(world, value),
  ) as Leaflet.LatLngExpression[][];
}

export function markerLayer(
  world: World,
  marker: Marker,
  baseUrl: string,
  renderer: Leaflet.Renderer,
): Leaflet.Layer | null {
  marker = { ...marker, pane: MAP_PANES.source.name, renderer };
  if (marker.type === 'icon' && marker.point && marker.icon) {
    const size = marker.size ?? { x: 24, z: 24 };
    const anchor = marker.anchor ?? { x: size.x / 2, z: size.z / 2 };
    const tooltip = marker.tooltip_anchor ?? { x: 0, z: -size.z / 2 };
    return L.marker(toLatLng(world, marker.point), {
      pane: MAP_PANES.sourceIcons.name,
      icon: L.icon({
        iconUrl: apiUrl(baseUrl, `/assets/icon/registered/${encodeURIComponent(marker.icon)}.png`),
        iconSize: [size.x, size.z],
        iconAnchor: [anchor.x, anchor.z],
        tooltipAnchor: [tooltip.x, tooltip.z],
        popupAnchor: [tooltip.x, tooltip.z],
      }),
    });
  }
  if (marker.type === 'polyline' && marker.points)
    return L.polyline(points(world, marker.points), marker);
  if (marker.type === 'polygon' && marker.points)
    return L.polygon(points(world, marker.points), marker);
  if (marker.type === 'rectangle' && marker.points && marker.points.length >= 2) {
    const [first, second] = marker.points;
    if (first && second && !Array.isArray(first) && !Array.isArray(second)) {
      return L.rectangle([toLatLng(world, first), toLatLng(world, second)], marker);
    }
  }
  if (marker.type === 'circle' && marker.center && marker.radius !== undefined) {
    return L.circle(toLatLng(world, marker.center), {
      ...marker,
      radius: marker.radius * world.scale,
    });
  }
  if (
    marker.type === 'ellipse' &&
    marker.center &&
    marker.radiusX !== undefined &&
    marker.radiusZ !== undefined
  ) {
    const center = marker.center;
    const radiusX = marker.radiusX;
    const radiusZ = marker.radiusZ;
    return L.polygon(
      Array.from({ length: 64 }, (_, index) => {
        const angle = (index * Math.PI) / 32;
        return toLatLng(world, {
          x: center.x + Math.cos(angle) * radiusX,
          z: center.z + Math.sin(angle) * radiusZ,
        });
      }),
      marker,
    );
  }
  return null;
}
