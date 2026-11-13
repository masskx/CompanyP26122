<script setup lang="ts">
import { computed } from 'vue'
import type { AiAnalysis } from '../types'

const props = defineProps<{
  analysis: AiAnalysis
}>()

const severityLabels = {
  watch: '需关注',
  inspect: '建议检修',
} as const

const limitation = computed(() => props.analysis.limitations[0])
</script>

<template>
  <div class="analysis-panel">
    <section class="analysis-summary">
      <span class="analysis-summary__mark" aria-hidden="true">AI</span>
      <div>
        <h4>综合判断</h4>
        <p>{{ analysis.explanation }}</p>
      </div>
    </section>

    <div
      v-if="analysis.findings.length > 0 || analysis.actions.length > 0"
      class="analysis-columns"
    >
      <section
        v-if="analysis.findings.length > 0"
        class="analysis-block"
      >
        <header class="analysis-block__heading">
          <h4>关键依据</h4>
          <span>{{ analysis.findings.length }} 项</span>
        </header>
        <ul class="finding-list">
          <li
            v-for="finding in analysis.findings"
            :key="`${finding.ruleId}-${finding.code}`"
            class="finding-item"
            :data-severity="finding.severity"
          >
            <span class="finding-item__marker" aria-hidden="true"></span>
            <div class="finding-item__content">
              <p>{{ finding.message }}</p>
              <span class="finding-item__severity">
                {{ severityLabels[finding.severity] }}
              </span>
            </div>
          </li>
        </ul>
      </section>

      <section
        v-if="analysis.actions.length > 0"
        class="analysis-block"
      >
        <header class="analysis-block__heading">
          <h4>建议动作</h4>
          <span>{{ analysis.actions.length }} 项</span>
        </header>
        <ol class="action-list">
          <li
            v-for="(action, index) in analysis.actions"
            :key="action.actionCode"
            class="action-item"
            :data-priority="action.priority"
          >
            <span class="action-item__index" aria-hidden="true">
              {{ index + 1 }}
            </span>
            <div class="action-item__content">
              <p>{{ action.text }}</p>
            </div>
            <span
              v-if="action.priority === 'primary'"
              class="action-item__tag"
            >
              主建议
            </span>
          </li>
        </ol>
      </section>
    </div>

    <aside
      v-if="limitation"
      class="analysis-limitation"
      aria-label="数据限制"
    >
      <span class="analysis-limitation__icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none">
          <path
            d="M12 7.5V13M12 16.5V16.6"
            stroke="currentColor"
            stroke-width="1.9"
            stroke-linecap="round"
          />
          <path
            d="M10.2 4.1L3.4 16.2A2 2 0 0 0 5.1 19.2H18.9A2 2 0 0 0 20.6 16.2L13.8 4.1A2 2 0 0 0 10.2 4.1Z"
            stroke="currentColor"
            stroke-width="1.7"
            stroke-linejoin="round"
          />
        </svg>
      </span>
      <div>
        <b>数据限制</b>
        <p>{{ limitation.text }}</p>
        <small>{{ limitation.affects }}</small>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.analysis-panel {
  display: flex;
  height: 100%;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
}

.analysis-summary {
  display: flex;
  flex: none;
  align-items: flex-start;
  gap: 12px;
  padding: 16px 18px;
  border: 1px solid #cfe0ed;
  border-left: 3px solid #2d7fc3;
  border-radius: 11px;
  background: linear-gradient(135deg, #f2f8fd 0%, #f8fbfe 100%);
}

.analysis-summary__mark {
  display: grid;
  width: 34px;
  height: 34px;
  flex: none;
  place-items: center;
  border-radius: 10px;
  background: #dcecf8;
  color: #1763a4;
  font-size: 12px;
  font-weight: 700;
}

.analysis-summary h4 {
  margin: 0;
  color: #526b7e;
  font-size: 13px;
  font-weight: 650;
  letter-spacing: 0.02em;
}

.analysis-summary p {
  margin: 7px 0 0;
  color: #294b67;
  font-size: 14px;
  line-height: 1.75;
}

.analysis-columns {
  display: grid;
  min-height: 0;
  flex: 1;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  margin-top: 14px;
  align-items: stretch;
}

.analysis-block {
  min-width: 0;
  padding: 14px;
  border: 1px solid #e1ebf2;
  border-radius: 10px;
  background: #f8fbfd;
}

.analysis-block__heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.analysis-block h4 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  color: #2f5571;
  font-size: 14px;
  font-weight: 650;
}

.analysis-block h4::before {
  width: 3px;
  height: 14px;
  flex: none;
  border-radius: 2px;
  background: #2d7fc3;
  content: "";
}

.analysis-block__heading > span {
  color: #65798b;
  font-size: 12px;
  white-space: nowrap;
}

.finding-list,
.action-list {
  display: grid;
  gap: 0;
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
}

.finding-item {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 9px;
  padding: 10px 0;
  border-bottom: 1px solid #e6edf3;
}

.finding-item:last-child {
  padding-bottom: 0;
  border-bottom: 0;
}

.finding-item__marker {
  width: 7px;
  height: 7px;
  flex: none;
  margin-top: 0.55em;
  border-radius: 50%;
  background: #ba842d;
  box-shadow: 0 0 0 3px #fff6e8;
}

.finding-item[data-severity="inspect"] .finding-item__marker {
  background: #b45a35;
  box-shadow: 0 0 0 3px #fff0e9;
}

.finding-item__content {
  display: grid;
  min-width: 0;
  flex: 1;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  gap: 8px;
}

.finding-item p,
.action-item p {
  margin: 0;
  color: #3f5f77;
  font-size: 13px;
  line-height: 1.68;
}

.finding-item__severity {
  padding: 2px 6px;
  border-radius: 4px;
  background: #fff4dd;
  color: #94600b;
  font-size: 11px;
  line-height: 1.5;
  white-space: nowrap;
}

.finding-item[data-severity="inspect"] .finding-item__severity {
  background: #fff0e9;
  color: #ad5628;
}

.action-item {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 9px;
  padding: 10px;
  border-bottom: 1px solid #e6edf3;
}

.action-item:last-child {
  border-bottom: 0;
}

.action-item[data-priority="primary"] {
  border-bottom-color: #dbe9f4;
  border-radius: 8px;
  background: #f5faff;
}

.action-item__index {
  display: grid;
  width: 22px;
  height: 22px;
  flex: none;
  place-items: center;
  border-radius: 50%;
  background: #e8eef3;
  color: #526b7e;
  font-size: 11px;
  font-weight: 700;
}

.action-item[data-priority="primary"] .action-item__index {
  background: #176cc0;
  color: #fff;
}

.action-item__content {
  min-width: 0;
  flex: 1;
}

.action-item__tag {
  flex: none;
  padding: 3px 7px;
  border-radius: 5px;
  background: #e6f1fb;
  color: #1767ad;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.5;
}

.analysis-limitation {
  display: flex;
  flex: none;
  align-items: flex-start;
  gap: 10px;
  margin-top: 14px;
  padding: 12px 14px;
  border: 1px solid #ecd9b6;
  border-radius: 9px;
  background: #fffaf1;
}

.analysis-limitation__icon {
  display: grid;
  width: 24px;
  height: 24px;
  flex: none;
  place-items: center;
  border-radius: 6px;
  background: #fff1d9;
  color: #a86c12;
}

.analysis-limitation__icon svg {
  width: 17px;
  height: 17px;
}

.analysis-limitation b {
  display: block;
  color: #7e5a1c;
  font-size: 12.5px;
  font-weight: 650;
}

.analysis-limitation p {
  margin: 4px 0 0;
  color: #6d5a38;
  font-size: 12.5px;
  line-height: 1.68;
}

.analysis-limitation small {
  display: block;
  margin-top: 4px;
  color: #8a744c;
  font-size: 11px;
  line-height: 1.5;
}

@media (max-width: 760px) {
  .analysis-columns {
    grid-template-columns: 1fr;
    gap: 12px;
  }

  .analysis-block + .analysis-block {
    margin-top: 0;
  }

  .finding-item__content {
    grid-template-columns: 1fr;
    gap: 5px;
  }

  .action-item {
    flex-wrap: wrap;
  }

  .action-item__tag {
    margin-left: 31px;
  }
}
</style>
