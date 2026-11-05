<script setup>
import { onBeforeUnmount, ref } from 'vue'
import { ElAlert, ElButton, ElCard, ElInput, ElTag, ElSelect, ElOption } from 'element-plus'

defineProps({
  writeMode: { type: String, required: true },
  modelMode: { type: String, required: true },
})

const tokenInput = ref('')
const connected = ref(false)
const connecting = ref(false)
const message = ref('尚未连接')
const error = ref(false)
// 会话凭证只存组件内存，不写 URL、浏览器存储、cookie 或控制台。
let sessionToken = null
let activeRequest = null
const roots = ref([])
const selectedRoot = ref('')
const job = ref(null)
const scanning = ref(false)
const scanMessage = ref('')
const submitting = ref(false)
let scanController = null
let pollTimer = null
let pendingKey = null
let pendingRoot = null
let sessionEpoch = 0

const reasonLabels = {
  excluded_name: '隐藏项或排除目录', symbolic_link: '符号链接',
  different_device: '其他设备', not_regular_file: '非普通文件',
  extension_not_allowed: '扩展名不支持', unavailable_directory: '无法遍历的目录',
  directory_unavailable: '目录无法访问，未将其中旧文件标为缺失',
  stat_failed: '无法获取文件信息', changed_during_read: '读取期间持续变化',
  file_too_large: '超过单文件大小上限', unsafe_or_unreadable_source: '文件不可读或路径不安全',
  source_not_found: '读取前文件已消失', read_failed: '读取失败',
  unsafe_source_path: '路径校验失败', previous_paths_not_observable: '旧路径当前不可扫描',
  max_files_exceeded: '候选文件超过本机配置上限，本批未应用',
  invalid_scan_configuration: '扫描配置无效', database_error: '数据库操作失败，本批已回滚',
  source_unavailable: '来源无法访问，本批未应用', scan_state_changed: '扫描期间来源或登记状态变化，请重试',
  scan_aborted: '扫描中止，本批未应用', partial_scan: '部分扫描未成功', service_restarted: '服务重启，任务已中断',
}
const countLabels = {
  scanned: '候选文件', stable: '稳定读取', added: '新增身份', initialized: '首次补哈希',
  modified: '内容更新', renamed: '确认改名', metadata_updated: '元数据更新',
  unchanged: '未变化', missing: '确认缺失', unavailable: '暂不可用', failed: '读取失败', skipped: '跳过项',
}

// 清除页面凭证、报告和待确认申请，取消页面请求与轮询，不取消已受理的后台任务。
function clearSession() {
  sessionEpoch += 1
  scanController?.abort()
  clearTimeout(pollTimer)
  scanController = null
  pollTimer = null
  roots.value = []
  selectedRoot.value = ''
  job.value = null
  scanning.value = false
  submitting.value = false
  scanMessage.value = ''
  pendingKey = null
  pendingRoot = null
  activeRequest?.abort()
  activeRequest = null
  sessionToken = null
  tokenInput.value = ''
  connected.value = false
  connecting.value = false
}

// 用输入的 token 验证本机会话，加载授权根并自动查询最近扫描报告。
async function connect() {
  if (connecting.value) return
  const candidate = tokenInput.value.trim()
  tokenInput.value = ''
  if (!candidate) {
    error.value = true
    message.value = '请粘贴终端中的会话 token。'
    return
  }
  connecting.value = true
  error.value = false
  message.value = '正在验证会话…'
  const controller = new AbortController()
  activeRequest = controller
  sessionToken = candidate
  try {
    const response = await fetch('/api/status', {
      headers: { Authorization: `Bearer ${sessionToken}` },
      credentials: 'omit', cache: 'no-store', redirect: 'error',
      signal: controller.signal,
    })
    if (!response.ok) throw new Error('authentication failed')
    const status = await response.json()
    if (status.authenticated !== true) throw new Error('authentication failed')
    if (activeRequest !== controller) return
    sessionToken = candidate
    connected.value = true
    roots.value = status.roots || []
    selectedRoot.value = roots.value[0]?.id || ''
    message.value = '已连接，可以使用本机会话。'
    if (selectedRoot.value) await loadLatest()
  } catch {
    if (activeRequest !== controller) return
    sessionToken = null
    error.value = true
    message.value = '连接失败，请确认服务已启动并重新输入终端中的 token。'
  } finally {
    if (activeRequest === controller) {
      activeRequest = null
      connecting.value = false
    }
  }
}

// 统一携带会话凭证发送扫描相关请求，校验响应并解析 JSON。
async function scanFetch(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...options.headers, Authorization: `Bearer ${sessionToken}` },
    credentials: 'omit', cache: 'no-store', redirect: 'error',
    signal: scanController.signal,
  })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}

// 更新页面任务和提示，仅在任务等待或运行时安排下一次状态查询。
function showJob(value) {
  job.value = value
  scanning.value = value && ['queued', 'running'].includes(value.state)
  const labels = { queued: '等待扫描', running: '正在扫描，完成后显示实际计数',
    succeeded: value?.result?.complete ? '扫描完成' : '扫描结束，部分范围或文件未成功',
    failed: '扫描中止，未应用本批文件更新', interrupted: '上次扫描被中断，请重新扫描' }
  scanMessage.value = value ? labels[value.state] : '尚无扫描记录'
  if (scanning.value) pollTimer = setTimeout(pollJob, 500)
}

// 查询所选根最近一次任务，供重新连接、切换根或手动刷新报告使用。
async function loadLatest() {
  const epoch = sessionEpoch
  clearTimeout(pollTimer)
  scanController?.abort()
  scanController = new AbortController()
  try {
    const data = await scanFetch(`/api/scans/latest/${encodeURIComponent(selectedRoot.value)}`)
    if (connected.value && epoch === sessionEpoch) showJob(data.job)
  } catch {
    if (connected.value && epoch === sessionEpoch) scanMessage.value = '无法读取历史扫描报告。'
  }
}

// 按当前任务编号查询状态和报告，查询失败时停止自动轮询并提示用户。
async function pollJob() {
  const epoch = sessionEpoch
  if (!connected.value || !job.value) return
  try {
    const value = await scanFetch(`/api/jobs/${encodeURIComponent(job.value.id)}`)
    if (connected.value && epoch === sessionEpoch) showJob(value)
  } catch {
    if (connected.value && epoch === sessionEpoch) {
      scanning.value = false
      scanMessage.value = '查询中断；后台任务可能仍在执行，请刷新扫描报告。'
    }
  }
}

// 准备或复用申请编号提交扫描，收到任务编号后启动状态查询。
async function startScan() {
  // 1. 页面必须已连接并选定根，且没有正在提交或执行的任务，才允许继续。
  if (!connected.value || scanning.value || submitting.value || !selectedRoot.value) return
  const epoch = sessionEpoch
  submitting.value = true
  scanController?.abort()
  scanController = new AbortController()
  // 2. 新申请生成编号；结果不确定的同一根申请保留旧编号供安全重试。
  // 上次提交是否成功尚不明确时复用原申请编号，避免网络重试创建第二个任务。
  if (!pendingKey || pendingRoot !== selectedRoot.value) {
    pendingKey = crypto.randomUUID()
    pendingRoot = selectedRoot.value
  }
  try {
    // 3. POST 只申请后台任务，提交根编号、校验开关与幂等编号，不等待全部扫描完成。
    const result = await scanFetch('/api/scans', { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': pendingKey },
      body: JSON.stringify({ root_id: selectedRoot.value, verify_content: true }) })
    if (!connected.value || epoch !== sessionEpoch) return
    // 4. 收到任务编号后清除待确认申请，先显示等待状态，再由 showJob 安排真实状态查询。
    pendingKey = null
    pendingRoot = null
    job.value = { id: result.job_id, state: 'queued' }
    showJob(job.value)
  } catch {
    // 5. 未收到明确受理结果时不丢弃申请编号；页面提示刷新报告或用原编号重试。
    if (connected.value && epoch === sessionEpoch) scanMessage.value = '提交失败或状态未知，请刷新报告；重试会复用本次请求编号。'
  } finally {
    if (epoch === sessionEpoch) submitting.value = false
  }
}

// 清空本页面会话及查询状态，并显示已断开连接提示。
function disconnect() {
  clearSession()
  error.value = false
  message.value = '已断开连接'
}

window.addEventListener('pagehide', disconnect)
onBeforeUnmount(() => {
  clearSession()
  window.removeEventListener('pagehide', disconnect)
})
</script>

<template>
  <main class="workspace">
    <header class="heading">
      <p class="eyebrow">本地文件助手</p>
      <h1>FileMindDemo</h1>
      <p class="subtitle">在你的电脑上，安全地开始。</p>
    </header>

    <ElCard class="session-card" shadow="never">
      <div class="card-heading">
        <h2>本机会话</h2>
        <ElTag :type="connected ? 'success' : 'info'" effect="light">
          {{ connected ? '已连接' : '未连接' }}
        </ElTag>
      </div>
      <div class="modes">
        <span>文件操作模式：<ElTag>{{ writeMode }}</ElTag></span>
        <span>模型模式：<ElTag type="warning">{{ modelMode }}</ElTag></span>
      </div>
      <p class="hint">Mock 使用模拟结果，不调用真实 AI。真实模型功能接入后由后端调用。</p>

      <form v-if="!connected" class="session-form" autocomplete="off" @submit.prevent="connect">
        <label for="session-token">会话 token</label>
        <p id="token-help" class="hint">从启动程序的本机终端复制 token。刷新页面或重启服务后，请重新输入。</p>
        <ElInput
          id="session-token"
          v-model="tokenInput"
          type="password"
          autocomplete="off"
          :spellcheck="false"
          aria-describedby="token-help"
          placeholder="粘贴终端中的会话 token"
          :disabled="connecting"
        />
        <ElButton type="primary" native-type="submit" :loading="connecting">连接本地服务</ElButton>
      </form>
      <ElButton v-else @click="disconnect">断开连接</ElButton>
      <div class="feedback" role="status" aria-live="polite">
        <ElAlert :title="message" :type="error ? 'error' : connected ? 'success' : 'info'" :closable="false" />
      </div>
    </ElCard>
    <ElCard v-if="connected" class="scan-card" shadow="never">
      <div class="card-heading"><h2>扫描授权资料</h2></div>
      <p class="hint">只读取本机已授权目录；不会修改资料，也不会调用模型。</p>
      <p v-if="!roots.length">没有可用的授权根。请在本机 TOML 或 CLI 配置并登记目录后重启服务。</p>
      <template v-else>
        <div class="scan-actions">
          <ElSelect v-model="selectedRoot" aria-label="授权根别名" :disabled="scanning || submitting" @change="loadLatest">
            <ElOption v-for="root in roots" :key="root.id" :label="root.alias" :value="root.id" />
          </ElSelect>
          <ElButton type="primary" :loading="scanning || submitting" @click="startScan">扫描目录</ElButton>
          <ElButton :disabled="submitting" @click="loadLatest">刷新扫描报告</ElButton>
        </div>
        <p role="status" aria-live="polite">{{ scanMessage }}</p>
        <template v-if="job">
          <p class="hint">扫描编号：{{ job.id }}</p>
          <p v-if="job.started_at" class="hint">开始：{{ new Date(job.started_at).toLocaleString() }}</p>
          <p v-if="job.finished_at" class="hint">结束：{{ new Date(job.finished_at).toLocaleString() }}</p>
          <p v-if="job.error_code" class="hint">任务说明：{{ reasonLabels[job.error_code] || job.error_code }}</p>
          <template v-if="job.result">
            <dl class="scan-counts">
              <div v-for="(value, key) in job.result.counts" :key="key"><dt>{{ countLabels[key] || key }}</dt><dd>{{ value }}</dd></div>
            </dl>
            <h3>跳过原因</h3>
            <p v-if="!Object.keys(job.result.skipped).length">没有跳过项。</p>
            <ul v-else><li v-for="(value, key) in job.result.skipped" :key="key">{{ reasonLabels[key] || key }}：{{ value }}</li></ul>
            <h3>未成功项</h3>
            <p v-if="!job.result.errors.length">没有错误。</p>
            <ul v-else><li v-for="(item, index) in job.result.errors" :key="index">{{ item.relpath || '授权根' }}：{{ reasonLabels[item.error_code] || item.error_code }}</li></ul>
            <p class="hint">暂不可用和读取失败的文件不会作为可用内容；确认缺失只改变登记状态，不删除原文件。</p>
          </template>
        </template>
      </template>
    </ElCard>
    <footer>会话凭证仅保存在当前页面内存中。</footer>
  </main>
</template>
