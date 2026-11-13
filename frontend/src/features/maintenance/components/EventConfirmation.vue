<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useMaintenanceStore } from '../stores/maintenance'
import {
  type MaintenanceEvent,
  type ReviewOutcome,
  reviewOutcomeLabels,
} from '../types'
import { formatDateTime } from '../utils'

const props = defineProps<{
  event: MaintenanceEvent
}>()

const maintenance = useMaintenanceStore()
const outcome = ref<ReviewOutcome | null>(
  props.event.review?.outcome ?? null,
)
const comment = ref(props.event.review?.comment ?? '')
const isEditing = ref(false)

const isReviewed = computed(() => props.event.status === 'confirmed')
const canSubmit = computed(
  () => Boolean(outcome.value) && (!isReviewed.value || isEditing.value),
)

watch(
  () => props.event.id,
  () => {
    outcome.value = props.event.review?.outcome ?? null
    comment.value = props.event.review?.comment ?? ''
    isEditing.value = false
  },
)

function startEditing() {
  if (!props.event.review) {
    return
  }

  outcome.value = props.event.review.outcome
  comment.value = props.event.review.comment
  isEditing.value = true
}

function cancelEditing() {
  outcome.value = props.event.review?.outcome ?? null
  comment.value = props.event.review?.comment ?? ''
  isEditing.value = false
}

function submitReview() {
  if (!outcome.value || !canSubmit.value) {
    return
  }

  if (isEditing.value) {
    if (maintenance.updateReview(
      props.event.id,
      outcome.value,
      comment.value,
    )) {
      isEditing.value = false
    }
    return
  }

  maintenance.submitReview(props.event.id, outcome.value, comment.value)
}
</script>

<template>
  <div class="confirmation-panel" :data-status="event.status">
    <template v-if="isReviewed && event.review && !isEditing">
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
          <h3 id="confirmed-title">该事件已完成人工审核</h3>
          <p>
            AI 结论评价：{{ reviewOutcomeLabels[event.review.outcome] }}
            <template v-if="event.review.updatedAt">
              · 最后修改
              {{ formatDateTime(event.review.updatedAt) }}
            </template>
          </p>
        </div>
        <button
          type="button"
          class="confirmed-summary__edit"
          @click="startEditing"
        >
          修改审核记录
        </button>
      </section>

      <dl class="confirmation-record">
        <div>
          <dt>审核结论</dt>
          <dd>{{ reviewOutcomeLabels[event.review.outcome] }}</dd>
        </div>
        <div>
          <dt>审核意见</dt>
          <dd>{{ event.review.comment || '未填写补充说明' }}</dd>
        </div>
        <div>
          <dt>审核人</dt>
          <dd>{{ event.review.reviewedBy }}</dd>
        </div>
        <div>
          <dt>审核时间</dt>
          <dd>{{ formatDateTime(event.review.reviewedAt) }}</dd>
        </div>
      </dl>
    </template>

    <template v-else>
      <header class="confirmation-panel__heading">
        <h3 id="confirmation-title">
          {{ isEditing ? '修改审核记录' : '人工审核与归档' }}
        </h3>
      </header>

      <fieldset class="confirmation-choice">
        <legend>人工审核结论</legend>
        <label
          class="confirmation-card"
          :class="{ 'confirmation-card--selected': outcome === 'accurate' }"
        >
          <input
            v-model="outcome"
            type="radio"
            value="accurate"
            name="review-outcome"
          />
          <span class="confirmation-card__mark" aria-hidden="true"></span>
          <span>
            <b>确认 AI 结论准确</b>
          </span>
        </label>
        <label
          class="confirmation-card"
          :class="{ 'confirmation-card--selected': outcome === 'needs_revision' }"
        >
          <input
            v-model="outcome"
            type="radio"
            value="needs_revision"
            name="review-outcome"
          />
          <span class="confirmation-card__mark" aria-hidden="true"></span>
          <span>
            <b>AI 结论需修正</b>
          </span>
        </label>
      </fieldset>

      <label class="confirmation-comment">
        <span>
          审核说明
          <small>选填</small>
        </span>
        <textarea
          v-model="comment"
          rows="5"
          maxlength="500"
          placeholder="填写核查情况或人工判断（选填）"
        ></textarea>
      </label>

      <div class="confirmation-footer">
        <p v-if="isEditing">修改仅更新审核结论和说明。</p>
        <div class="confirmation-footer__actions">
          <button
            v-if="isEditing"
            type="button"
            class="secondary"
            @click="cancelEditing"
          >
            取消
          </button>
          <button
            type="button"
            :disabled="!canSubmit"
            @click="submitReview"
          >
            {{
              isEditing
                ? '保存修改'
                : '确认'
            }}
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.confirmation-panel {
  display: flex;
  height: 100%;
  min-height: 0;
  flex-direction: column;
  gap: 14px;
}

.confirmation-panel[data-status="pending"] {
  height: 100%;
}

.confirmation-panel__heading h3 {
  margin: 0;
  color: #2b4f6c;
  font-size: 18px;
  font-weight: 650;
}

.confirmation-panel__heading {
  padding-bottom: 10px;
  border-bottom: 1px solid #edf2f5;
}

.confirmed-summary {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 16px 17px;
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
  font-size: 15px;
  font-weight: 650;
}

.confirmed-summary p {
  margin: 6px 0 0;
  color: #6e8f81;
  font-size: 12px;
  line-height: 1.55;
}

.confirmed-summary__edit {
  min-height: 36px;
  margin-left: auto;
  padding: 0 11px;
  border: 1px solid #a9c9bb;
  border-radius: 5px;
  background: #fff;
  color: #39745e;
  font-size: 12px;
  white-space: nowrap;
}

.confirmed-summary__edit:hover {
  border-color: #6fa78e;
  background: #f8fcfa;
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
  padding: 14px 16px;
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
  color: #65798b;
  font-size: 12px;
}

.confirmation-record dd {
  margin: 8px 0 0;
  color: #3c5e76;
  font-size: 13px;
  line-height: 1.7;
}

.confirmation-choice {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin: 0;
  padding: 0;
  border: 0;
}

.confirmation-choice legend {
  margin-bottom: 10px;
  color: #486579;
  font-size: 12px;
}

.confirmation-card {
  display: flex;
  min-height: 58px;
  align-items: center;
  gap: 9px;
  padding: 12px;
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

.confirmation-card b {
  display: block;
  color: #365a74;
  font-size: 13px;
  font-weight: 650;
  line-height: 1.45;
}

.confirmation-comment {
  position: relative;
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
}

.confirmation-comment > span {
  display: block;
  margin-bottom: 8px;
  color: #486579;
  font-size: 12px;
}

.confirmation-comment > span small {
  color: #65798b;
}

.confirmation-comment textarea {
  width: 100%;
  min-height: 120px;
  flex: 1;
  resize: none;
  padding: 12px 13px;
  border: 1px solid #d3dfe8;
  border-radius: 7px;
  background: #fbfdff;
  color: #405f76;
  font-size: 13px;
  line-height: 1.65;
}

.confirmation-comment textarea:focus {
  border-color: #77a7c8;
  outline: none;
  box-shadow: 0 0 0 3px rgb(23 108 192 / 8%);
}

.confirmation-footer {
  display: flex;
  flex: none;
  align-items: stretch;
  flex-direction: column;
  gap: 10px;
  padding-top: 12px;
  border-top: 1px solid #edf2f5;
}

.confirmation-footer p {
  margin: 0;
  color: #65798b;
  font-size: 12px;
  line-height: 1.6;
}

.confirmation-footer__actions {
  display: flex;
  width: 100%;
  gap: 8px;
}

.confirmation-footer button {
  display: inline-flex;
  min-width: 0;
  height: 40px;
  flex: 1;
  align-items: center;
  justify-content: center;
  padding: 0 17px;
  border-radius: 6px;
  background: #176cc0;
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  line-height: 1;
}

.confirmation-footer button.secondary {
  border: 1px solid #cfdce6;
  background: #fff;
  color: #60788b;
}

.confirmation-footer button.secondary:hover {
  border-color: #a8bfd0;
  background: #f8fbfd;
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

}
</style>
