<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useMaintenanceStore } from '../stores/maintenance'
import {
  equipmentTypeLabels,
  eventSourceLabels,
  riskLevelLabels,
  type MaintenanceEvent,
} from '../types'
import { formatDateTime } from '../utils'
import AiAnalysisPanel from './AiAnalysisPanel.vue'
import BackupDataPanel from './BackupDataPanel.vue'
import DetectorDataPanel from './DetectorDataPanel.vue'
import EventConfirmation from './EventConfirmation.vue'

const props = defineProps<{
  event: MaintenanceEvent
}>()

const maintenance = useMaintenanceStore()
const closeButton = ref<HTMLButtonElement | null>(null)
let previousBodyOverflow = ''

function handleWindowKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    maintenance.closeEvent()
  }
}

onMounted(async () => {
  previousBodyOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  window.addEventListener('keydown', handleWindowKeydown)
  await nextTick()
  closeButton.value?.focus()
})

onBeforeUnmount(() => {
  document.body.style.overflow = previousBodyOverflow
  window.removeEventListener('keydown', handleWindowKeydown)
})
</script>

<template>
  <Teleport to="body">
    <div class="event-dialog-backdrop">
      <section
        class="event-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="event-dialog-title"
      >
        <header class="event-dialog__header">
          <div class="event-dialog__eyebrow">
            <span>{{ event.eventNumber }}</span>
            <i aria-hidden="true"></i>
            <span>{{ event.siteId }} · {{ event.siteName }}</span>
            <i aria-hidden="true"></i>
            <span>{{ equipmentTypeLabels[event.equipmentType] }}</span>
          </div>
          <div class="event-dialog__title-row">
            <div class="event-dialog__heading-main">
              <div class="event-dialog__title-line">
                <h2 id="event-dialog-title">{{ event.eventTitle }}</h2>
                <div class="event-dialog__badges">
                  <span class="risk-tag" :data-risk="event.riskLevel">
                    {{ riskLevelLabels[event.riskLevel] }}
                  </span>
                  <span class="status-tag" :data-status="event.status">
                    {{
                      event.status === 'pending'
                        ? '待人工审核'
                        : '已审核归档'
                    }}
                  </span>
                </div>
              </div>
              <div class="event-dialog__meta">
                <span>
                  <small>触发时间</small>
                  <time :datetime="event.triggeredAt">
                    {{ formatDateTime(event.triggeredAt) }}
                  </time>
                </span>
                <span>
                  <small>事件来源</small>
                  {{ eventSourceLabels[event.eventSource] }}
                </span>
                <span>
                  <small>设备</small>
                  {{ event.equipmentName }}
                </span>
              </div>
            </div>
            <button
              ref="closeButton"
              type="button"
              class="event-dialog__close"
              aria-label="关闭事件详情"
              @click="maintenance.closeEvent"
            >
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M7 7L17 17M17 7L7 17"
                  stroke="currentColor"
                  stroke-width="1.8"
                  stroke-linecap="round"
                />
              </svg>
            </button>
          </div>
        </header>

        <div class="event-dialog__body">
          <div class="analysis-view">
            <section class="analysis-section">
              <div class="data-panels">
                <BackupDataPanel
                  class="data-panels__backup"
                  :event="event"
                />
                <DetectorDataPanel
                  class="data-panels__detector"
                  :event="event"
                />
              </div>
            </section>

            <div class="analysis-workspace" :data-status="event.status">
              <section class="analysis-section analysis-section--ai">
                <header class="section-heading section-heading--ai">
                  <div>
                    <p class="section-kicker">分析结论</p>
                    <h3 id="ai-analysis-title">AI 综合分析</h3>
                  </div>
                  <div class="ai-inline-meta" aria-label="分析信息">
                    <span>{{ event.aiAnalysis.versions.model.name }}</span>
                    <span>
                      {{ formatDateTime(event.aiAnalysis.generatedAt) }}
                    </span>
                  </div>
                </header>

                <AiAnalysisPanel
                  class="ai-analysis-body"
                  :analysis="event.aiAnalysis"
                />
              </section>

              <div
                class="confirmation-column"
                :class="{
                  'confirmation-column--sticky': event.status === 'pending',
                }"
              >
                <EventConfirmation :event="event" />
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.event-dialog-backdrop {
  position: fixed;
  z-index: 70;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgb(4 25 43 / 56%);
  backdrop-filter: blur(3px);
}

.event-dialog {
  display: flex;
  width: 1080px;
  max-width: 100%;
  max-height: min(920px, calc(100vh - 40px));
  flex-direction: column;
  overflow: hidden;
  border: 1px solid rgb(255 255 255 / 48%);
  border-radius: 14px;
  background: #fff;
  box-shadow: 0 30px 90px rgb(0 16 31 / 36%);
}

.event-dialog__header {
  position: relative;
  flex: none;
  padding: 13px 68px 11px 22px;
  border-bottom: 1px solid #e2eaf0;
  background: #fff;
}

.event-dialog__eyebrow {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 9px;
  color: #60788b;
  font-size: 12px;
  line-height: 1.5;
}

.event-dialog__eyebrow i {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: #9cabb7;
}

.event-dialog__title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-top: 5px;
  padding-right: 0;
}

.event-dialog__title-row h2 {
  margin: 0;
  color: #173e5c;
  font-size: 22px;
  font-weight: 680;
  letter-spacing: 0.1px;
  line-height: 1.4;
}

.event-dialog__heading-main {
  min-width: 0;
}

.event-dialog__title-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
}

.event-dialog__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  margin-top: 7px;
  color: #405f76;
  font-size: 12px;
}

.event-dialog__meta span {
  display: inline-flex;
  align-items: baseline;
  gap: 7px;
}

.event-dialog__meta small {
  color: #65798b;
  font-size: 12px;
}

.event-dialog__close {
  position: absolute;
  top: 12px;
  right: 16px;
  display: grid;
  width: 40px;
  height: 40px;
  flex: none;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: #65798b;
  line-height: 0;
}

.event-dialog__close:hover {
  background: #eef3f7;
  color: #355a74;
}

.event-dialog__close svg {
  display: block;
  width: 21px;
  height: 21px;
}

.event-dialog__badges {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 0;
}

.risk-tag,
.status-tag {
  display: inline-flex;
  padding: 5px 9px;
  border-radius: 6px;
  background: #fff4dd;
  color: #94600b;
  font-size: 12px;
  line-height: 1.4;
}

.risk-tag[data-risk="inspect"] {
  background: #fff0e9;
  color: #ad5628;
}

.risk-tag[data-risk="insufficient_data"] {
  background: #eef2f5;
  color: #5f7283;
}

.status-tag {
  background: #eaf6f1;
  color: #2f765d;
}

.status-tag[data-status="pending"] {
  background: #fff5e8;
  color: #94600b;
}

.event-dialog__body {
  overflow-y: auto;
  padding: 16px 20px 18px;
  background: #f4f8fb;
  scrollbar-color: #b9cbd8 transparent;
  scrollbar-width: thin;
}

.analysis-view {
  display: grid;
  gap: 12px;
}

.analysis-section {
  display: grid;
  gap: 8px;
}

.section-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding: 0 2px;
}

.section-heading > div {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 10px;
}

.section-kicker {
  margin: 0;
  color: #60788b;
  font-size: 12px;
  font-weight: 500;
  line-height: 1.4;
}

.section-heading h3 {
  margin: 0;
  color: #244b69;
  font-size: 17px;
  font-weight: 650;
  line-height: 1.4;
}

.section-heading--ai {
  align-items: center;
}

.ai-inline-meta {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}

.ai-inline-meta span {
  padding: 5px 8px;
  border: 1px solid #dce8f1;
  border-radius: 6px;
  background: #f4f8fb;
  color: #536b80;
  font-size: 12px;
  line-height: 1.4;
  white-space: nowrap;
}

.data-panels {
  display: grid;
  grid-template-columns: minmax(300px, 0.62fr) minmax(0, 1.38fr);
  gap: 14px;
  align-items: stretch;
}

.analysis-workspace {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(360px, 0.85fr);
  gap: 16px;
  align-items: stretch;
}

.analysis-section--ai,
.confirmation-column {
  min-width: 0;
  padding: 16px;
  border: 1px solid #dce7ef;
  border-radius: 12px;
  background: #fff;
}

.analysis-section--ai {
  display: flex;
  height: 100%;
  min-height: 0;
  flex-direction: column;
  gap: 10px;
}

.confirmation-column {
  display: flex;
  height: 100%;
  min-height: 0;
}

.confirmation-column--sticky {
  position: sticky;
  top: 0;
  max-height: calc(100vh - 180px);
}

.confirmation-column > * {
  width: 100%;
}

.ai-analysis-body {
  min-height: 0;
  flex: 1;
}

@media (max-width: 1080px) {
  .analysis-workspace {
    grid-template-columns: 1fr;
  }

  .analysis-section--ai,
  .confirmation-column {
    height: auto;
    position: static;
    max-height: none;
  }

  .ai-analysis-body {
    flex: none;
  }
}

@media (max-width: 1023px) {
  .data-panels {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 800px) {
  .event-dialog-backdrop {
    padding: 10px;
  }

  .event-dialog {
    max-height: calc(100vh - 20px);
  }

  .event-dialog__header,
  .event-dialog__body {
    padding-right: 17px;
    padding-left: 17px;
  }

  .event-dialog__header {
    padding-right: 58px;
  }

  .event-dialog__title-row h2 {
    font-size: 20px;
  }
}

@media (max-width: 560px) {
  .event-dialog-backdrop {
    place-items: stretch;
    padding: 0;
  }

  .event-dialog {
    width: 100%;
    max-height: 100dvh;
    border-radius: 0;
  }

  .event-dialog__meta {
    align-items: flex-start;
    flex-direction: column;
    gap: 6px;
  }

  .section-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 9px;
  }

  .analysis-section--ai,
  .confirmation-column {
    padding: 15px;
  }

}
</style>
