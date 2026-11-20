<!-- List of a plugin's background jobs (newest first): state, progress, phase, error, cancel / remove / log.
     The `result` slot renders a finished job's result; `phaseText` turns a worker phase into text. -->
<script setup lang="ts">
import { ref } from 'vue'
import { ElButton, ElDialog, ElEmpty, ElMessage, ElProgress, ElTooltip } from 'element-plus'
import { ACTIVE, fmtDuration, fmtTime, useKit, type Job } from './kit'

const props = defineProps<{ jobs: Job[]; phaseText?: (job: Job) => string; selected?: string }>()
const emit = defineEmits<{ changed: []; select: [id: string] }>()
const kit = useKit()
const logOpen = ref(false)
const logText = ref('')
const busy = ref('')

const CHIP: Record<string, string> = {
  queued: 'info', running: 'warning', succeeded: 'success', failed: 'danger', cancelled: 'info', interrupted: 'danger',
}

async function act(job: Job, what: 'cancel' | 'remove') {
  busy.value = job.id
  try {
    await (what === 'cancel' ? kit.cancel(job.id) : kit.remove(job.id))
    emit('changed')
  } catch (e) {
    ElMessage.error(kit.errorText(e))
  } finally {
    busy.value = ''
  }
}

async function showLog(job: Job) {
  logText.value = ''
  logOpen.value = true
  try {
    logText.value = (await kit.log(job.id)).text
  } catch (e) {
    logText.value = kit.errorText(e)
  }
}

const took = (j: Job) => (j.finished && j.started ? fmtDuration(j.finished - j.started) : '')
</script>

<template>
  <div class="pk-jobs">
    <ElEmpty v-if="!props.jobs.length" :image-size="60" :description="kit.t('common.jobs.empty')" />
    <div
      v-for="job in props.jobs" :key="job.id" class="pk-job"
      :class="{ selected: props.selected === job.id }" @click="emit('select', job.id)"
    >
      <div class="pk-job-head">
        <span class="pk-job-title">{{ job.title || job.kind }}</span>
        <span class="chip" :class="CHIP[job.state]">{{ kit.t(`common.jobs.state.${job.state}`) }}</span>
        <span class="pk-job-time mono">
          {{ kit.t('common.jobs.created', { time: fmtTime(job.created) }) }}<template v-if="took(job)">
            · {{ kit.t('common.jobs.took', { d: took(job) }) }}</template>
        </span>
        <span class="pk-job-actions" @click.stop>
          <ElButton v-if="ACTIVE.includes(job.state)" size="small" text :loading="busy === job.id" @click="act(job, 'cancel')">
            {{ kit.t('common.jobs.cancel') }}
          </ElButton>
          <ElButton size="small" text @click="showLog(job)">{{ kit.t('common.jobs.log') }}</ElButton>
          <ElButton
            v-if="!ACTIVE.includes(job.state)" size="small" text :loading="busy === job.id"
            @click="act(job, 'remove')"
          >
            {{ kit.t('common.jobs.remove') }}
          </ElButton>
        </span>
      </div>
      <div v-if="ACTIVE.includes(job.state)" class="pk-job-progress">
        <ElProgress :percentage="Math.round(job.progress * 100)" :stroke-width="6" :show-text="false" />
        <span class="pk-phase">{{ props.phaseText ? props.phaseText(job) : job.phase }}
          <span class="mono">{{ Math.round(job.progress * 100) }}%</span></span>
      </div>
      <ElTooltip v-if="job.error" :content="kit.codedText(job.error)" placement="top">
        <div class="pk-job-error">{{ kit.codedText(job.error) }}</div>
      </ElTooltip>
      <div v-if="job.state === 'succeeded'" class="pk-job-result" @click.stop>
        <slot name="result" :job="job" />
      </div>
    </div>
    <ElDialog v-model="logOpen" :title="kit.t('common.jobs.logTitle')" width="min(860px, 94vw)" append-to-body>
      <pre class="pk-log mono">{{ logText || kit.t('common.jobs.logEmpty') }}</pre>
      <template #footer>
        <ElButton @click="logOpen = false">{{ kit.t('common.jobs.close') }}</ElButton>
      </template>
    </ElDialog>
  </div>
</template>

<style scoped>
.pk-jobs {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.pk-job {
  padding: 10px 12px;
  border: 1px solid var(--hairline);
  border-radius: 10px;
  cursor: default;
}
.pk-job.selected {
  border-color: color-mix(in srgb, var(--accent) 60%, transparent);
}
.pk-job-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
}
.pk-job-title {
  font-weight: 600;
  color: var(--ink);
  overflow-wrap: anywhere;
}
.pk-job-time {
  font-size: 12px;
  color: var(--ink-3);
}
.pk-job-actions {
  margin-left: auto;
  display: flex;
  gap: 2px;
}
.pk-job-actions .el-button + .el-button {
  margin-left: 0;
}
.pk-job-progress {
  margin-top: 8px;
}
.pk-phase {
  display: flex;
  justify-content: space-between;
  margin-top: 4px;
  font-size: 12px;
  color: var(--ink-2);
}
.pk-job-error {
  margin-top: 6px;
  font-size: 12.5px;
  color: color-mix(in srgb, var(--bad) 80%, var(--ink));
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
}
.pk-job-result {
  margin-top: 10px;
}
.pk-log {
  max-height: 60vh;
  overflow: auto;
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
