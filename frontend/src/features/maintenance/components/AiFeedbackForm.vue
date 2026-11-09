<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useMaintenanceStore } from '../stores/maintenance'
import {
  aiFeedbackLabels,
  type AiFeedbackRating,
  type MaintenanceEvent,
} from '../types'
import { formatDateTime } from '../utils'

const props = defineProps<{
  event: MaintenanceEvent
}>()

const maintenance = useMaintenanceStore()
const selectedRating = ref<AiFeedbackRating | null>(
  props.event.aiFeedback?.rating ?? null,
)
const comment = ref(props.event.aiFeedback?.comment ?? '')

const hasFeedback = computed(() => Boolean(props.event.aiFeedback))

watch(
  () => props.event.id,
  () => {
    selectedRating.value = props.event.aiFeedback?.rating ?? null
    comment.value = props.event.aiFeedback?.comment ?? ''
  },
)

function saveFeedback() {
  if (!selectedRating.value) {
    return
  }

  maintenance.saveAiFeedback(
    props.event.id,
    selectedRating.value,
    comment.value,
  )
}
</script>

<template>
  <section class="ai-feedback" aria-labelledby="ai-feedback-title">
    <header class="ai-feedback__heading">
      <div>
        <span>HUMAN FEEDBACK</span>
        <h3 id="ai-feedback-title">AI 结论评价</h3>
        <p>请标识本次 AI 分析结论是否准确，评价将随工单记录保留。</p>
      </div>
      <span v-if="hasFeedback" class="ai-feedback__saved">已记录</span>
    </header>

    <div class="ai-feedback__options" role="radiogroup" aria-label="AI结论评价">
      <label
        class="feedback-option"
        :class="{ 'feedback-option--selected': selectedRating === 'accurate' }"
      >
        <input
          v-model="selectedRating"
          type="radio"
          value="accurate"
          name="ai-feedback"
        />
        <span class="feedback-option__icon" aria-hidden="true">
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
        <span>
          <b>{{ aiFeedbackLabels.accurate }}</b>
          <small>结论与数据证据相符</small>
        </span>
      </label>

      <label
        class="feedback-option"
        :class="{
          'feedback-option--selected': selectedRating === 'needs_revision',
        }"
      >
        <input
          v-model="selectedRating"
          type="radio"
          value="needs_revision"
          name="ai-feedback"
        />
        <span class="feedback-option__icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path
              d="M6 18L9.5 16.8L18.2 8.1C18.8 7.5 18.8 6.6 18.2 6L18 5.8C17.4 5.2 16.5 5.2 15.9 5.8L7.2 14.5L6 18Z"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linejoin="round"
            />
          </svg>
        </span>
        <span>
          <b>{{ aiFeedbackLabels.needs_revision }}</b>
          <small>结论或建议需要人工修正</small>
        </span>
      </label>
    </div>

    <label class="ai-feedback__comment">
      <span>补充说明 <small>选填</small></span>
      <textarea
        v-model="comment"
        maxlength="300"
        rows="3"
        placeholder="可补充现场情况、判断依据或需要修正的内容"
      ></textarea>
      <em>{{ comment.length }}/300</em>
    </label>

    <footer class="ai-feedback__footer">
      <small v-if="event.aiFeedback">
        最近评价：{{ aiFeedbackLabels[event.aiFeedback.rating] }} ·
        {{ formatDateTime(event.aiFeedback.submittedAt) }}
      </small>
      <button
        type="button"
        :disabled="!selectedRating"
        @click="saveFeedback"
      >
        {{ hasFeedback ? '更新评价' : '保存评价' }}
      </button>
    </footer>
  </section>
</template>

<style scoped>
.ai-feedback {
  padding: 19px 20px 18px;
  border: 1px solid #dbe7f0;
  border-radius: 9px;
  background: #fff;
}

.ai-feedback__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 15px;
}

.ai-feedback__heading > div > span {
  color: #8aa0b1;
  font-size: 9px;
  letter-spacing: 1.4px;
}

.ai-feedback__heading h3 {
  margin: 5px 0 5px;
  color: #2c516f;
  font-size: 15px;
  font-weight: 600;
}

.ai-feedback__heading p {
  margin: 0;
  color: #8498a8;
  font-size: 11px;
  line-height: 1.6;
}

.ai-feedback__saved {
  flex: none;
  padding: 4px 8px;
  border-radius: 999px;
  background: #eaf6f1;
  color: #397b66;
  font-size: 10px;
}

.ai-feedback__options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.feedback-option {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
  padding: 12px 13px;
  border: 1px solid #dae5ee;
  border-radius: 7px;
  background: #fbfdff;
  cursor: pointer;
  transition:
    border-color 140ms ease,
    background-color 140ms ease;
}

.feedback-option:hover {
  border-color: #a7c4d9;
}

.feedback-option--selected {
  border-color: #73a7cf;
  background: #f0f7fd;
}

.feedback-option input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.feedback-option__icon {
  display: grid;
  width: 30px;
  height: 30px;
  flex: none;
  place-items: center;
  border-radius: 8px;
  background: #eef3f7;
  color: #668094;
}

.feedback-option--selected .feedback-option__icon {
  background: #dcecf9;
  color: #176cc0;
}

.feedback-option__icon svg {
  width: 18px;
  height: 18px;
}

.feedback-option b,
.feedback-option small {
  display: block;
}

.feedback-option b {
  color: #3b5c74;
  font-size: 12px;
  font-weight: 600;
}

.feedback-option small {
  overflow: hidden;
  margin-top: 4px;
  color: #8a9dab;
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ai-feedback__comment {
  position: relative;
  display: block;
  margin-top: 14px;
}

.ai-feedback__comment > span {
  display: block;
  margin-bottom: 7px;
  color: #516d82;
  font-size: 11px;
}

.ai-feedback__comment > span small {
  color: #98a7b3;
}

.ai-feedback__comment textarea {
  width: 100%;
  min-height: 76px;
  resize: vertical;
  padding: 10px 12px 24px;
  border: 1px solid #d4e0e9;
  border-radius: 7px;
  background: #fff;
  color: #405f76;
  font-size: 12px;
  line-height: 1.6;
}

.ai-feedback__comment textarea:focus {
  border-color: #79a9ca;
  outline: none;
  box-shadow: 0 0 0 3px rgb(23 108 192 / 8%);
}

.ai-feedback__comment em {
  position: absolute;
  right: 10px;
  bottom: 8px;
  color: #a0aeba;
  font-size: 9px;
  font-style: normal;
}

.ai-feedback__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-top: 11px;
}

.ai-feedback__footer small {
  color: #879aa9;
  font-size: 10px;
}

.ai-feedback__footer button {
  min-width: 88px;
  height: 34px;
  padding: 0 14px;
  border-radius: 6px;
  background: #176cc0;
  color: #fff;
  font-size: 11px;
  font-weight: 500;
}

.ai-feedback__footer button:disabled {
  background: #b7c7d3;
}

@media (max-width: 640px) {
  .ai-feedback {
    padding: 17px 14px;
  }

  .ai-feedback__options {
    grid-template-columns: 1fr;
  }

  .ai-feedback__footer {
    align-items: stretch;
    flex-direction: column;
  }

  .ai-feedback__footer button {
    width: 100%;
  }
}
</style>
