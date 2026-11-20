<!-- Map quality evaluation page: pick an estimated map and a reference (another map or an uploaded cloud), run the
     registration + metrics job, look at the KPIs, the error image, the tau table and the distance histogram. -->
<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import {
  ElButton, ElInputNumber, ElMessage, ElMessageBox, ElOption, ElRadioButton, ElRadioGroup, ElSelect, ElSwitch,
} from 'element-plus'
import { RC } from './shared/sdk'
import { fmtSize, makeKit, provideKit, type Job } from './shared/kit'
import { useJobs } from './shared/useJobs'
import JobList from './shared/JobList.vue'
import PythonGate from './shared/PythonGate.vue'

interface MapItem { name: string; created: string; algorithm: string | null; points: number | null }
interface RefFile { name: string; size: number; format: string | null; points: number | null; error: string | null }
interface Row { tau: number; precision: number; completeness: number; fscore: number; inlier_rmse: number | null }
interface Result {
  estimate: { name: string; points: number; eval_points: number }
  reference: { kind: string; name: string; points: number; eval_points: number }
  params: { eval_voxel: number; tau: number; max_dist: number; init: string; refine: boolean }
  registration: {
    init: string; method: string; refine: boolean; converged: boolean | null; iterations: number
    inlier_fraction: number; rmse: number | null; transform: number[][]
    pose: { x: number; y: number; z: number; roll: number; pitch: number; yaw: number }
    search: { tried: number; best: { yaw0: number; start: string } } | null
  }
  metrics: {
    tau: number; max_dist: number; precision: number; completeness: number; fscore: number
    accuracy: { mean: number | null; median: number | null; rmse: number | null; p95: number | null; matched: number; beyond: number }
    chamfer: { value: number | null; est_to_ref: number | null; ref_to_est: number | null; truncated_est: number; truncated_ref: number }
    table: Row[]
    crop: { estimate_kept: number; reference_kept: number } | null
  }
  histogram: { bin_width: number; max: number; counts: number[]; overflow: number }
  image: { file: string; vmin: number; vmax: number; ramp: string[] }
  warnings: string[]
  seconds: { total: number }
}

const kit = makeKit('map_eval')
provideKit(kit)
const { t } = kit
const { GlassPanel, InfoTip } = RC().ui
const { jobs, python, ready, refresh } = useJobs(kit)

const maps = ref<MapItem[]>([])
const refs = ref<RefFile[]>([])
const refInfo = reactive({ free: 0, limit_mb: 2048 })
const estimate = ref('')
const refKind = ref<'map' | 'file'>('map')
const refMap = ref('')
const refFile = ref('')
const starting = ref(false)
const uploading = ref<number | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const selectedJob = ref('')
const params = reactive({
  init: 'identity', method: 'GICP', refine: true, x: 0, y: 0, z: 0, yaw: 0, yaw_step: 30,
  reg_voxel: 0.25, max_corr: 1.0, max_iterations: 50,
  eval_voxel: 0.05, tau: 0.1, max_dist: 1.0, color_max: 0, crop_overlap: false,
})
const refName = computed(() => (refKind.value === 'map' ? refMap.value : refFile.value))
// the job whose full result is shown: the one clicked, else the newest that succeeded; others get one line
const shown = computed(() => selectedJob.value || jobs.value.find((j) => j.state === 'succeeded')?.id || '')
const canRun = computed(() => ready.value && !!estimate.value && !!refName.value &&
  !(refKind.value === 'map' && refMap.value === estimate.value))

async function loadMaps() {
  try {
    maps.value = await kit.request<MapItem[]>('GET', '/maps', undefined, 60000)
    if (!estimate.value) estimate.value = maps.value[0]?.name ?? ''
    if (!refMap.value) refMap.value = maps.value.find((m) => m.name !== estimate.value)?.name ?? ''
  } catch (e) {
    ElMessage.error(kit.errorText(e))
  }
}

async function loadRefs() {
  try {
    const out = await kit.request<{ files: RefFile[]; free: number; limit_mb: number }>('GET', '/references')
    refs.value = out.files
    refInfo.free = out.free
    refInfo.limit_mb = out.limit_mb
    if (refFile.value && !refs.value.some((r) => r.name === refFile.value)) refFile.value = ''
    if (!refFile.value) refFile.value = refs.value.find((r) => !r.error)?.name ?? ''
  } catch (e) {
    ElMessage.error(kit.errorText(e))
  }
}

/** multipart upload with progress (XMLHttpRequest: fetch has no upload progress) */
function upload(file: File) {
  const form = new FormData()
  form.append('file', file, file.name)
  uploading.value = 0
  const xhr = new XMLHttpRequest()
  xhr.open('POST', `${kit.base}/references`)
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) uploading.value = Math.round((e.loaded / e.total) * 100) }
  xhr.onload = async () => {
    uploading.value = null
    let body: { name?: string; detail?: { code?: string; params?: Record<string, unknown> } | string } = {}
    try { body = JSON.parse(xhr.responseText) } catch { /* not JSON */ }
    if (xhr.status >= 200 && xhr.status < 300 && body.name) {
      ElMessage.success(t('form.uploaded', { name: body.name }))
      refKind.value = 'file'
      refFile.value = body.name
      await loadRefs()
    } else if (body.detail && typeof body.detail === 'object' && body.detail.code) {
      ElMessage.error(kit.codedText({ code: body.detail.code, params: body.detail.params ?? {} }))
    } else {
      ElMessage.error(`HTTP ${xhr.status} ${xhr.statusText}`)
    }
  }
  xhr.onerror = () => {
    uploading.value = null
    ElMessage.error(t('errors.bad_upload', { reason: 'network error' }))
  }
  xhr.send(form)
}

function pickFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file) upload(file)
}

async function removeRef(name: string) {
  try {
    await ElMessageBox.confirm(t('form.deleteConfirm', { name }), { type: 'warning' })
  } catch {
    return
  }
  try {
    await kit.request('DELETE', `/references/${encodeURIComponent(name)}`)
    await loadRefs()
  } catch (e) {
    ElMessage.error(kit.errorText(e))
  }
}

async function run() {
  starting.value = true
  try {
    const job = await kit.request<Job>('POST', '/run', {
      estimate: estimate.value, reference: { kind: refKind.value, name: refName.value }, params: { ...params },
    })
    selectedJob.value = job.id
    ElMessage.success(t('form.started'))
    await refresh()
  } catch (e) {
    ElMessage.error(kit.errorText(e))
  } finally {
    starting.value = false
  }
}

// ---- result formatting
const res = (j: Job) => j.result as unknown as Result | null
const phaseText = (j: Job) => (j.phase ? t(`phases.${j.phase}`) : '')
const m = (v: number | null | undefined, d = 3) => (v == null ? '–' : `${v.toFixed(d)} m`)
const pct = (v: number | null | undefined) => (v == null ? '–' : `${(v * 100).toFixed(1)}%`)
const num = (v: number | null | undefined) => (v == null ? '–' : v.toLocaleString())
const fix = (v: number, d = 3) => (Math.abs(v) < 0.5 * 10 ** -d ? (0).toFixed(d) : v.toFixed(d))
const gradient = (r: Result) => `linear-gradient(to right, ${r.image.ramp.join(', ')})`
const startText = (s: string) => t(s === 'origin' ? 'result.startOrigin' : 'result.startCentroid')

/** bars of the distance histogram as SVG rectangles (viewBox 0 0 W H) */
const W = 600
const H = 150
const PAD = { l: 44, r: 8, t: 8, b: 22 }
function bars(r: Result) {
  const c = r.histogram.counts
  const top = Math.max(1, ...c)
  const bw = (W - PAD.l - PAD.r) / c.length
  return c.map((n, i) => {
    const h = (n / top) * (H - PAD.t - PAD.b)
    return { x: PAD.l + i * bw + 0.5, y: H - PAD.b - h, w: Math.max(0.5, bw - 1), h, n,
      from: i * r.histogram.bin_width, to: (i + 1) * r.histogram.bin_width }
  })
}
const histMax = (r: Result) => Math.max(1, ...r.histogram.counts)
const xOf = (r: Result, d: number) => PAD.l + (d / r.histogram.max) * (W - PAD.l - PAD.r)

loadMaps()
loadRefs()
</script>

<template>
  <div class="me-page">
    <GlassPanel :title="t('title')">
      <p class="me-intro">{{ t('intro') }}</p>
      <PythonGate :python="python" @changed="refresh" />
    </GlassPanel>

    <GlassPanel :title="t('form.title')">
      <div v-if="!maps.length" class="me-dim">{{ t('form.noMaps') }}</div>
      <div class="me-grid">
        <label class="me-field wide">
          <span>{{ t('form.estimate') }}</span><InfoTip :content="t('form.estimateTip')" />
          <ElSelect v-model="estimate" filterable size="small" :placeholder="t('form.pickMap')" class="me-select">
            <ElOption v-for="mp in maps" :key="mp.name" :value="mp.name" :label="mp.name">
              <span>{{ mp.name }}</span>
              <span v-if="mp.points" class="me-opt">{{ t('form.points', { n: num(mp.points) }) }}</span>
            </ElOption>
          </ElSelect>
        </label>
        <div class="me-field wide">
          <span>{{ t('form.reference') }}</span>
          <ElRadioGroup v-model="refKind" size="small">
            <ElRadioButton value="map">{{ t('form.refMap') }}</ElRadioButton>
            <ElRadioButton value="file">{{ t('form.refFile') }}</ElRadioButton>
          </ElRadioGroup>
          <ElSelect v-if="refKind === 'map'" v-model="refMap" filterable size="small" :placeholder="t('form.pickMap')" class="me-select">
            <ElOption v-for="mp in maps" :key="mp.name" :value="mp.name" :label="mp.name" :disabled="mp.name === estimate">
              <span>{{ mp.name }}</span>
              <span v-if="mp.points" class="me-opt">{{ t('form.points', { n: num(mp.points) }) }}</span>
            </ElOption>
          </ElSelect>
          <ElSelect v-else v-model="refFile" size="small" :placeholder="t('form.pickRef')" class="me-select">
            <ElOption v-for="rf in refs" :key="rf.name" :value="rf.name" :label="rf.name" :disabled="!!rf.error">
              <span>{{ rf.name }}</span>
              <span class="me-opt">{{ rf.error ? rf.error : t('form.points', { n: num(rf.points) }) }}</span>
            </ElOption>
          </ElSelect>
        </div>
        <div v-if="refKind === 'file'" class="me-refs wide">
          <div class="me-refs-head">
            <span>{{ t('form.refFiles') }}</span>
            <input ref="fileInput" type="file" accept=".pcd,.ply" class="me-hidden" @change="pickFile">
            <ElButton size="small" :loading="uploading !== null" @click="fileInput?.click()">
              {{ uploading !== null ? t('form.uploading', { p: uploading }) : t('form.upload') }}
            </ElButton>
            <span class="me-dim">{{ t('form.uploadHint', { mb: refInfo.limit_mb, free: fmtSize(refInfo.free) }) }}</span>
          </div>
          <div v-if="!refs.length" class="me-dim">{{ t('form.noRefs') }}</div>
          <div v-for="rf in refs" :key="rf.name" class="me-ref">
            <span class="mono">{{ rf.name }}</span>
            <span class="me-dim">{{ fmtSize(rf.size) }} · {{ rf.error ? rf.error : `${rf.format} · ${t('form.points', { n: num(rf.points) })}` }}</span>
            <ElButton size="small" text type="danger" @click="removeRef(rf.name)">{{ t('form.deleteRef') }}</ElButton>
          </div>
        </div>

        <div class="me-sub wide">{{ t('form.registration') }}</div>
        <div class="me-field wide">
          <span>{{ t('form.init') }}</span><InfoTip :content="t('form.initTip')" />
          <ElRadioGroup v-model="params.init" size="small">
            <ElRadioButton value="identity">{{ t('form.initIdentity') }}</ElRadioButton>
            <ElRadioButton value="manual">{{ t('form.initManual') }}</ElRadioButton>
            <ElRadioButton value="search">{{ t('form.initSearch') }}</ElRadioButton>
          </ElRadioGroup>
        </div>
        <template v-if="params.init === 'manual'">
          <label class="me-field"><span>{{ t('form.x') }}</span>
            <ElInputNumber v-model="params.x" :min="-10000" :max="10000" :step="0.5" :precision="2" size="small" controls-position="right" /></label>
          <label class="me-field"><span>{{ t('form.y') }}</span>
            <ElInputNumber v-model="params.y" :min="-10000" :max="10000" :step="0.5" :precision="2" size="small" controls-position="right" /></label>
          <label class="me-field"><span>{{ t('form.z') }}</span>
            <ElInputNumber v-model="params.z" :min="-10000" :max="10000" :step="0.1" :precision="2" size="small" controls-position="right" /></label>
          <label class="me-field"><span>{{ t('form.yaw') }}</span>
            <ElInputNumber v-model="params.yaw" :min="-180" :max="180" :step="5" :precision="1" size="small" controls-position="right" /></label>
        </template>
        <label v-if="params.init === 'search'" class="me-field">
          <span>{{ t('form.yawStep') }}</span><InfoTip :content="t('form.yawStepTip')" />
          <ElInputNumber v-model="params.yaw_step" :min="5" :max="90" :step="5" size="small" controls-position="right" />
        </label>
        <div class="me-field">
          <span>{{ t('form.method') }}</span><InfoTip :content="t('form.methodTip')" />
          <ElRadioGroup v-model="params.method" size="small" class="me-right">
            <ElRadioButton value="GICP">GICP</ElRadioButton>
            <ElRadioButton value="VGICP">VGICP</ElRadioButton>
          </ElRadioGroup>
        </div>
        <label class="me-field">
          <span>{{ t('form.refine') }}</span><InfoTip :content="t('form.refineTip')" />
          <ElSwitch v-model="params.refine" class="me-right" />
        </label>
        <label class="me-field">
          <span>{{ t('form.regVoxel') }}</span><InfoTip :content="t('form.regVoxelTip')" />
          <ElInputNumber v-model="params.reg_voxel" :min="0.05" :max="2" :step="0.05" :precision="2" size="small" controls-position="right" />
        </label>
        <label class="me-field">
          <span>{{ t('form.maxCorr') }}</span><InfoTip :content="t('form.maxCorrTip')" />
          <ElInputNumber v-model="params.max_corr" :min="0.05" :max="10" :step="0.25" :precision="2" size="small" controls-position="right" />
        </label>
        <label class="me-field">
          <span>{{ t('form.maxIter') }}</span>
          <ElInputNumber v-model="params.max_iterations" :min="5" :max="200" :step="5" size="small" controls-position="right" />
        </label>

        <div class="me-sub wide">{{ t('form.metrics') }}</div>
        <label class="me-field">
          <span>{{ t('form.evalVoxel') }}</span><InfoTip :content="t('form.evalVoxelTip')" />
          <ElInputNumber v-model="params.eval_voxel" :min="0.01" :max="0.5" :step="0.01" :precision="2" size="small" controls-position="right" />
        </label>
        <label class="me-field">
          <span>{{ t('form.tau') }}</span><InfoTip :content="t('form.tauTip')" />
          <ElInputNumber v-model="params.tau" :min="0.01" :max="2" :step="0.05" :precision="2" size="small" controls-position="right" />
        </label>
        <label class="me-field">
          <span>{{ t('form.maxDist') }}</span><InfoTip :content="t('form.maxDistTip')" />
          <ElInputNumber v-model="params.max_dist" :min="0.05" :max="10" :step="0.25" :precision="2" size="small" controls-position="right" />
        </label>
        <label class="me-field">
          <span>{{ t('form.colorMax') }}</span><InfoTip :content="t('form.colorMaxTip')" />
          <ElInputNumber v-model="params.color_max" :min="0" :max="10" :step="0.05" :precision="2" size="small" controls-position="right" />
        </label>
        <label class="me-field">
          <span>{{ t('form.crop') }}</span><InfoTip :content="t('form.cropTip')" />
          <ElSwitch v-model="params.crop_overlap" class="me-right" />
        </label>
      </div>
      <ElButton type="primary" class="me-run" :loading="starting" :disabled="!canRun" @click="run">
        {{ t('form.run') }}
      </ElButton>
    </GlassPanel>

    <GlassPanel :title="t('common.jobs.title')">
      <JobList :jobs="jobs" :phase-text="phaseText" :selected="selectedJob" @changed="refresh" @select="(id: string) => (selectedJob = id)">
        <template #result="{ job }">
          <div v-if="res(job) && shown !== job.id" class="me-compact">
            <span class="mono">{{ t('result.compact', { f: pct(res(job)!.metrics.fscore), tau: res(job)!.metrics.tau, acc: m(res(job)!.metrics.accuracy.mean), c: pct(res(job)!.metrics.completeness), cd: m(res(job)!.metrics.chamfer.value) }) }}</span>
            <span v-if="res(job)!.warnings.length" class="chip warning">{{ t('result.warnCount', { n: res(job)!.warnings.length }) }}</span>
            <ElButton size="small" text type="primary" @click="selectedJob = job.id">{{ t('result.show') }}</ElButton>
          </div>
          <div v-else-if="res(job)" class="me-result">
            <div v-for="w in res(job)!.warnings" :key="w" class="me-warn">{{ t(`warnings.${w}`) }}</div>

            <div class="me-kpis">
              <div class="me-kpi accent">
                <span>{{ t('result.accMean') }}</span><b class="mono">{{ m(res(job)!.metrics.accuracy.mean) }}</b>
                <small>{{ t('result.accSub', { n: num(res(job)!.metrics.accuracy.matched), d: res(job)!.metrics.max_dist }) }}</small>
              </div>
              <div class="me-kpi">
                <span>{{ t('result.accMedian') }}</span><b class="mono">{{ m(res(job)!.metrics.accuracy.median) }}</b>
                <small>{{ t('result.accRmse') }} {{ m(res(job)!.metrics.accuracy.rmse) }}</small>
              </div>
              <div class="me-kpi">
                <span>{{ t('result.accP95') }}</span><b class="mono">{{ m(res(job)!.metrics.accuracy.p95) }}</b>
                <small>{{ t('result.accuracy') }}</small>
              </div>
              <div class="me-kpi accent">
                <span>{{ t('result.fscore') }}</span><b class="mono">{{ pct(res(job)!.metrics.fscore) }}</b>
                <small>{{ t('result.fscoreSub', { tau: res(job)!.metrics.tau }) }}</small>
              </div>
              <div class="me-kpi">
                <span>{{ t('result.completeness') }}</span><b class="mono">{{ pct(res(job)!.metrics.completeness) }}</b>
                <small>{{ t('result.completenessSub') }}</small>
              </div>
              <div class="me-kpi">
                <span>{{ t('result.precision') }}</span><b class="mono">{{ pct(res(job)!.metrics.precision) }}</b>
                <small>{{ t('result.precisionSub') }}</small>
              </div>
              <div class="me-kpi">
                <span>{{ t('result.chamfer') }}</span><b class="mono">{{ m(res(job)!.metrics.chamfer.value) }}</b>
                <small>{{ t('result.chamferSub', { d: res(job)!.metrics.max_dist }) }}</small>
              </div>
              <div class="me-kpi">
                <span>{{ t('result.inliers') }}</span><b class="mono">{{ pct(res(job)!.registration.inlier_fraction) }}</b>
                <small>{{ t('result.regRmse') }} {{ m(res(job)!.registration.rmse) }}</small>
              </div>
            </div>
            <div class="me-dim">
              {{ t('result.truncated', { e: num(res(job)!.metrics.chamfer.truncated_est), r: num(res(job)!.metrics.chamfer.truncated_ref) }) }}
              · {{ t('result.cloudPoints', { e: num(res(job)!.estimate.points), ee: num(res(job)!.estimate.eval_points), r: num(res(job)!.reference.points), re: num(res(job)!.reference.eval_points), v: res(job)!.params.eval_voxel }) }}
              <template v-if="res(job)!.metrics.crop">
                · {{ t('result.cropInfo', { e: num(res(job)!.metrics.crop!.estimate_kept), r: num(res(job)!.metrics.crop!.reference_kept) }) }}
              </template>
              · {{ t('result.seconds', { s: res(job)!.seconds.total }) }}
            </div>

            <div class="me-two">
              <div class="me-box">
                <div class="me-box-title">{{ t('result.registration') }}</div>
                <div class="me-kv">
                  <span>{{ t('form.init') }}</span>
                  <b>{{ t(`form.init${res(job)!.registration.init.charAt(0).toUpperCase()}${res(job)!.registration.init.slice(1)}`) }} · {{ res(job)!.registration.method }}</b>
                  <span>{{ t('result.converged') }}</span>
                  <b>{{ !res(job)!.registration.refine ? t('result.notRun')
                    : `${res(job)!.registration.converged ? t('result.yes') : t('result.no')} · ${t('result.iterations', { n: res(job)!.registration.iterations })}` }}</b>
                  <span>{{ t('result.pose') }}</span>
                  <b class="mono">x {{ fix(res(job)!.registration.pose.x) }} y {{ fix(res(job)!.registration.pose.y) }} z {{ fix(res(job)!.registration.pose.z) }} m ·
                    yaw {{ fix(res(job)!.registration.pose.yaw, 2) }}° roll {{ fix(res(job)!.registration.pose.roll, 2) }}° pitch {{ fix(res(job)!.registration.pose.pitch, 2) }}°</b>
                </div>
                <div v-if="res(job)!.registration.search" class="me-dim">
                  {{ t('result.search', { n: res(job)!.registration.search!.tried, yaw: res(job)!.registration.search!.best.yaw0, start: startText(res(job)!.registration.search!.best.start) }) }}
                </div>
                <div class="me-dim">{{ t('result.transform') }}</div>
                <pre class="me-matrix mono">{{ res(job)!.registration.transform.map((row) => row.map((v) => fix(v, 5).padStart(10)).join(' ')).join('\n') }}</pre>
              </div>
              <div class="me-box">
                <div class="me-box-title">{{ t('result.table') }}</div>
                <table class="me-table">
                  <thead>
                    <tr>
                      <th>{{ t('result.colTau') }}</th><th>{{ t('result.colPrecision') }}</th><th>{{ t('result.colCompleteness') }}</th>
                      <th>{{ t('result.colF') }}</th><th>{{ t('result.colInlierRmse') }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in res(job)!.metrics.table" :key="row.tau" :class="{ main: row.tau === res(job)!.metrics.tau }">
                      <td class="mono">{{ row.tau }}</td><td class="mono">{{ pct(row.precision) }}</td>
                      <td class="mono">{{ pct(row.completeness) }}</td><td class="mono">{{ pct(row.fscore) }}</td>
                      <td class="mono">{{ row.inlier_rmse == null ? '–' : row.inlier_rmse.toFixed(4) }}</td>
                    </tr>
                  </tbody>
                </table>
                <div class="me-box-title">{{ t('result.histogram') }}</div>
                <svg :viewBox="`0 0 ${W} ${H}`" class="me-hist" role="img">
                  <line :x1="PAD.l" :x2="W - PAD.r" :y1="H - PAD.b" :y2="H - PAD.b" class="axis" />
                  <line :x1="PAD.l" :x2="PAD.l" :y1="PAD.t" :y2="H - PAD.b" class="axis" />
                  <rect v-for="(b, i) in bars(res(job)!)" :key="i" :x="b.x" :y="b.y" :width="b.w" :height="b.h" class="bar">
                    <title>{{ b.from.toFixed(3) }}–{{ b.to.toFixed(3) }} m: {{ b.n.toLocaleString() }}</title>
                  </rect>
                  <line :x1="xOf(res(job)!, res(job)!.metrics.tau)" :x2="xOf(res(job)!, res(job)!.metrics.tau)" :y1="PAD.t" :y2="H - PAD.b" class="tau" />
                  <text :x="xOf(res(job)!, res(job)!.metrics.tau) + 4" :y="PAD.t + 10" class="lbl">τ</text>
                  <text :x="PAD.l - 4" :y="PAD.t + 8" class="lbl" text-anchor="end">{{ histMax(res(job)!).toLocaleString() }}</text>
                  <text :x="PAD.l - 4" :y="H - PAD.b" class="lbl" text-anchor="end">0</text>
                  <text v-for="f in [0, 0.25, 0.5, 0.75, 1]" :key="f" :x="xOf(res(job)!, f * res(job)!.histogram.max)" :y="H - 6" class="lbl" text-anchor="middle">
                    {{ (f * res(job)!.histogram.max).toFixed(2) }}
                  </text>
                </svg>
                <div class="me-dim">{{ t('result.overflow', { d: res(job)!.histogram.max, n: num(res(job)!.histogram.overflow) }) }}</div>
              </div>
            </div>

            <div class="me-box-title">{{ t('result.image') }}</div>
            <div class="me-legend">
              <span class="mono">{{ res(job)!.image.vmin.toFixed(2) }} m</span>
              <i :style="{ background: gradient(res(job)!) }" />
              <span class="mono">≥ {{ res(job)!.image.vmax.toFixed(2) }} m</span>
              <span class="me-dim">{{ t('result.legend') }}</span>
            </div>
            <a class="me-image" :href="kit.fileUrl(job.id, 'error_top.png')" target="_blank" rel="noopener"><img :src="kit.fileUrl(job.id, 'error_top.png')" alt=""></a>
            <div class="me-actions">
              <span class="me-dim">{{ t('result.downloads') }}:</span>
              <a :href="kit.fileUrl(job.id, 'result.json')" download>{{ t('result.resultJson') }}</a>
              <a :href="kit.fileUrl(job.id, 'error_top.png')" download>{{ t('result.imagePng') }}</a>
              <a :href="kit.fileUrl(job.id, 'aligned.pcd')" download>{{ t('result.alignedPcd') }}</a>
            </div>
          </div>
        </template>
      </JobList>
    </GlassPanel>
  </div>
</template>

<style scoped>
.me-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.me-intro {
  margin: 0 0 10px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--ink-2);
}
.me-dim {
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--ink-3);
}
.me-warn {
  padding: 6px 10px;
  border-radius: 6px;
  font-size: 12.5px;
  line-height: 1.6;
  color: color-mix(in srgb, var(--warn) 70%, var(--ink));
  background: color-mix(in srgb, var(--warn) 10%, transparent);
}
.me-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 10px 20px;
  align-items: center;
}
.me-field {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  font-size: 13px;
  color: var(--ink);
}
.me-field > span:first-child {
  white-space: nowrap;
}
.me-field .el-input-number {
  width: 120px;
  margin-left: auto;
}
.me-right {
  margin-left: auto;
}
.wide {
  grid-column: 1 / -1;
}
.me-select {
  width: min(440px, 100%);
}
.me-opt {
  float: right;
  margin-left: 16px;
  font-size: 12px;
  color: var(--ink-3);
}
.me-sub {
  margin-top: 6px;
  padding-top: 8px;
  border-top: 1px solid var(--hairline);
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ink-2);
}
.me-refs {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
}
.me-refs-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  font-size: 13px;
}
.me-ref {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
  font-size: 12.5px;
}
.me-hidden {
  display: none;
}
.me-run {
  margin-top: 14px;
}
.me-compact {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
  font-size: 12.5px;
  color: var(--ink-2);
}
.me-result {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.me-kpis {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 8px;
}
.me-kpi {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  font-size: 12px;
  color: var(--ink-3);
}
.me-kpi.accent {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--hairline));
}
.me-kpi b {
  font-size: 17px;
  font-weight: 600;
  color: var(--ink);
}
.me-kpi small {
  font-size: 11.5px;
  line-height: 1.4;
}
.me-two {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
  gap: 12px;
}
.me-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  min-width: 0;
}
.me-box-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--ink-2);
}
.me-kv {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 12px;
  font-size: 12.5px;
  color: var(--ink-3);
}
.me-kv b {
  font-weight: 500;
  color: var(--ink);
}
.me-matrix {
  margin: 0;
  padding: 6px 8px;
  overflow-x: auto;
  font-size: 12px;
  line-height: 1.5;
  color: var(--ink);
  border-radius: 6px;
  background: color-mix(in srgb, var(--ink) 5%, transparent);
}
.me-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}
.me-table th,
.me-table td {
  padding: 3px 6px;
  text-align: right;
  border-bottom: 1px solid var(--hairline);
}
.me-table th {
  font-weight: 500;
  color: var(--ink-3);
}
.me-table td {
  color: var(--ink);
}
.me-table tr.main td {
  font-weight: 600;
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}
.me-hist {
  width: 100%;
  height: auto;
}
.me-hist .axis {
  stroke: var(--ink-3);
  stroke-width: 1;
}
.me-hist .bar {
  fill: var(--accent);
  opacity: 0.85;
}
.me-hist .tau {
  stroke: var(--bad);
  stroke-width: 1.2;
  stroke-dasharray: 4 3;
}
.me-hist .lbl {
  font-size: 10px;
  fill: var(--ink-3);
}
.me-legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  font-size: 12px;
  color: var(--ink-2);
}
.me-legend i {
  display: inline-block;
  width: 200px;
  height: 10px;
  border-radius: 3px;
}
.me-image {
  display: block;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: #fff;
  cursor: zoom-in;
}
.me-image img {
  display: block;
  max-width: 100%;
  max-height: 720px;
  margin: 0 auto;
}
.me-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  font-size: 12.5px;
}
.me-actions a {
  color: var(--accent);
}
</style>
