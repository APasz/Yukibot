import type { components } from '../generated/api';

type Schemas = components['schemas'];
export type Configuration = Schemas['NetworkConfiguration'];
export type Identity = Schemas['NetworkIdentity'];
export type Limits = Schemas['NetworkLimits'];
export type Target = Schemas['NetworkTarget'];
export type Recording = Schemas['NetworkReportInput'];
export type Report = Schemas['NetworkReport'];
export type ReportSummary = Schemas['NetworkReportSummary'];
export type Context = Schemas['NetworkContext'];
export type RecordedContext = Schemas['RecordedContext'];
export type Probe = Recording['probes'][number];
export type Marker = Schemas['NetworkMarker'];
export type MonitoredApp = Schemas['NetworkMonitoredApp'];
export type AppList = Schemas['NetworkAppList'];
