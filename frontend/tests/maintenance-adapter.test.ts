import { describe, expect, it } from 'vitest'
import type {
  StationSummary,
  WorkOrder,
} from '../src/api/modules/maintenance'
import {
  indexStations,
  workOrderToEvent,
} from '../src/features/maintenance/adapter'

const stations: StationSummary[] = [
  {
    station_id: 'AS06',
    name: '监测站 06',
    devices: [{ device_id: 'AS06-DET-01', name: 'AS06 碘化钠探测器' }],
  },
]

const aiResult = {
  explanation: '外部交流供电中断，备电续航下降。',
  explanation_source: 'template',
  findings: [
    {
      rule_id: 'BACKUP-RUNTIME-DROP',
      code: 'backup.runtime_drop',
      severity: 'watch',
      metric: 'runtime_hours',
      observed: 18,
      baseline: 23.66,
      change_percent: -23.9,
      window: '2026-11-23T00:00:00+08:00/2026-11-24T00:00:00+08:00',
      message: '参考续航 18.0 小时，较基线下降约 24%',
      source_refs: ['ts:AS06:AS06-BAT-01:runtime_hours:11-23/11-24'],
    },
  ],
  actions: [
    {
      action_code: 'backup.check_charging_path',
      priority: 'primary',
      text: '检查充电模块、接线和实际负载',
      requires_human_confirmation: true,
    },
  ],
  limitations: [
    { code: 'backup.missing_current', text: '缺少充电电流', affects: '无法确认充电状态' },
  ],
  data_snapshot: {
    window: {
      start: '2026-11-23T00:00:00+08:00',
      end: '2026-11-24T00:00:00+08:00',
    },
    data_as_of: '2026-11-23T23:59:30+08:00',
    sample_count: 2882,
    completeness: 1,
    missing_rate: 0,
  },
  versions: {
    rule_set: 'maintenance-rules-test',
    action_catalog: 'maintenance-actions-test',
    prompt: 'maintenance-narrator-test',
    model: { name: 'qwen3-14b', precision: 'W8A8', revision: 'test' },
  },
  generated_at: '2026-11-24T00:05:00+08:00',
}

function workOrder(overrides: Partial<WorkOrder> = {}): WorkOrder {
  return {
    event_id: 'AS06-BAT-01:backup.mains_lost:1795404840000',
    site_id: 'AS06',
    equipment_id: 'AS06-BAT-01',
    risk_type: 'backup_power',
    health_level: 'watch',
    status: 'pending',
    triggered_at: '2026-11-23T16:33:00+08:00',
    ai_result: aiResult,
    review: null,
    updated_at: '2026-11-24T00:05:00+08:00',
    ...overrides,
  }
}

describe('work-order -> maintenance event adapter', () => {
  it('maps a backup work-order copy into the maintenance event shape', () => {
    const event = workOrderToEvent(
      workOrder({
        status: 'confirmed',
        review: {
          outcome: 'accurate',
          comment: '现场核查一致',
          reviewed_at: '2026-11-24T10:00:00+08:00',
          updated_at: '2026-11-24T10:05:00+08:00',
          archived_at: '2026-11-24T10:00:00+08:00',
        },
      }),
      indexStations(stations),
    )

    expect(event.equipmentType).toBe('backup_power')
    expect(event.siteName).toBe('监测站 06')
    expect(event.equipmentName).toBe('AS06-BAT-01')
    expect(event.status).toBe('confirmed')
    expect(event.healthLevel).toBe('watch')
    expect(event.eventSource).toBe('run_trend')
    expect(event.review).toMatchObject({
      outcome: 'accurate',
      comment: '现场核查一致',
      reviewedBy: '现场专家',
    })
    expect(event.aiAnalysis.findings[0]).toMatchObject({
      ruleId: 'BACKUP-RUNTIME-DROP',
      changePercent: -23.9,
    })
    expect(event.aiAnalysis.actions[0]).toMatchObject({
      actionCode: 'backup.check_charging_path',
      priority: 'primary',
      requiresHumanConfirmation: true,
    })
    expect(event.aiAnalysis.dataSnapshot.window.start).toBe(
      '2026-11-23T00:00:00+08:00',
    )
    expect(event.aiAnalysis.versions.model.name).toBe('qwen3-14b')
    expect(event).toHaveProperty('backupTrend')
    expect('detectorTrend' in event).toBe(false)
  })

  it('infers detector equipment for a general work order', () => {
    const event = workOrderToEvent(
      workOrder({
        event_id: 'AS06-DET-01:detector.dose_rate_warning:1795404840000',
        equipment_id: 'AS06-DET-01',
        risk_type: 'general',
        health_level: 'inspect',
      }),
      indexStations(stations),
    )

    expect(event.equipmentType).toBe('detector')
    expect(event.equipmentName).toBe('AS06 碘化钠探测器')
    expect(event).toHaveProperty('detectorTrend')
    expect('backupTrend' in event).toBe(false)
  })

  it('falls back when the work-order copy has no AI result', () => {
    const event = workOrderToEvent(
      workOrder({ ai_result: null, health_level: 'insufficient_data' }),
      indexStations(stations),
    )

    expect(event.aiAnalysis.explanation).toContain('暂无 AI 分析结果')
    expect(event.aiAnalysis.findings).toEqual([])
    expect(event.aiAnalysis.actions).toEqual([])
    expect(event.aiAnalysis.versions.model.name).toBe('未知')
    expect(event.healthLevel).toBe('insufficient_data')
  })
})
