import { createApp, nextTick, type App } from 'vue'
import { createPinia } from 'pinia'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { WorkOrder } from '../src/api/modules/maintenance'
import {
  listStations,
  listWorkOrders,
} from '../src/api/modules/maintenance'
import MaintenanceView from '../src/features/maintenance/MaintenanceView.vue'

vi.mock('../src/api/modules/maintenance', () => ({
  listWorkOrders: vi.fn(),
  listStations: vi.fn(),
  putWorkOrderReview: vi.fn(),
  fetchTelemetry: vi.fn(),
}))

vi.mock('../src/features/maintenance/components/EChart.vue', () => ({
  default: {
    name: 'EChart',
    props: ['option', 'label', 'height'],
    template: '<div class="echart-stub" role="img" :aria-label="label"></div>',
  },
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
        message: '参考续航 18.0 小时，较基线下降约 24%',
        source_refs: [],
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

let app: App | null = null
let host: HTMLElement | null = null

afterEach(() => {
  app?.unmount()
  host?.remove()
  app = null
  host = null
})

describe('maintenance view with the work-order API', () => {
  it('renders a real work-order copy in the pending list', async () => {
    vi.mocked(listStations).mockResolvedValue([
      { station_id: 'AS06', name: '监测站 06' },
    ])
    vi.mocked(listWorkOrders).mockResolvedValue([workOrder])

    host = document.createElement('div')
    document.body.appendChild(host)
    app = createApp(MaintenanceView)
    app.use(createPinia())
    app.mount(host)
    await nextTick()

    await vi.waitFor(() => {
      expect(host?.textContent).toContain('参考续航 18.0 小时')
    })

    expect(host?.textContent).toContain('共 1 条')
    expect(host?.textContent).toContain('AS06')
    expect(host?.textContent).toContain('监测站 06')
    expect(host?.textContent).toContain('查看分析')
  })

  it('shows the empty state when the backend has no work-order copies', async () => {
    vi.mocked(listStations).mockResolvedValue([])
    vi.mocked(listWorkOrders).mockResolvedValue([])

    host = document.createElement('div')
    document.body.appendChild(host)
    app = createApp(MaintenanceView)
    app.use(createPinia())
    app.mount(host)
    await nextTick()

    await vi.waitFor(() => {
      expect(host?.textContent).toContain('暂无待确认事件')
    })
  })
})
