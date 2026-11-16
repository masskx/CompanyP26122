import { createApp, nextTick, type App } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { describe, expect, it, vi } from 'vitest'
import MaintenanceEventDialog from '../src/features/maintenance/components/MaintenanceEventDialog.vue'
import { createDemoEvents } from '../src/features/maintenance/fixtures/demoEvents'
import type { MaintenanceEvent } from '../src/features/maintenance/types'

vi.mock('../src/features/maintenance/components/EChart.vue', () => ({
  default: {
    name: 'EChart',
    props: ['option', 'label', 'height'],
    template: '<div class="echart-stub" role="img" :aria-label="label"></div>',
  },
}))

async function renderDialog(event: MaintenanceEvent) {
  setActivePinia(createPinia())
  const host = document.createElement('div')
  document.body.appendChild(host)

  const app: App = createApp(MaintenanceEventDialog, { event })
  app.mount(host)
  await nextTick()

  return {
    cleanup() {
      app.unmount()
      host.remove()
    },
  }
}

describe('maintenance event trend data', () => {
  it('uses a single health level without a separate analysis status', () => {
    const events = createDemoEvents()
    const healthLevels = new Set(events.map((event) => event.healthLevel))

    expect(healthLevels).toEqual(
      new Set(['watch', 'inspect', 'insufficient_data']),
    )
    expect(events.every((event) => !('analysisStatus' in event))).toBe(true)
  })

  it('keeps detector and backup trends mutually exclusive', () => {
    const events = createDemoEvents()

    expect(events.some((event) => event.equipmentType === 'detector')).toBe(true)
    expect(
      events.some((event) => event.equipmentType === 'backup_power'),
    ).toBe(true)

    for (const event of events) {
      if (event.equipmentType === 'detector') {
        expect(event).toHaveProperty('detectorTrend')
        expect('backupTrend' in event).toBe(false)
      } else {
        expect(event).toHaveProperty('backupTrend')
        expect('detectorTrend' in event).toBe(false)
      }
    }
  })

  it('only renders the backup data panel for a backup event', async () => {
    const event = createDemoEvents().find(
      (item) =>
        item.equipmentType === 'backup_power' &&
        item.eventNumber === 'EV-1015-006',
    )

    expect(event).toBeDefined()
    const dialog = await renderDialog(event!)

    try {
      expect(document.body.textContent).toContain('近 24 小时电量与续航参考')
      expect(document.body.textContent).not.toContain(
        '电离室 · 剂量率与雨量',
      )
    } finally {
      dialog.cleanup()
    }
  })

  it('only renders the detector data panel for a detector event', async () => {
    const event = createDemoEvents().find(
      (item) =>
        item.equipmentType === 'detector' &&
        item.eventNumber === 'EV-1015-005',
    )

    expect(event).toBeDefined()
    const dialog = await renderDialog(event!)

    try {
      expect(document.body.textContent).toContain('电离室 · 剂量率与雨量')
      expect(document.body.textContent).not.toContain(
        '近 24 小时电量与续航参考',
      )
    } finally {
      dialog.cleanup()
    }
  })
})
