<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useMaintenanceStore } from '../stores/maintenance'
import {
  eventSourceLabels,
  reviewOutcomeLabels,
  riskLevelLabels,
  type MaintenanceEvent,
} from '../types'
import { formatDateTime } from '../utils'

const PAGE_SIZE = 8

const maintenance = useMaintenanceStore()
const currentPage = ref(1)

const listTitle = computed(() =>
  maintenance.activeTab === 'pending'
    ? '事件分析清单 · 待人工审核'
    : '已确认事件记录',
)

const hasActiveFilters = computed(
  () =>
    Boolean(maintenance.filters.keyword) ||
    maintenance.filters.siteId !== 'all' ||
    maintenance.filters.equipmentType !== 'all',
)

const pageCount = computed(() =>
  Math.max(1, Math.ceil(maintenance.filteredEvents.length / PAGE_SIZE)),
)

const pageStart = computed(() =>
  maintenance.filteredEvents.length === 0
    ? 0
    : (currentPage.value - 1) * PAGE_SIZE + 1,
)

const pageEnd = computed(() =>
  Math.min(
    currentPage.value * PAGE_SIZE,
    maintenance.filteredEvents.length,
  ),
)

const paginatedEvents = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return maintenance.filteredEvents.slice(start, start + PAGE_SIZE)
})

const pageNumbers = computed(() =>
  Array.from({ length: pageCount.value }, (_, index) => index + 1),
)

watch(
  () => [
    maintenance.activeTab,
    maintenance.filters.siteId,
    maintenance.filters.equipmentType,
    maintenance.filters.keyword,
  ],
  () => {
    currentPage.value = 1
  },
)

watch(pageCount, (count) => {
  if (currentPage.value > count) {
    currentPage.value = count
  }
})

function viewEvent(event: MaintenanceEvent) {
  maintenance.openEvent(event.id)
}

function setPage(page: number) {
  currentPage.value = Math.min(pageCount.value, Math.max(1, page))
}
</script>

<template>
  <section
    class="event-list"
    :data-tab="maintenance.activeTab"
    aria-labelledby="event-list-title"
  >
    <header class="event-list__header">
      <div>
        <h2 id="event-list-title">{{ listTitle }}</h2>
        <p>
          {{
            maintenance.activeTab === 'pending'
              ? 'AI 分析结果供人工审核，审核后进入归档记录'
              : '保留人工审核结论、意见和审核时间'
          }}
        </p>
      </div>
      <span aria-live="polite">共 {{ maintenance.filteredEvents.length }} 条</span>
    </header>

    <div v-if="maintenance.filteredEvents.length > 0" class="event-table-wrap">
      <table class="event-table">
        <thead>
          <tr>
            <th>事件 / 风险</th>
            <th>站点 / 设备</th>
            <th>AI 分析摘要</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="event in paginatedEvents"
            :key="event.id"
            :data-status="event.status"
          >
            <td data-label="事件 / 风险">
              <div class="event-cell">
                <div class="event-cell__title">
                  <strong>{{ event.eventTitle }}</strong>
                  <span
                    class="risk-tag"
                    :data-risk="event.riskLevel"
                  >
                    {{ riskLevelLabels[event.riskLevel] }}
                  </span>
                </div>
                <div class="event-cell__meta">
                  <span>{{ event.eventNumber }}</span>
                  <i aria-hidden="true"></i>
                  <time :datetime="event.triggeredAt">
                    {{ formatDateTime(event.triggeredAt) }}
                  </time>
                  <i aria-hidden="true"></i>
                  <span>{{ eventSourceLabels[event.eventSource] }}</span>
                </div>
              </div>
            </td>
            <td data-label="站点 / 设备">
              <div class="site-cell">
                <div>
                  <strong>{{ event.siteId }}</strong>
                  <span>{{ event.siteName }}</span>
                </div>
                <small>{{ event.equipmentName }}</small>
              </div>
            </td>
            <td data-label="AI 分析摘要">
              <div class="analysis-cell">
                <span
                  v-if="event.review"
                  class="review-tag"
                  :data-outcome="event.review.outcome"
                >
                  AI 评价 · {{ reviewOutcomeLabels[event.review.outcome] }}
                </span>
                <p>
                  {{
                    event.status === 'confirmed'
                      ? event.review?.comment || event.aiAnalysis.explanation
                      : event.aiAnalysis.explanation
                  }}
                </p>
                <small v-if="event.review">
                  {{
                    event.review.updatedAt
                      ? '最后修改于 ' +
                        formatDateTime(event.review.updatedAt)
                      : formatDateTime(event.review.reviewedAt)
                  }}
                  ·
                  {{ event.review.reviewedBy }}
                </small>
              </div>
            </td>
            <td data-label="操作">
              <div class="event-actions">
                <button
                  type="button"
                  class="event-actions__view"
                  :aria-label="`${event.eventTitle}，${event.status === 'pending' ? '查看分析' : '查看记录'}`"
                  @click="viewEvent(event)"
                >
                  {{ event.status === 'pending' ? '查看分析' : '查看记录' }}
                  <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
                    <path
                      d="M7.5 5L12.5 10L7.5 15"
                      stroke="currentColor"
                      stroke-width="1.7"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    />
                  </svg>
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
          hasActiveFilters
            ? '未找到匹配的事件'
            : maintenance.activeTab === 'pending'
              ? '暂无待确认事件'
              : '暂无已确认记录'
        }}
      </h3>
      <p>
        {{
          hasActiveFilters
            ? '可调整站点、设备类型或搜索条件后重试。'
            : '检测到异常后，新事件将显示在这里。'
        }}
      </p>
    </div>

    <footer
      v-if="maintenance.filteredEvents.length > 0"
      class="event-pagination"
    >
      <p>
        显示
        <strong>{{ pageStart }}–{{ pageEnd }}</strong>
        / 共 {{ maintenance.filteredEvents.length }} 条
      </p>
      <nav aria-label="事件列表分页">
        <button
          type="button"
          class="event-pagination__arrow"
          :disabled="currentPage === 1"
          aria-label="上一页"
          @click="setPage(currentPage - 1)"
        >
          <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <path
              d="M12.5 5L7.5 10L12.5 15"
              stroke="currentColor"
              stroke-width="1.7"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </button>
        <button
          v-for="page in pageNumbers"
          :key="page"
          type="button"
          :class="{ active: currentPage === page }"
          :aria-current="currentPage === page ? 'page' : undefined"
          :aria-label="`第 ${page} 页`"
          @click="setPage(page)"
        >
          {{ page }}
        </button>
        <button
          type="button"
          class="event-pagination__arrow"
          :disabled="currentPage === pageCount"
          aria-label="下一页"
          @click="setPage(currentPage + 1)"
        >
          <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <path
              d="M7.5 5L12.5 10L7.5 15"
              stroke="currentColor"
              stroke-width="1.7"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </button>
      </nav>
    </footer>
  </section>
</template>

<style scoped>
.event-list {
  --event-accent: #176cc0;
  --event-accent-soft: #eaf3fb;
  overflow: hidden;
  border: 1px solid #cddce8;
  border-radius: var(--radius-panel);
  background: linear-gradient(180deg, #fff 0%, #fbfdff 100%);
  box-shadow: 0 16px 38px rgb(24 69 101 / 10%);
}

.event-list[data-tab="confirmed"] {
  --event-accent: #218564;
  --event-accent-soft: #eaf6f1;
}

.event-list__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 18px 22px;
  border-bottom: 1px solid #e4ebf1;
  background: linear-gradient(
    90deg,
    var(--event-accent-soft) 0,
    #fff 32%,
    #fff 100%
  );
}

.event-list__header h2 {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 0;
  color: #264d69;
  font-size: 18px;
  font-weight: 650;
}

.event-list__header h2::before {
  width: 4px;
  height: 20px;
  flex: none;
  border-radius: 2px;
  background: var(--event-accent);
  content: "";
}

.event-list__header p {
  margin: 6px 0 0 13px;
  color: #5f7487;
  font-size: 12px;
  line-height: 1.55;
}

.event-list__header > span {
  flex: none;
  padding: 5px 10px;
  border: 1px solid color-mix(in srgb, var(--event-accent) 24%, white);
  border-radius: 999px;
  background: var(--event-accent-soft);
  color: var(--event-accent);
  font-size: 12px;
}

.event-table-wrap {
  overflow-x: auto;
}

.event-table {
  width: 100%;
  min-width: 1000px;
  border-collapse: collapse;
  table-layout: fixed;
}

.event-table th {
  padding: 13px 20px;
  background: #f3f7fa;
  color: #60788b;
  font-size: 12px;
  font-weight: 600;
  text-align: left;
}

.event-table th:nth-child(1) {
  width: 25%;
}

.event-table th:nth-child(2) {
  width: 17%;
}

.event-table th:nth-child(3) {
  width: 43%;
}

.event-table th:nth-child(4) {
  width: 15%;
}

.event-table td {
  padding: 18px 20px;
  border-bottom: 1px solid #e7eef3;
  color: #4e6579;
  font-size: 13px;
  vertical-align: middle;
}

.event-table tbody tr:last-child td {
  border-bottom: 0;
}

.event-table tbody tr {
  transition: background-color 140ms ease;
}

.event-table tbody tr[data-status="pending"] {
  background: linear-gradient(90deg, rgb(23 108 192 / 3%), transparent 22%);
}

.event-table tbody tr:hover {
  background: #f8fbfe;
}

.event-cell {
  min-width: 0;
}

.event-cell__title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.event-cell__title strong {
  color: #2f526c;
  font-size: 14px;
  font-weight: 650;
  line-height: 1.55;
}

.event-cell__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 7px;
  color: #65798b;
  font-size: 12px;
  line-height: 1.5;
}

.event-cell__meta i {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: #9cabb7;
}

.site-cell {
  display: grid;
  gap: 6px;
}

.site-cell > div {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
}

.site-cell strong {
  color: #315b79;
  font-size: 14px;
}

.site-cell span {
  color: #536b80;
  font-size: 12px;
}

.site-cell small {
  color: #65798b;
  font-size: 12px;
}

.analysis-cell {
  min-width: 0;
}

.risk-tag,
.review-tag {
  display: inline-flex;
  padding: 4px 8px;
  border-radius: 5px;
  background: #fff4dd;
  color: #94600b;
  font-size: 12px;
  line-height: 1.4;
  white-space: nowrap;
}

.risk-tag[data-risk="inspect"] {
  background: #fff0e9;
  color: #ad5628;
}

.risk-tag[data-risk="insufficient_data"] {
  background: #eef2f5;
  color: #5f7283;
}

.review-tag {
  margin-bottom: 8px;
  background: #eaf6f1;
  color: #2f765d;
}

.review-tag[data-outcome="needs_revision"] {
  background: #fff0f2;
  color: #a94453;
}

.analysis-cell p {
  display: -webkit-box;
  overflow: hidden;
  margin: 0;
  color: #4c6579;
  font-size: 13px;
  line-height: 1.68;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.analysis-cell small {
  display: block;
  margin-top: 8px;
  color: #65798b;
  font-size: 12px;
}

.event-actions {
  display: flex;
  align-items: center;
}

.event-actions__view {
  display: inline-flex;
  min-height: 36px;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 0 10px 0 12px;
  border: 1px solid #cfe0ed;
  border-radius: 7px;
  background: #f6fafe;
  color: #1767ad;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  transition:
    border-color 140ms ease,
    background-color 140ms ease;
}

.event-actions__view:hover {
  border-color: #9dbdd5;
  background: #edf6fd;
}

.event-actions__view svg {
  width: 16px;
  height: 16px;
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
  color: #355a74;
  font-size: 16px;
  font-weight: 650;
}

.event-list__empty p {
  margin: 0;
  color: #5f7487;
  font-size: 13px;
}

.event-pagination {
  display: flex;
  min-height: 58px;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 10px 20px;
  border-top: 1px solid #e6edf3;
  background: #f9fbfd;
}

.event-pagination p {
  margin: 0;
  color: #647a8c;
  font-size: 12px;
}

.event-pagination p strong {
  color: #355a74;
  font-weight: 650;
}

.event-pagination nav {
  display: flex;
  align-items: center;
  gap: 6px;
}

.event-pagination button {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  padding: 0;
  border: 1px solid #d3e0e9;
  border-radius: 7px;
  background: #fff;
  color: #536b7f;
  font-size: 12px;
}

.event-pagination button:hover:not(:disabled) {
  border-color: #9fbdd3;
  color: #1767ad;
}

.event-pagination button.active {
  border-color: #176cc0;
  background: #176cc0;
  color: #fff;
  font-weight: 650;
}

.event-pagination button:disabled {
  opacity: 0.42;
}

.event-pagination button svg {
  width: 17px;
  height: 17px;
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
    padding: 18px;
    border-bottom: 1px solid #e3ebf1;
  }

  .event-table tr:last-child {
    border-bottom: 0;
  }

  .event-table td {
    display: grid;
    grid-template-columns: 126px minmax(0, 1fr);
    gap: 14px;
    padding: 8px 0;
    border: 0;
  }

  .event-table td::before {
    content: attr(data-label);
    color: #60788b;
    font-size: 12px;
    font-weight: 600;
  }

  .event-table td:last-child {
    padding-top: 14px;
  }
}

@media (max-width: 620px) {
  .event-list__header {
    align-items: flex-start;
    padding: 18px 16px;
  }

  .event-table tr {
    padding: 16px;
  }

  .event-table td {
    grid-template-columns: 1fr;
    gap: 6px;
  }

  .event-pagination {
    align-items: flex-start;
    flex-direction: column;
    gap: 10px;
    padding: 14px 16px;
  }

  .event-pagination nav {
    width: 100%;
  }

  .event-pagination button {
    flex: 1;
  }
}
</style>
