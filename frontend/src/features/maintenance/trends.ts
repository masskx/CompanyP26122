import {
  fetchTelemetry,
  type TelemetryResponse,
} from '@/api/modules/maintenance'
import type {
  BackupTrend,
  DetectorPeriodTrend,
  DetectorTrend,
  MaintenanceEvent,
  TimeSeriesPoint,
} from './types'

const PERIODS = ['current24h', 'lastMonth', 'lastYear'] as const
type Period = (typeof PERIODS)[number]

export interface EventTrends {
  detectorTrend?: DetectorTrend
  backupTrend?: BackupTrend
}

function validDate(value: string): Date | null {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function shiftMonths(value: string, months: number): string | null {
  const date = validDate(value)
  if (!date) {
    return null
  }

  const day = date.getUTCDate()
  date.setUTCDate(1)
  date.setUTCMonth(date.getUTCMonth() + months)
  const lastDay = new Date(
    Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0),
  ).getUTCDate()
  date.setUTCDate(Math.min(day, lastDay))
  return date.toISOString()
}

function formatLabel(value: string): string {
  const date = validDate(value)
  if (!date) {
    return value
  }

  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(date)

  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${values.month}-${values.day} ${values.hour}:${values.minute}`
}

function pointValue(
  point: TelemetryResponse['points'][number],
): number | null {
  const value = point.value ?? point.avg ?? null
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function toPoints(
  response: TelemetryResponse | null,
  scale = 1,
): TimeSeriesPoint[] {
  if (!response) {
    return []
  }

  return response.points.map((point) => {
    const value = pointValue(point)
    return {
      time: formatLabel(point.time),
      value: value === null ? null : value * scale,
    }
  })
}

function lastValue(points: TimeSeriesPoint[]): number | null {
  for (let index = points.length - 1; index >= 0; index -= 1) {
    const value = points[index]?.value
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value
    }
  }
  return null
}

function periodWindows(event: MaintenanceEvent): Record<
  Period,
  { start: string; end: string }
> | null {
  const window = event.aiAnalysis.dataSnapshot.window
  const start = validDate(window.start)
  const end = validDate(window.end)
  const fallbackEnd = validDate(event.triggeredAt)

  if (!start || !end || !fallbackEnd) {
    return null
  }

  const currentStart = start.toISOString()
  const currentEnd = end.toISOString()
  const lastMonthStart = shiftMonths(currentStart, -1)
  const lastMonthEnd = shiftMonths(currentEnd, -1)
  const lastYearStart = shiftMonths(currentStart, -12)
  const lastYearEnd = shiftMonths(currentEnd, -12)

  if (!lastMonthStart || !lastMonthEnd || !lastYearStart || !lastYearEnd) {
    return null
  }

  return {
    current24h: { start: currentStart, end: currentEnd },
    lastMonth: { start: lastMonthStart, end: lastMonthEnd },
    lastYear: { start: lastYearStart, end: lastYearEnd },
  }
}

async function telemetry(
  stationId: string,
  metric: string,
  window: { start: string; end: string },
  deviceId?: string,
): Promise<TelemetryResponse | null> {
  try {
    return await fetchTelemetry(stationId, {
      metric,
      device_id: deviceId,
      start: window.start,
      end: window.end,
      interval: 3600,
    })
  } catch {
    return null
  }
}

async function detectorTrends(
  event: MaintenanceEvent,
  windows: Record<Period, { start: string; end: string }>,
): Promise<DetectorTrend> {
  const deviceId = event.equipmentId
  if (!deviceId) {
    return {
      current24h: { doseRate: [], rainfall: [] },
      lastMonth: { doseRate: [], rainfall: [] },
      lastYear: { doseRate: [], rainfall: [] },
    }
  }

  const periods = await Promise.all(
    PERIODS.map(async (period) => {
      const window = windows[period]
      const [dose, rain] = await Promise.all([
        telemetry(event.siteId, 'dose_rate', window, deviceId),
        telemetry(event.siteId, 'rainfall', window),
      ])
      const periodTrend: DetectorPeriodTrend = {
        doseRate: toPoints(dose, dose?.calibration_factor ?? 1),
        rainfall: toPoints(rain),
      }
      return [period, periodTrend] as const
    }),
  )

  return Object.fromEntries(periods) as unknown as DetectorTrend
}

async function backupTrend(
  event: MaintenanceEvent,
  windows: Record<Period, { start: string; end: string }>,
): Promise<BackupTrend> {
  const window = windows.current24h
  const [battery, runtime] = await Promise.all([
    telemetry(event.siteId, 'battery_level', window),
    telemetry(event.siteId, 'runtime_hours', window),
  ])

  const batteryPoints = toPoints(battery)
  const runtimePoints = toPoints(runtime)
  const historical = event.aiAnalysis.findings.find(
    (finding) => finding.code === 'backup.runtime_drop',
  )?.baseline

  return {
    battery: batteryPoints,
    currentBattery: lastValue(batteryPoints),
    estimatedRuntimeHours: lastValue(runtimePoints),
    historicalRuntimeHours:
      typeof historical === 'number' ? historical : null,
  }
}

export async function loadEventTrends(
  event: MaintenanceEvent,
): Promise<EventTrends> {
  const windows = periodWindows(event)
  if (!windows) {
    return {}
  }

  if (event.equipmentType === 'detector') {
    return { detectorTrend: await detectorTrends(event, windows) }
  }

  return { backupTrend: await backupTrend(event, windows) }
}
