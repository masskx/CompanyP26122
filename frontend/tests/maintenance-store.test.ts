import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { WorkOrder } from '../src/api/modules/maintenance'
import {
  listStations,
  listWorkOrders,
  putWorkOrderReview,
} from '../src/api/modules/maintenance'
import { useMaintenanceStore } from '../src/features/maintenance/stores/maintenance'

vi.mock('../src/api/modules/maintenance', () => ({
  listWorkOrders: vi.fn(),
  listStations: vi.fn(),
  putWorkOrderReview: vi.fn(),
  fetchTelemetry: vi.fn(),
}))

const workOrder: WorkOrder = {
  event_id: 'AS06-BAT-01:backup.mains_lost:1795404840000',
  site_id: 'AS06',
  equipment_id: 'AS06-BAT-01',
  risk_type: 'backup_power',
  health_level: 'watch',
  status: 'pending',
  triggered_at: '2026-11-23T16:33:00+08:00',
  ai_result: {
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
        message: '参考续航下降',
        source_refs: [],
      },
    ],
    actions: [],
    limitations: [],
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
      rule_set: 'rules',
      action_catalog: 'actions',
      prompt: 'prompt',
      model: { name: 'qwen3-14b', precision: 'W8A8', revision: 'test' },
    },
    generated_at: '2026-11-24T00:05:00+08:00',
  },
  review: null,
}

function reviewed(overrides: Partial<WorkOrder> = {}): WorkOrder {
  return {
    ...workOrder,
    status: 'confirmed',
    updated_at: '2026-11-24T10:00:00+08:00',
    review: {
      outcome: 'accurate',
      comment: '现场核查一致',
      reviewed_at: '2026-11-24T10:00:00+08:00',
      updated_at: '2026-11-24T10:00:00+08:00',
      archived_at: '2026-11-24T10:00:00+08:00',
    },
    ...overrides,
  }
}

describe('maintenance store with work-order API', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(listWorkOrders).mockReset()
    vi.mocked(listStations).mockReset()
    vi.mocked(putWorkOrderReview).mockReset()
  })

  it('loads work-order copies as events and submits a review', async () => {
    vi.mocked(listStations).mockResolvedValue([
      { station_id: 'AS06', name: '监测站 06' },
    ])
    vi.mocked(listWorkOrders).mockResolvedValue([workOrder])
    vi.mocked(putWorkOrderReview).mockResolvedValue(reviewed())

    const store = useMaintenanceStore()
    await store.loadEvents()

    expect(store.events).toHaveLength(1)
    expect(store.events[0]).toMatchObject({
      id: workOrder.event_id,
      siteName: '监测站 06',
      equipmentType: 'backup_power',
      status: 'pending',
      healthLevel: 'watch',
    })
    expect(store.pendingCount).toBe(1)
    expect(store.confirmedCount).toBe(0)

    await expect(
      store.submitReview(workOrder.event_id, 'accurate', ' 现场核查一致 '),
    ).resolves.toBe(true)

    expect(putWorkOrderReview).toHaveBeenCalledWith(workOrder.event_id, {
      outcome: 'accurate',
      comment: '现场核查一致',
    })
    expect(store.events[0]?.status).toBe('confirmed')
    expect(store.events[0]?.review).toMatchObject({
      outcome: 'accurate',
      comment: '现场核查一致',
      reviewedBy: '现场专家',
    })
    expect(store.pendingCount).toBe(0)
    expect(store.confirmedCount).toBe(1)
    expect(store.selectedEventId).toBeNull()
  })

  it('updates an archived review through the same endpoint', async () => {
    vi.mocked(listStations).mockResolvedValue([
      { station_id: 'AS06', name: '监测站 06' },
    ])
    vi.mocked(listWorkOrders).mockResolvedValue([reviewed()])
    vi.mocked(putWorkOrderReview).mockResolvedValue(
      reviewed({
        review: {
          outcome: 'needs_revision',
          comment: '补充：充电模块待复核',
          reviewed_at: '2026-11-24T15:00:00+08:00',
          updated_at: '2026-11-24T15:00:00+08:00',
          archived_at: '2026-11-24T10:00:00+08:00',
        },
      }),
    )

    const store = useMaintenanceStore()
    await store.loadEvents()

    await expect(
      store.updateReview(
        workOrder.event_id,
        'needs_revision',
        '补充：充电模块待复核',
      ),
    ).resolves.toBe(true)

    expect(putWorkOrderReview).toHaveBeenCalledWith(workOrder.event_id, {
      outcome: 'needs_revision',
      comment: '补充：充电模块待复核',
    })
    expect(store.events[0]?.review).toMatchObject({
      outcome: 'needs_revision',
      comment: '补充：充电模块待复核',
      reviewedBy: '现场专家',
    })
  })

  it('shows a readable error when the backend is unavailable', async () => {
    vi.mocked(listStations).mockResolvedValue([])
    vi.mocked(listWorkOrders).mockRejectedValue(new Error('Network Error'))

    const store = useMaintenanceStore()
    await store.loadEvents()

    expect(store.events).toEqual([])
    expect(store.eventsError).toContain('Network Error')
    expect(store.isLoadingEvents).toBe(false)
  })
})
