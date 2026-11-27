import http from '../client'
import internalHttp from '../internal'

export type WorkOrderRiskType = 'detector' | 'backup_power' | 'general'
export type WorkOrderHealthLevel = 'watch' | 'inspect' | 'insufficient_data'
export type WorkOrderStatus = 'pending' | 'confirmed'

export interface WorkOrderReview {
  outcome: 'accurate' | 'needs_revision'
  comment: string
  reviewed_at: string
  updated_at?: string | null
  archived_at?: string | null
}

export interface WorkOrder {
  event_id: string
  site_id: string
  equipment_id: string
  risk_type: WorkOrderRiskType
  health_level: WorkOrderHealthLevel
  status: WorkOrderStatus | null
  triggered_at: string
  analysis_id?: string | null
  ai_result?: Record<string, unknown> | null
  review?: WorkOrderReview | null
  updated_at?: string | null
  received_at?: string | null
}

export interface WorkOrderList {
  items: WorkOrder[]
  next_offset: number | null
}

export interface ReviewSubmission {
  outcome: 'accurate' | 'needs_revision'
  comment?: string
  reviewed_at?: string
  updated_at?: string
  archived_at?: string
}

export interface StationDeviceSummary {
  device_id: string
  name?: string
}

export interface StationSummary {
  station_id: string
  name: string
  devices?: StationDeviceSummary[]
  backup_power_id?: string | null
  environment_id?: string | null
}

export interface TelemetryPoint {
  time: string
  value?: number | null
  avg?: number | null
  min?: number | null
  max?: number | null
  count?: number
}

export interface TelemetryResponse {
  station_id: string
  equipment_id: string
  metric: string
  unit: string | null
  calibration_factor: number | null
  interval_seconds: number | null
  points: TelemetryPoint[]
}

export interface TelemetryQuery {
  metric: string
  device_id?: string
  start: string
  end: string
  interval?: number
}

const workOrdersPath = '/maintenance/work-orders'

export function listWorkOrders(limit = 200, offset = 0): Promise<WorkOrder[]> {
  return internalHttp
    .get<WorkOrderList>(workOrdersPath, { params: { limit, offset } })
    .then((response) => response.data.items ?? [])
}

export function putWorkOrderReview(
  eventId: string,
  submission: ReviewSubmission,
): Promise<WorkOrder> {
  return internalHttp
    .put<WorkOrder>(
      `${workOrdersPath}/${encodeURIComponent(eventId)}/review`,
      submission,
    )
    .then((response) => response.data)
}

export function listStations(): Promise<StationSummary[]> {
  return http
    .get<StationSummary[]>('/v1/stations')
    .then((response) => response.data)
}

export function fetchTelemetry(
  stationId: string,
  query: TelemetryQuery,
): Promise<TelemetryResponse> {
  return http
    .get<TelemetryResponse>(
      `/v1/stations/${encodeURIComponent(stationId)}/telemetry`,
      { params: query },
    )
    .then((response) => response.data)
}
