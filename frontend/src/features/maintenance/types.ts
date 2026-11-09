export type MaintenanceStatus = 'pending' | 'confirmed'

export type MaintenanceTab = 'pending' | 'confirmed'

export type EquipmentType = 'detector' | 'backup_power'

export type EquipmentFilter = 'all' | EquipmentType

export type RiskLevel = 'watch' | 'inspect' | 'insufficient_data'

export type EventSource =
  | 'monitoring_anomaly'
  | 'run_trend'
  | 'device_alarm'

export type AiFeedbackRating = 'accurate' | 'needs_revision'

export type ConfirmationDecision = 'accepted' | 'rejected'

export type DialogTab = 'analysis' | 'confirmation'

export interface MaintenanceFilters {
  equipmentType: EquipmentFilter
  keyword: string
}

export interface TimeSeriesPoint {
  time: string
  value: number
}

export interface DetectorTrend {
  doseRate: TimeSeriesPoint[]
  rainfall: TimeSeriesPoint[]
}

export interface BackupTrend {
  battery: TimeSeriesPoint[]
  currentBattery: number
  estimatedRuntimeHours: number
  historicalRuntimeHours: number
}

export interface AnalysisEvidence {
  source: string
  metric: string
  timeRange: string
  observation: string
}

export interface AnalysisRecommendation {
  priority: 'high' | 'medium' | 'low'
  action: string
  rationale: string
  requiresHumanConfirmation: boolean
}

export interface AiAnalysis {
  summary: string
  evidence: AnalysisEvidence[]
  recommendations: AnalysisRecommendation[]
  dataGaps: string[]
  followUp: string
  confidence: number
  modelName: string
  modelPrecision: string
  generatedAt: string
}

export interface AiFeedback {
  rating: AiFeedbackRating
  comment: string
  submittedAt: string
}

export interface HumanConfirmation {
  decision: ConfirmationDecision
  comment: string
  confirmedAt: string
  confirmedBy: string
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
  aiFeedback?: AiFeedback
  confirmation?: HumanConfirmation
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

export const aiFeedbackLabels: Record<AiFeedbackRating, string> = {
  accurate: '准确',
  needs_revision: '需修正',
}

export const confirmationDecisionLabels: Record<
  ConfirmationDecision,
  string
> = {
  accepted: '认可 AI 分析并归档',
  rejected: '不认可 AI 分析，记录人工结论',
}
