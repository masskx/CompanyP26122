import type {
  StationSummary,
  WorkOrder,
} from '@/api/modules/maintenance'
import type {
  AiAnalysis,
  AnalysisAction,
  AnalysisDataSnapshot,
  AnalysisFinding,
  AnalysisLimitation,
  AnalysisVersions,
  BackupTrend,
  DetectorTrend,
  EventSource,
  HealthLevel,
  HumanReview,
  MaintenanceEvent,
  MaintenanceStatus,
  ReviewOutcome,
} from './types'

type Dict = Record<string, unknown>

const REVIEWER_LABEL = '现场专家'

function dict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Dict)
    : {}
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value)
    ? value
    : fallback
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function arrayOf(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function stringArray(value: unknown): string[] {
  return arrayOf(value).filter(
    (item): item is string => typeof item === 'string',
  )
}

function truncate(value: string, max = 90): string {
  const normalized = value.replace(/\s+/g, ' ').trim()
  return normalized.length > max
    ? `${normalized.slice(0, max)}…`
    : normalized
}

export function emptyDetectorTrend(): DetectorTrend {
  return {
    current24h: { doseRate: [], rainfall: [] },
    lastMonth: { doseRate: [], rainfall: [] },
    lastYear: { doseRate: [], rainfall: [] },
  }
}

export function emptyBackupTrend(): BackupTrend {
  return {
    battery: [],
    currentBattery: null,
    estimatedRuntimeHours: null,
    historicalRuntimeHours: null,
  }
}

function mapFinding(raw: unknown): AnalysisFinding {
  const value = dict(raw)
  return {
    ruleId: text(value.rule_id),
    code: text(value.code),
    severity: value.severity === 'inspect' ? 'inspect' : 'watch',
    metric: text(value.metric),
    observed: numberOrNull(value.observed),
    baseline: numberOrNull(value.baseline),
    changePercent: numberOrNull(value.change_percent),
    window: text(value.window),
    message: text(value.message),
    sourceRefs: stringArray(value.source_refs),
  }
}

function mapAction(raw: unknown): AnalysisAction {
  const value = dict(raw)
  return {
    actionCode: text(value.action_code),
    priority: value.priority === 'primary' ? 'primary' : 'secondary',
    text: text(value.text),
    requiresHumanConfirmation:
      value.requires_human_confirmation === true,
  }
}

function mapLimitation(raw: unknown): AnalysisLimitation {
  const value = dict(raw)
  return {
    code: text(value.code),
    text: text(value.text),
    affects: text(value.affects),
  }
}

function fallbackWindow(wo: WorkOrder): { start: string; end: string } {
  const end = Number.isNaN(new Date(wo.triggered_at).getTime())
    ? new Date().toISOString()
    : wo.triggered_at
  const start = new Date(
    new Date(end).getTime() - 24 * 60 * 60 * 1000,
  ).toISOString()
  return { start, end }
}

export function eventWindow(wo: WorkOrder): { start: string; end: string } {
  const snapshot = dict(dict(wo.ai_result).data_snapshot)
  const window = dict(snapshot.window)
  const fallback = fallbackWindow(wo)
  return {
    start: text(window.start, fallback.start),
    end: text(window.end, fallback.end),
  }
}

function mapAnalysis(wo: WorkOrder): AiAnalysis {
  const analysis = dict(wo.ai_result)
  const findings = arrayOf(analysis.findings).map(mapFinding)
  const actions = arrayOf(analysis.actions).map(mapAction)
  const limitations = arrayOf(analysis.limitations).map(mapLimitation)
  const snapshot = dict(analysis.data_snapshot)
  const window = eventWindow(wo)
  const versions = dict(analysis.versions)
  const model = dict(versions.model)

  const dataSnapshot: AnalysisDataSnapshot = {
    window,
    dataAsOf: text(snapshot.data_as_of, wo.triggered_at),
    sampleCount: numberOr(snapshot.sample_count, 0),
    completeness: numberOr(snapshot.completeness, 0),
    missingRate: numberOr(snapshot.missing_rate, 0),
  }

  const mappedVersions: AnalysisVersions = {
    ruleSet: text(versions.rule_set, '未知'),
    actionCatalog: text(versions.action_catalog, '未知'),
    prompt: text(versions.prompt, '未知'),
    model: {
      name: text(model.name, '未知'),
      precision: text(model.precision, '未知'),
      revision: text(model.revision, '未知'),
    },
  }

  const explanation = text(
    analysis.explanation,
    '暂无 AI 分析结果，请以规则依据和现场核查为准。',
  )

  return {
    explanation,
    explanationSource:
      analysis.explanation_source === 'model' ? 'model' : 'template',
    findings,
    actions,
    limitations,
    dataSnapshot,
    versions: mappedVersions,
    generatedAt: text(analysis.generated_at, wo.triggered_at),
  }
}

function equipmentTypeFor(
  wo: WorkOrder,
): 'detector' | 'backup_power' {
  if (wo.risk_type === 'backup_power') {
    return 'backup_power'
  }
  if (wo.risk_type === 'detector') {
    return 'detector'
  }
  return /-BAT-/i.test(wo.equipment_id) ? 'backup_power' : 'detector'
}

function healthLevelFor(wo: WorkOrder): HealthLevel {
  if (
    wo.health_level === 'watch' ||
    wo.health_level === 'inspect' ||
    wo.health_level === 'insufficient_data'
  ) {
    return wo.health_level
  }
  return 'insufficient_data'
}

function statusFor(wo: WorkOrder): MaintenanceStatus {
  return wo.status === 'confirmed' ? 'confirmed' : 'pending'
}

function eventSourceFor(findings: AnalysisFinding[]): EventSource {
  const codes = findings.map((finding) => finding.code)
  if (
    codes.some(
      (code) =>
        code.includes('device_alarm') || code.includes('device_fault'),
    )
  ) {
    return 'device_alarm'
  }
  if (
    codes.some(
      (code) =>
        code.includes('rise') ||
        code.includes('runtime_drop') ||
        code.includes('slow_recharge') ||
        code.includes('rainfall'),
    )
  ) {
    return 'run_trend'
  }
  return 'monitoring_anomaly'
}

function reviewFor(wo: WorkOrder): HumanReview | undefined {
  if (!wo.review) {
    return undefined
  }
  const outcome: ReviewOutcome =
    wo.review.outcome === 'needs_revision'
      ? 'needs_revision'
      : 'accurate'
  const updatedAt = text(wo.review.updated_at)
  return {
    outcome,
    comment: text(wo.review.comment),
    reviewedAt: text(wo.review.reviewed_at, wo.triggered_at),
    reviewedBy: REVIEWER_LABEL,
    ...(updatedAt ? { updatedAt } : {}),
  }
}

function stationFor(
  wo: WorkOrder,
  stations: Map<string, StationSummary>,
): StationSummary | undefined {
  return stations.get(wo.site_id)
}

function equipmentNameFor(
  wo: WorkOrder,
  station: StationSummary | undefined,
): string {
  const device = station?.devices?.find(
    (item) => item.device_id === wo.equipment_id,
  )
  return device?.name || wo.equipment_id
}

export function indexStations(
  stations: StationSummary[],
): Map<string, StationSummary> {
  return new Map(stations.map((station) => [station.station_id, station]))
}

export function workOrderToEvent(
  wo: WorkOrder,
  stations: Map<string, StationSummary>,
): MaintenanceEvent {
  const station = stationFor(wo, stations)
  const equipmentType = equipmentTypeFor(wo)
  const aiAnalysis = mapAnalysis(wo)
  const findings = aiAnalysis.findings
  const firstMessage = findings[0]?.message
  const eventTitle = truncate(
    firstMessage || aiAnalysis.explanation || '维护事件',
  )

  const base = {
    id: wo.event_id,
    eventNumber: wo.event_id,
    triggeredAt: wo.triggered_at,
    siteId: wo.site_id,
    siteName: station?.name || wo.site_id,
    equipmentId: wo.equipment_id,
    equipmentName: equipmentNameFor(wo, station),
    eventTitle,
    eventSource: eventSourceFor(findings),
    healthLevel: healthLevelFor(wo),
    status: statusFor(wo),
    aiAnalysis,
    review: reviewFor(wo),
  }

  if (equipmentType === 'backup_power') {
    return {
      ...base,
      equipmentType: 'backup_power',
      backupTrend: emptyBackupTrend(),
    }
  }

  return {
    ...base,
    equipmentType: 'detector',
    detectorTrend: emptyDetectorTrend(),
  }
}

/** Refresh an existing event from a work-order copy without losing its trends. */
export function applyWorkOrderToEvent(
  event: MaintenanceEvent,
  wo: WorkOrder,
  stations: Map<string, StationSummary>,
): void {
  const updated = workOrderToEvent(wo, stations)
  event.eventNumber = updated.eventNumber
  event.triggeredAt = updated.triggeredAt
  event.siteId = updated.siteId
  event.siteName = updated.siteName
  event.equipmentId = updated.equipmentId
  event.equipmentName = updated.equipmentName
  event.eventTitle = updated.eventTitle
  event.eventSource = updated.eventSource
  event.healthLevel = updated.healthLevel
  event.status = updated.status
  event.aiAnalysis = updated.aiAnalysis
  event.review = updated.review
}
