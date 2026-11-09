<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useMaintenanceStore } from '../stores/maintenance'
import {
  equipmentTypeLabels,
  eventSourceLabels,
  riskLevelLabels,
  type AnalysisRecommendation,
  type MaintenanceEvent,
} from '../types'
import { formatDateTime, formatPercent } from '../utils'
import AiFeedbackForm from './AiFeedbackForm.vue'
import BackupDataPanel from './BackupDataPanel.vue'
import DetectorDataPanel from './DetectorDataPanel.vue'
import EventConfirmation from './EventConfirmation.vue'

const props = defineProps<{
  event: MaintenanceEvent
}>()

const maintenance = useMaintenanceStore()
const closeButton = ref<HTMLButtonElement | null>(null)
let previousBodyOverflow = ''

const confidenceLabel = computed(() =>
  formatPercent(props.event.aiAnalysis.confidence),
)

const priorityLabels: Record<AnalysisRecommendation['priority'], string> = {
  high: '高优先级',
  medium: '中优先级',
  low: '低优先级',
}

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
            <div>
              <h2 id="event-dialog-title">{{ event.eventTitle }}</h2>
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
              aria-label="关闭工单详情"
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

          <div class="event-dialog__badges">
            <span class="risk-tag" :data-risk="event.riskLevel">
              {{ riskLevelLabels[event.riskLevel] }}
            </span>
            <span
              class="status-tag"
              :data-status="event.status"
            >
              {{ event.status === 'pending' ? '待人工确认' : '已确认归档' }}
            </span>
            <span class="confidence-tag">
              AI 置信度 {{ confidenceLabel }}
            </span>
          </div>
        </header>

        <div class="event-dialog__tabs" role="tablist" aria-label="工单详情">
          <button
            type="button"
            role="tab"
            :aria-selected="maintenance.dialogTab === 'analysis'"
            :class="{ active: maintenance.dialogTab === 'analysis' }"
            @click="maintenance.dialogTab = 'analysis'"
          >
            分析详情
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="maintenance.dialogTab === 'confirmation'"
            :class="{ active: maintenance.dialogTab === 'confirmation' }"
            @click="maintenance.dialogTab = 'confirmation'"
          >
            人工确认
          </button>
        </div>

        <div class="event-dialog__body">
          <div
            v-if="maintenance.dialogTab === 'analysis'"
            class="analysis-view"
            role="tabpanel"
          >
            <section class="analysis-section">
              <header class="section-heading">
                <div>
                  <span>DATABASE EVIDENCE</span>
                  <h3>设备运行数据</h3>
                  <p>
                    以下趋势数据由演示数据提供，接入后读取 MES 数据库真实记录。
                  </p>
                </div>
              </header>

              <div
                class="data-panels"
                :data-primary="event.equipmentType"
              >
                <DetectorDataPanel
                  class="data-panels__detector"
                  :event="event"
                />
                <BackupDataPanel
                  class="data-panels__backup"
                  :event="event"
                />
              </div>
            </section>

            <section class="analysis-section">
              <header class="section-heading">
                <div>
                  <span>AI ASSESSMENT</span>
                  <h3>AI 综合分析</h3>
                  <p>结论基于设备数据和维护记录生成，仅供人工核查参考。</p>
                </div>
                <span class="ai-model-tag">
                  {{ event.aiAnalysis.modelName }} ·
                  {{ event.aiAnalysis.modelPrecision }}
                </span>
              </header>

              <div class="ai-summary">
                <span aria-hidden="true">AI</span>
                <div>
                  <h4>综合结论</h4>
                  <p>{{ event.aiAnalysis.summary }}</p>
                </div>
              </div>

              <div class="ai-detail-grid">
                <section class="ai-detail-card">
                  <h4>支持证据</h4>
                  <ul class="evidence-list">
                    <li
                      v-for="evidence in event.aiAnalysis.evidence"
                      :key="`${evidence.source}-${evidence.metric}`"
                    >
                      <span>{{ evidence.source }} · {{ evidence.timeRange }}</span>
                      <b>{{ evidence.metric }}</b>
                      <p>{{ evidence.observation }}</p>
                    </li>
                  </ul>
                </section>

                <section class="ai-detail-card ai-detail-card--gap">
                  <h4>数据缺口</h4>
                  <ul class="gap-list">
                    <li
                      v-for="gap in event.aiAnalysis.dataGaps"
                      :key="gap"
                    >
                      {{ gap }}
                    </li>
                  </ul>
                </section>
              </div>

              <section class="recommendations">
                <h4>维护建议</h4>
                <ol>
                  <li
                    v-for="recommendation in event.aiAnalysis.recommendations"
                    :key="recommendation.action"
                  >
                    <span
                      class="priority-tag"
                      :data-priority="recommendation.priority"
                    >
                      {{ priorityLabels[recommendation.priority] }}
                    </span>
                    <div>
                      <b>{{ recommendation.action }}</b>
                      <p>{{ recommendation.rationale }}</p>
                    </div>
                  </li>
                </ol>
              </section>

              <section class="follow-up">
                <h4>后续复核</h4>
                <p>{{ event.aiAnalysis.followUp }}</p>
              </section>

              <footer class="ai-meta">
                <span>
                  分析生成时间
                  {{ formatDateTime(event.aiAnalysis.generatedAt) }}
                </span>
                <span>模型 {{ event.aiAnalysis.modelName }}</span>
                <span>置信度 {{ confidenceLabel }}</span>
              </footer>
            </section>

            <AiFeedbackForm :event="event" />
          </div>

          <div
            v-else
            class="confirmation-view"
            role="tabpanel"
          >
            <EventConfirmation :event="event" />
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
  padding: 24px;
  background: rgb(4 25 43 / 54%);
  backdrop-filter: blur(2px);
}

.event-dialog {
  display: flex;
  width: min(1120px, 100%);
  max-height: min(900px, calc(100vh - 48px));
  flex-direction: column;
  overflow: hidden;
  border: 1px solid rgb(255 255 255 / 42%);
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 30px 90px rgb(0 16 31 / 35%);
}

.event-dialog__header {
  flex: none;
  padding: 20px 25px 14px;
  border-bottom: 1px solid #e5ecf2;
  background: #fff;
}

.event-dialog__eyebrow {
  display: flex;
  align-items: center;
  gap: 9px;
  color: #8b9dab;
  font-size: 10px;
}

.event-dialog__eyebrow i {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: #a8b5bf;
}

.event-dialog__title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  margin-top: 7px;
}

.event-dialog__title-row h2 {
  margin: 0;
  color: #203f58;
  font-size: 21px;
  font-weight: 600;
  letter-spacing: 0.15px;
}

.event-dialog__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 22px;
  margin-top: 12px;
  color: #506c82;
  font-size: 11px;
}

.event-dialog__meta span {
  display: inline-flex;
  align-items: baseline;
  gap: 7px;
}

.event-dialog__meta small {
  color: #93a2af;
  font-size: 10px;
}

.event-dialog__close {
  display: grid;
  width: 34px;
  height: 34px;
  flex: none;
  place-items: center;
  border-radius: 7px;
  background: transparent;
  color: #8295a5;
}

.event-dialog__close:hover {
  background: #f0f4f7;
  color: #46667f;
}

.event-dialog__close svg {
  width: 22px;
  height: 22px;
}

.event-dialog__badges {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 13px;
}

.risk-tag,
.status-tag,
.confidence-tag,
.ai-model-tag {
  display: inline-flex;
  padding: 4px 8px;
  border-radius: 4px;
  background: #fff6e6;
  color: #9a6918;
  font-size: 10px;
  line-height: 1.4;
}

.risk-tag[data-risk="inspect"] {
  background: #fff1e9;
  color: #ad592d;
}

.risk-tag[data-risk="insufficient_data"] {
  background: #f0f3f6;
  color: #6f8191;
}

.status-tag {
  background: #edf6f2;
  color: #3c816a;
}

.status-tag[data-status="pending"] {
  background: #fff5e8;
  color: #9f6b16;
}

.confidence-tag,
.ai-model-tag {
  background: #edf4fb;
  color: #4f7493;
}

.event-dialog__tabs {
  display: flex;
  flex: none;
  gap: 27px;
  padding: 0 25px;
  border-bottom: 1px solid #e4ebf1;
  background: #fff;
}

.event-dialog__tabs button {
  position: relative;
  padding: 13px 1px 12px;
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: #70879a;
  font-size: 12px;
}

.event-dialog__tabs button:hover {
  color: #176cc0;
}

.event-dialog__tabs button.active {
  border-bottom-color: #176cc0;
  color: #176cc0;
  font-weight: 600;
}

.event-dialog__body {
  overflow-y: auto;
  padding: 20px 25px 28px;
  background: #f6f9fc;
  scrollbar-color: #c5d3df transparent;
  scrollbar-width: thin;
}

.analysis-view {
  display: grid;
  gap: 18px;
}

.analysis-section {
  display: grid;
  gap: 14px;
}

.section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 1px 1px 0;
}

.section-heading > div > span {
  color: #8ba0b0;
  font-size: 9px;
  letter-spacing: 1.45px;
}

.section-heading h3 {
  margin: 5px 0 5px;
  color: #294e6a;
  font-size: 17px;
  font-weight: 600;
}

.section-heading p {
  margin: 0;
  color: #8296a6;
  font-size: 11px;
}

.data-panels {
  display: grid;
  gap: 14px;
}

.data-panels[data-primary="backup_power"] .data-panels__backup {
  order: -1;
}

.ai-summary {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 17px 18px;
  border: 1px solid #cfe0ec;
  border-left: 3px solid #2c7cc3;
  border-radius: 7px;
  background: #f4f9fd;
}

.ai-summary > span {
  display: grid;
  width: 34px;
  height: 34px;
  flex: none;
  place-items: center;
  border-radius: 9px;
  background: #dbeaf7;
  color: #1763a4;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.6px;
}

.ai-summary h4,
.ai-detail-card h4,
.recommendations h4,
.follow-up h4 {
  margin: 0;
  color: #345a76;
  font-size: 13px;
  font-weight: 600;
}

.ai-summary p {
  margin: 8px 0 0;
  color: #526f83;
  font-size: 12px;
  line-height: 1.85;
}

.ai-detail-grid {
  display: grid;
  grid-template-columns: 1.5fr 1fr;
  gap: 14px;
}

.ai-detail-card {
  padding: 17px 18px;
  border: 1px solid #dce7ef;
  border-radius: 7px;
  background: #fff;
}

.ai-detail-card--gap {
  background: #fffaf1;
  border-color: #eee1c8;
}

.evidence-list,
.gap-list {
  display: grid;
  gap: 12px;
  margin: 13px 0 0;
  padding: 0;
  list-style: none;
}

.evidence-list li {
  padding-left: 12px;
  border-left: 2px solid #d9e8f3;
}

.evidence-list span {
  display: block;
  color: #91a2b0;
  font-size: 9px;
}

.evidence-list b {
  display: block;
  margin-top: 4px;
  color: #41627b;
  font-size: 11px;
  font-weight: 600;
}

.evidence-list p,
.gap-list li {
  margin: 5px 0 0;
  color: #60798c;
  font-size: 11px;
  line-height: 1.7;
}

.gap-list li {
  position: relative;
  margin: 0;
  padding-left: 14px;
}

.gap-list li::before {
  position: absolute;
  top: 0.72em;
  left: 0;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #c38c31;
  content: "";
}

.recommendations {
  padding: 17px 18px;
  border: 1px solid #dce7ef;
  border-radius: 7px;
  background: #fff;
}

.recommendations ol {
  display: grid;
  gap: 12px;
  margin: 13px 0 0;
  padding: 0;
  list-style: none;
}

.recommendations li {
  display: flex;
  align-items: flex-start;
  gap: 11px;
}

.priority-tag {
  flex: none;
  padding: 3px 7px;
  border-radius: 4px;
  background: #edf3f7;
  color: #667e90;
  font-size: 9px;
}

.priority-tag[data-priority="high"] {
  background: #fff0f2;
  color: #b04b59;
}

.priority-tag[data-priority="medium"] {
  background: #fff6e6;
  color: #9d6915;
}

.recommendations b {
  color: #405f76;
  font-size: 11px;
  font-weight: 600;
}

.recommendations p {
  margin: 5px 0 0;
  color: #73899a;
  font-size: 11px;
  line-height: 1.65;
}

.follow-up {
  padding: 15px 18px;
  border: 1px solid #dce7ef;
  border-radius: 7px;
  background: #fff;
}

.follow-up p {
  margin: 8px 0 0;
  color: #627b8e;
  font-size: 11px;
  line-height: 1.8;
}

.ai-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 22px;
  padding: 0 2px;
  color: #91a1ae;
  font-size: 9px;
}

.confirmation-view {
  padding: 5px 0;
}

@media (max-width: 800px) {
  .event-dialog-backdrop {
    padding: 10px;
  }

  .event-dialog {
    max-height: calc(100vh - 20px);
  }

  .event-dialog__header,
  .event-dialog__tabs,
  .event-dialog__body {
    padding-right: 17px;
    padding-left: 17px;
  }

  .event-dialog__title-row h2 {
    font-size: 18px;
  }

  .ai-detail-grid {
    grid-template-columns: 1fr;
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
    gap: 5px;
  }

  .section-heading {
    flex-direction: column;
    gap: 9px;
  }

  .ai-summary {
    padding: 15px;
  }
}
</style>
