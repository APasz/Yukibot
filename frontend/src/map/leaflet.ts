import '../../../resources/vendor/leaflet/leaflet.js';
import type * as Leaflet from 'leaflet';

export const MAP_PANES = {
  source: { name: 'yukibot-map-source', zIndex: 420 },
  sourceIcons: { name: 'yukibot-map-source-icons', zIndex: 421 },
  players: { name: 'yukibot-map-players', zIndex: 440 },
  annotations: { name: 'yukibot-map-annotations', zIndex: 460 },
  preview: { name: 'yukibot-map-preview', zIndex: 480 },
} as const;

declare global {
  interface Window {
    L?: typeof Leaflet;
  }
}

const leaflet = window.L;
if (!leaflet) throw new Error('The bundled Leaflet library failed to initialize.');
export default leaflet;
