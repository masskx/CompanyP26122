<script setup lang="ts">
import { computed } from 'vue'
import { useMaintenanceStore } from '../stores/maintenance'
import {
  aiFeedbackLabels,
  eventSourceLabels,
  riskLevelLabels,
  type MaintenanceEvent,
} from '../types'
import { formatDateTime } from '../utils'

const maintenance = useMaintenanceStore()

const listTitle = computed(() =>
  maintenance.activeTab === 'pending'
    ? '事件分析清单 · 待人工确认'
    : '已确认事件记录',
)

function viewEvent(event: MaintenanceEvent) {
  maintenance.openEvent(event.id, 'analysis')
}

function confirmEvent(event: MaintenanceEvent) {
  maintenance.openEvent(event.id, 'confirmation')
}
</script>

<template>
  <section class="event-list" aria-labelledby="event-list-title">
    <header class="event-list__header">
      <div>
        <h2 id="event-list-title">{{ listTitle }}</h2>
        <p>
          {{
            maintenance.activeTab === 'pending'
              ? 'AI 分析结果供人工核查，确认后进入归档记录'
              : '保留人工确认意见、AI 结论评价和确认时间'
          }}
        </p>
      </div>
      <span aria-live="polite">共 {{ maintenance.filteredEvents.length }} 条</span>
    </header>

    <div v-if="maintenance.filteredEvents.length > 0" class="event-table-wrap">
      <table class="event-table">
        <thead>
          <tr>
            <th>事件编号 / 触发时间</th>
            <th>站点 / 设备</th>
            <th>事件 / 来源</th>
            <th>AI 分析摘要</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="event in maintenance.filteredEvents"
            :key="event.id"
            :data-status="event.status"
          >
            <td data-label="事件编号 / 触发时间">
              <strong class="event-number">{{ event.eventNumber }}</strong>
              <time :datetime="event.triggeredAt">
                {{ formatDateTime(event.triggeredAt) }}
              </time>
            </td>
            <td data-label="站点 / 设备">
              <div class="site-cell">
                <strong>{{ event.siteId }}</strong>
                <span>{{ event.siteName }}</span>
                <small>{{ event.equipmentName }}</small>
              </div>
            </td>
            <td data-label="事件 / 来源">
              <div class="event-source">
                <strong>{{ event.eventTitle }}</strong>
                <span>{{ eventSourceLabels[event.eventSource] }}</span>
              </div>
            </td>
            <td data-label="AI 分析摘要">
              <div class="analysis-cell">
                <div class="analysis-cell__meta">
                  <span
                    class="risk-tag"
                    :data-risk="event.riskLevel"
                  >
                    {{ riskLevelLabels[event.riskLevel] }}
                  </span>
                  <span
                    v-if="event.aiFeedback"
                    class="feedback-tag"
                    :data-rating="event.aiFeedback.rating"
                  >
                    AI 评价 · {{ aiFeedbackLabels[event.aiFeedback.rating] }}
                  </span>
                </div>
                <p>
                  {{
                    event.status === 'confirmed'
                      ? event.confirmation?.comment || event.aiAnalysis.summary
                      : event.aiAnalysis.summary
                  }}
                </p>
                <small v-if="event.confirmation">
                  {{ formatDateTime(event.confirmation.confirmedAt) }} ·
                  {{ event.confirmation.confirmedBy }}
                </small>
              </div>
            </td>
            <td data-label="操作">
              <div class="event-actions">
                <button
                  type="button"
                  class="event-actions__view"
                  @click="viewEvent(event)"
                >
                  {{ event.status === 'pending' ? '查看分析' : '查看记录' }}
                </button>
                <button
                  v-if="event.status === 'pending'"
                  type="button"
                  class="event-actions__confirm"
                  @click="confirmEvent(event)"
                >
                  人工确认
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else class="event-list__empty">
      <span class="event-list__empty-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none">
          <path
            d="M5 7.5H19M5 12H15.5M5 16.5H12"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linecap="round"
          />
          <circle
            cx="18"
            cy="17"
            r="3.2"
            stroke="currentColor"
            stroke-width="1.7"
          />
        </svg>
      </span>
      <h3>
        {{
          maintenance.filters.keyword ||
          maintenance.filters.equipmentType !== 'all'
            ? '未找到匹配的工单'
            : maintenance.activeTab === 'pending'
              ? '暂无待确认工单'
              : '暂无已确认记录'
        }}
      </h3>
      <p>
        {{
          maintenance.filters.keyword ||
          maintenance.filters.equipmentType !== 'all'
            ? '可调整设备类型或搜索条件后重试。'
            : '后端检测到异常后，新工单将显示在这里。'
        }}
      </p>
    </div>
  </section>
</template>

<style scoped>
.event-list {
  overflow: hidden;
  border: 1px solid #dce6ee;
  border-radius: 9px;
  background: #fff;
  box-shadow: 0 10px 30px rgb(24 69 101 / 6%);
}

.event-list__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 19px 22px 17px;
  border-bottom: 1px solid #e4ebf1;
}

.event-list__header h2 {
  margin: 0;
  color: #2a506d;
  font-size: 16px;
  font-weight: 600;
}

.event-list__header p {
  margin: 6px 0 0;
  color: #8a9baa;
  font-size: 11px;
}

.event-list__header > span {
  flex: none;
  color: #95a5b2;
  font-size: 12px;
}

.event-table-wrap {
  overflow-x: auto;
}

.event-table {
  width: 100%;
  min-width: 980px;
  border-collapse: collapse;
  table-layout: fixed;
}

.event-table th {
  padding: 13px 17px;
  background: #f5f8fb;
  color: #7890a3;
  font-size: 11px;
  font-weight: 500;
  text-align: left;
}

.event-table th:nth-child(1) {
  width: 18%;
}

.event-table th:nth-child(2) {
  width: 15%;
}

.event-table th:nth-child(3) {
  width: 17%;
}

.event-table th:nth-child(4) {
  width: 36%;
}

.event-table th:nth-child(5) {
  width: 14%;
}

.event-table td {
  padding: 18px 17px;
  border-bottom: 1px solid #e9eff4;
  color: #526d82;
  font-size: 12px;
  vertical-align: top;
}

.event-table tbody tr:last-child td {
  border-bottom: 0;
}

.event-table tbody tr {
  transition: background-color 140ms ease;
}

.event-table tbody tr:hover {
  background: #f9fcff;
}

.event-number {
  display: block;
  color: #284f6e;
  font-size: 13px;
  font-weight: 600;
}

.event-table time {
  display: block;
  margin-top: 6px;
  color: #8b9dac;
  font-size: 10px;
  white-space: nowrap;
}

.site-cell strong {
  display: inline-block;
  color: #365c78;
  font-size: 13px;
}

.site-cell span,
.site-cell small {
  display: block;
  overflow: hidden;
  margin-top: 4px;
  color: #8195a5;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.site-cell small {
  color: #97a6b2;
}

.event-source strong {
  display: block;
  color: #3e5e77;
  font-size: 12px;
  font-weight: 500;
  line-height: 1.6;
}

.event-source span {
  display: block;
  margin-top: 5px;
  color: #8b9dac;
  font-size: 10px;
}

.analysis-cell__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.risk-tag,
.feedback-tag {
  display: inline-flex;
  padding: 3px 7px;
  border-radius: 4px;
  background: #fff6e6;
  color: #a16b14;
  font-size: 10px;
  line-height: 1.4;
  white-space: nowrap;
}

.risk-tag[data-risk="inspect"] {
  background: #fff1e9;
  color: #b85d2e;
}

.risk-tag[data-risk="insufficient_data"] {
  background: #f0f3f6;
  color: #748494;
}

.feedback-tag {
  background: #edf7f3;
  color: #397965;
}

.feedback-tag[data-rating="needs_revision"] {
  background: #fff0f2;
  color: #b34e5e;
}

.analysis-cell p {
  display: -webkit-box;
  overflow: hidden;
  margin: 8px 0 0;
  color: #526c80;
  font-size: 12px;
  line-height: 1.7;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.analysis-cell small {
  display: block;
  margin-top: 7px;
  color: #98a7b3;
  font-size: 10px;
}

.event-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.event-actions button {
  min-height: 32px;
  padding: 0 11px;
  border-radius: 5px;
  font-size: 11px;
  white-space: nowrap;
}

.event-actions__view {
  border: 1px solid transparent;
  background: transparent;
  color: #176cc0;
}

.event-actions__view:hover {
  background: #edf5fd;
}

.event-actions__confirm {
  border: 1px solid #a8c4da;
  background: #fff;
  color: #3e6a8d;
}

.event-actions__confirm:hover {
  border-color: #6fa2c7;
  background: #f8fbfd;
}

.event-list__empty {
  display: flex;
  min-height: 330px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 45px 24px;
  text-align: center;
}

.event-list__empty-icon {
  display: grid;
  width: 58px;
  height: 58px;
  place-items: center;
  border-radius: 18px;
  background: #edf5fb;
  color: #5b8bb1;
}

.event-list__empty-icon svg {
  width: 29px;
  height: 29px;
}

.event-list__empty h3 {
  margin: 17px 0 7px;
  color: #3b5e78;
  font-size: 15px;
  font-weight: 600;
}

.event-list__empty p {
  margin: 0;
  color: #8a9dac;
  font-size: 12px;
}

@media (max-width: 960px) {
  .event-table-wrap {
    overflow: visible;
  }

  .event-table {
    min-width: 0;
  }

  .event-table thead {
    display: none;
  }

  .event-table,
  .event-table tbody,
  .event-table tr,
  .event-table td {
    display: block;
    width: 100%;
  }

  .event-table tr {
    padding: 17px 18px;
    border-bottom: 1px solid #e5ecf2;
  }

  .event-table tr:last-child {
    border-bottom: 0;
  }

  .event-table td {
    display: grid;
    grid-template-columns: 116px minmax(0, 1fr);
    gap: 13px;
    padding: 7px 0;
    border: 0;
  }

  .event-table td::before {
    content: attr(data-label);
    color: #8b9dad;
    font-size: 10px;
  }

  .event-table td:last-child {
    padding-top: 13px;
  }

  .event-actions {
    justify-content: flex-start;
  }
}

@media (max-width: 560px) {
  .event-list__header {
    align-items: flex-start;
    padding: 17px 16px;
  }

  .event-table tr {
    padding: 15px;
  }

  .event-table td {
    grid-template-columns: 1fr;
    gap: 5px;
  }
}
</style>
