import type { components } from '../generated/api';

export type Coordinate = components['schemas']['MapCoordinate'];
export type Manifest = components['schemas']['MapManifest'];
export type Annotation = components['schemas']['MapAnnotation'];
export type AnnotationDraft = components['schemas']['MapAnnotationDraft'];
export type AnnotationList = components['schemas']['MapAnnotationList'];
export type WorldSettings = components['schemas']['MapWorldSettings'];
export type TileIndex = components['schemas']['MapTileIndexResponse'];
export type World = Manifest['worlds'][number] & WorldSettings & { scale: number };
export type Tool = 'pan' | 'marker' | 'line';
export type Tone = 'info' | 'success' | 'warning' | 'error';

export interface Player extends Coordinate {
  uuid?: string;
  name: string;
  display_name?: string;
  world: string;
}

export interface PlayerResponse {
  players: Player[];
  max_age_seconds?: number;
}

export interface MapConfig {
  containerId: string;
  canvasId: string;
  canvasFrameId: string;
  worldSelectId: string;
  colorInputId: string;
  snapToggleId: string;
  statusId: string;
  noticeId: string;
  playersId: string;
  coordinatesId: string;
  modeId: string;
  promptId: string;
  promptTitleId: string;
  promptFormId: string;
  promptInputId: string;
  promptSubmitId: string;
  refreshButtonId: string;
  markerButtonId: string;
  lineButtonId: string;
  appName: string;
  nodeName: string;
  mapApiUrl: string;
  clientErrorUrl: string;
  publicMapUrl: string | null;
  canWrite: boolean;
}

export interface MapApi {
  mount(config: MapConfig): void;
  deleteAnnotation(containerId: string, annotationId: string): Promise<void>;
}

declare global {
  interface Window {
    modWebMap?: MapApi;
  }
}
