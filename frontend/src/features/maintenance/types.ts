export type MaintenanceStatus = 'pending' | 'confirmed'

export type MaintenanceTab = 'pending' | 'confirmed'

export type EquipmentType = 'detector' | 'backup_power'

export type EquipmentFilter = 'all' | EquipmentType

export type RiskLevel = 'watch' | 'inspect' | 'insufficient_data'

export type EventSource =
  | 'monitoring_anomaly'
  | 'run_trend'
  | 'device_alarm'

export type ReviewOutcome = 'accurate' | 'needs_revision'

export interface MaintenanceFilters {
  siteId: string
  equipmentType: EquipmentFilter
  keyword: string
}

export interface TimeSeriesPoint {
  time: string
  value: number | null
}

export type DetectorTrendPeriod = 'current24h' | 'lastMonth' | 'lastYear'

export interface DetectorPeriodTrend {
  doseRate: TimeSeriesPoint[]
  rainfall: TimeSeriesPoint[]
}

export interface DetectorTrend {
  current24h: DetectorPeriodTrend
  lastMonth: DetectorPeriodTrend
  lastYear: DetectorPeriodTrend
}

export interface BackupTrend {
  battery: TimeSeriesPoint[]
  currentBattery: number
  estimatedRuntimeHours: number
  historicalRuntimeHours: number
}

export type ExplanationSource = 'model' | 'template'

export type FindingSeverity = 'watch' | 'inspect'

export type ActionPriority = 'primary' | 'secondary'

export interface AnalysisFinding {
  ruleId: string
  code: string
  severity: FindingSeverity
  metric: string
  observed: number | null
  baseline: number | null
  changePercent: number | null
  window: string
  message: string
  sourceRefs: string[]
}

export interface AnalysisAction {
  actionCode: string
  priority: ActionPriority
  text: string
  requiresHumanConfirmation: boolean
}

export interface AnalysisLimitation {
  code: string
  text: string
  affects: string
}

export interface AnalysisDataSnapshot {
  window: {
    start: string
    end: string
  }
  dataAsOf: string
  sampleCount: number
  completeness: number
  missingRate: number
}

export interface AnalysisVersions {
  ruleSet: string
  actionCatalog: string
  prompt: string
  model: {
    name: string
    precision: string
    revision: string
  }
}

export interface AiAnalysis {
  explanation: string
  explanationSource: ExplanationSource
  findings: AnalysisFinding[]
  actions: AnalysisAction[]
  limitations: AnalysisLimitation[]
  dataSnapshot: AnalysisDataSnapshot
  versions: AnalysisVersions
  generatedAt: string
}

export interface HumanReview {
  outcome: ReviewOutcome
  comment: string
  reviewedAt: string
  reviewedBy: string
  updatedAt?: string
}

export interface MaintenanceEvent {
  id: string
  eventNumber: string
  triggeredAt: string
  siteId: string
  siteName: string
  equipmentType: EquipmentType
  equipmentName: string
  eventTitle: string
  eventSource: EventSource
  riskLevel: RiskLevel
  status: MaintenanceStatus
  detectorTrend: DetectorTrend
  backupTrend: BackupTrend
  aiAnalysis: AiAnalysis
  review?: HumanReview
}

export const equipmentTypeLabels: Record<EquipmentType, string> = {
  detector: '探测器',
  backup_power: '备电',
}

export const riskLevelLabels: Record<RiskLevel, string> = {
  watch: '需关注',
  inspect: '建议检修',
  insufficient_data: '数据不足',
}

export const eventSourceLabels: Record<EventSource, string> = {
  monitoring_anomaly: '监测异常事件',
  run_trend: '运行趋势事件',
  device_alarm: '设备报警',
}

export const reviewOutcomeLabels: Record<ReviewOutcome, string> = {
  accurate: '准确',
  needs_revision: '需修正',
}
