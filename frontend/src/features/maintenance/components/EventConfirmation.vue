<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useMaintenanceStore } from '../stores/maintenance'
import {
  aiFeedbackLabels,
  confirmationDecisionLabels,
  type ConfirmationDecision,
  type MaintenanceEvent,
} from '../types'
import { formatDateTime } from '../utils'

const props = defineProps<{
  event: MaintenanceEvent
}>()

const maintenance = useMaintenanceStore()
const decision = ref<ConfirmationDecision | null>(
  props.event.confirmation?.decision ?? null,
)
const comment = ref(props.event.confirmation?.comment ?? '')

const isConfirmed = computed(() => props.event.status === 'confirmed')
const needsComment = computed(
  () => decision.value === 'rejected' && !comment.value.trim(),
)
const canSubmit = computed(
  () =>
    !isConfirmed.value &&
    Boolean(decision.value) &&
    !needsComment.value,
)

watch(
  () => props.event.id,
  () => {
    decision.value = props.event.confirmation?.decision ?? null
    comment.value = props.event.confirmation?.comment ?? ''
  },
)

function submitConfirmation() {
  if (!decision.value || !canSubmit.value) {
    return
  }

  maintenance.confirmEvent(props.event.id, decision.value, comment.value)
}
</script>

<template>
  <div class="confirmation-panel">
    <template v-if="isConfirmed && event.confirmation">
      <section class="confirmed-summary" aria-labelledby="confirmed-title">
        <span class="confirmed-summary__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path
              d="M5.5 12.5L9.5 16.5L18.5 7.5"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </span>
        <div>
          <h3 id="confirmed-title">该工单已完成人工确认</h3>
          <p>
            {{ confirmationDecisionLabels[event.confirmation.decision] }}
          </p>
        </div>
      </section>

      <dl class="confirmation-record">
        <div>
          <dt>确认结论</dt>
          <dd>{{ confirmationDecisionLabels[event.confirmation.decision] }}</dd>
        </div>
        <div>
          <dt>确认意见</dt>
          <dd>{{ event.confirmation.comment || '未填写补充说明' }}</dd>
        </div>
        <div>
          <dt>确认人</dt>
          <dd>{{ event.confirmation.confirmedBy }}</dd>
        </div>
        <div>
          <dt>确认时间</dt>
          <dd>{{ formatDateTime(event.confirmation.confirmedAt) }}</dd>
        </div>
      </dl>
    </template>

    <template v-else>
      <header class="confirmation-panel__heading">
        <span>HUMAN CONFIRMATION</span>
        <h3>确认本次事件分析</h3>
        <p>
          确认仅针对 {{ event.eventNumber }}，不会改变该站点其他工单的分析状态。
        </p>
      </header>

      <div class="ai-feedback-summary">
        <div>
          <span>AI 结论评价</span>
          <strong v-if="event.aiFeedback">
            {{ aiFeedbackLabels[event.aiFeedback.rating] }}
          </strong>
          <strong v-else>尚未评价</strong>
        </div>
        <p v-if="event.aiFeedback?.comment">
          {{ event.aiFeedback.comment }}
        </p>
        <p v-else>
          可在“分析详情”中标识 AI 结论是否准确，再提交人工确认。
        </p>
      </div>

      <fieldset class="confirmation-choice">
        <legend>人工确认结论</legend>
        <label
          class="confirmation-card"
          :class="{ 'confirmation-card--selected': decision === 'accepted' }"
        >
          <input
            v-model="decision"
            type="radio"
            value="accepted"
            name="confirmation-decision"
          />
          <span class="confirmation-card__mark" aria-hidden="true"></span>
          <span>
            <b>认可本次 AI 分析</b>
            <small>确认分析依据和结论后归档</small>
          </span>
        </label>
        <label
          class="confirmation-card"
          :class="{ 'confirmation-card--selected': decision === 'rejected' }"
        >
          <input
            v-model="decision"
            type="radio"
            value="rejected"
            name="confirmation-decision"
          />
          <span class="confirmation-card__mark" aria-hidden="true"></span>
          <span>
            <b>不认可本次 AI 分析</b>
            <small>记录人工判断和后续处理结论</small>
          </span>
        </label>
      </fieldset>

      <label class="confirmation-comment">
        <span>
          确认说明
          <small>{{ decision === 'rejected' ? '必填' : '选填' }}</small>
        </span>
        <textarea
          v-model="comment"
          rows="5"
          maxlength="500"
          placeholder="填写现场核查情况、人工判断或其他需要保留的说明"
        ></textarea>
        <em v-if="needsComment">不认可 AI 分析时，请填写人工确认结论。</em>
      </label>

      <div class="confirmation-footer">
        <p>
          提交后工单将移入“已确认记录”，AI 分析结果不会触发设备控制或自动工单流转。
        </p>
        <button
          type="button"
          :disabled="!canSubmit"
          @click="submitConfirmation"
        >
          提交人工确认
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.confirmation-panel {
  display: grid;
  gap: 18px;
  max-width: 860px;
  margin: 0 auto;
}

.confirmation-panel__heading > span {
  color: #8aa0b1;
  font-size: 9px;
  letter-spacing: 1.45px;
}

.confirmation-panel__heading h3 {
  margin: 6px 0 7px;
  color: #2b4f6c;
  font-size: 18px;
  font-weight: 600;
}

.confirmation-panel__heading p {
  margin: 0;
  color: #7f93a3;
  font-size: 12px;
  line-height: 1.7;
}

.confirmed-summary {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 17px 18px;
  border: 1px solid #d9e9e2;
  border-radius: 8px;
  background: #f3faf7;
}

.confirmed-summary__icon {
  display: grid;
  width: 38px;
  height: 38px;
  flex: none;
  place-items: center;
  border-radius: 50%;
  background: #dff2e9;
  color: #2d8766;
}

.confirmed-summary__icon svg {
  width: 21px;
  height: 21px;
}

.confirmed-summary h3 {
  margin: 0;
  color: #316e59;
  font-size: 14px;
  font-weight: 600;
}

.confirmed-summary p {
  margin: 5px 0 0;
  color: #6e8f81;
  font-size: 11px;
}

.confirmation-record {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin: 0;
  border: 1px solid #dce6ee;
  border-radius: 8px;
  background: #fff;
}

.confirmation-record > div {
  padding: 16px 18px;
  border-right: 1px solid #e6edf3;
  border-bottom: 1px solid #e6edf3;
}

.confirmation-record > div:nth-child(2n) {
  border-right: 0;
}

.confirmation-record > div:nth-last-child(-n + 2) {
  border-bottom: 0;
}

.confirmation-record dt {
  color: #8a9dab;
  font-size: 10px;
}

.confirmation-record dd {
  margin: 7px 0 0;
  color: #3c5e76;
  font-size: 12px;
  line-height: 1.7;
}

.ai-feedback-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 13px 15px;
  border: 1px solid #dfe8ef;
  border-radius: 7px;
  background: #f8fbfd;
}

.ai-feedback-summary > div {
  display: flex;
  flex: none;
  align-items: baseline;
  gap: 9px;
}

.ai-feedback-summary span {
  color: #7f94a4;
  font-size: 10px;
}

.ai-feedback-summary strong {
  color: #315d7e;
  font-size: 12px;
  font-weight: 600;
}

.ai-feedback-summary p {
  margin: 0;
  color: #7f93a2;
  font-size: 11px;
  line-height: 1.6;
  text-align: right;
}

.confirmation-choice {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 11px;
  margin: 0;
  padding: 0;
  border: 0;
}

.confirmation-choice legend {
  margin-bottom: 9px;
  color: #516d81;
  font-size: 11px;
}

.confirmation-card {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 15px;
  border: 1px solid #d8e4ed;
  border-radius: 8px;
  background: #fff;
  cursor: pointer;
}

.confirmation-card:hover {
  border-color: #a8c4d9;
}

.confirmation-card--selected {
  border-color: #6fa3ca;
  background: #f1f8fd;
  box-shadow: 0 0 0 2px rgb(23 108 192 / 6%);
}

.confirmation-card input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.confirmation-card__mark {
  width: 17px;
  height: 17px;
  flex: none;
  border: 1px solid #b9cbd9;
  border-radius: 50%;
  background: #fff;
  box-shadow: inset 0 0 0 4px #fff;
}

.confirmation-card--selected .confirmation-card__mark {
  border-color: #176cc0;
  background: #176cc0;
}

.confirmation-card b,
.confirmation-card small {
  display: block;
}

.confirmation-card b {
  color: #365a74;
  font-size: 12px;
  font-weight: 600;
}

.confirmation-card small {
  margin-top: 5px;
  color: #879aa9;
  font-size: 10px;
}

.confirmation-comment {
  position: relative;
  display: block;
}

.confirmation-comment > span {
  display: block;
  margin-bottom: 8px;
  color: #516d81;
  font-size: 11px;
}

.confirmation-comment > span small {
  color: #9aa9b5;
}

.confirmation-comment textarea {
  width: 100%;
  min-height: 116px;
  resize: vertical;
  padding: 11px 12px;
  border: 1px solid #d3dfe8;
  border-radius: 7px;
  background: #fff;
  color: #405f76;
  font-size: 12px;
  line-height: 1.7;
}

.confirmation-comment textarea:focus {
  border-color: #77a7c8;
  outline: none;
  box-shadow: 0 0 0 3px rgb(23 108 192 / 8%);
}

.confirmation-comment em {
  display: block;
  margin-top: 7px;
  color: #b64b5a;
  font-size: 10px;
  font-style: normal;
}

.confirmation-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding-top: 3px;
}

.confirmation-footer p {
  max-width: 620px;
  margin: 0;
  color: #8a9dab;
  font-size: 10px;
  line-height: 1.65;
}

.confirmation-footer button {
  min-width: 120px;
  height: 38px;
  flex: none;
  padding: 0 17px;
  border-radius: 6px;
  background: #176cc0;
  color: #fff;
  font-size: 12px;
  font-weight: 500;
}

.confirmation-footer button:disabled {
  background: #b7c7d3;
}

@media (max-width: 640px) {
  .confirmation-record,
  .confirmation-choice {
    grid-template-columns: 1fr;
  }

  .confirmation-record > div {
    border-right: 0;
    border-bottom: 1px solid #e6edf3;
  }

  .confirmation-record > div:nth-last-child(-n + 2) {
    border-bottom: 1px solid #e6edf3;
  }

  .confirmation-record > div:last-child {
    border-bottom: 0;
  }

  .ai-feedback-summary,
  .confirmation-footer {
    align-items: stretch;
    flex-direction: column;
  }

  .ai-feedback-summary p {
    text-align: left;
  }

  .confirmation-footer button {
    width: 100%;
  }
}
</style>
