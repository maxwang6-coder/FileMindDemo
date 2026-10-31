<script setup>
import { onBeforeUnmount, ref } from 'vue'
import { ElAlert, ElButton, ElCard, ElInput, ElTag } from 'element-plus'

defineProps({
  writeMode: { type: String, required: true },
  modelMode: { type: String, required: true },
})

const tokenInput = ref('')
const connected = ref(false)
const connecting = ref(false)
const message = ref('尚未连接')
const error = ref(false)
// Only in component memory: no URL, browser storage, cookie, or console output.
let sessionToken = null
let activeRequest = null

function clearSession() {
  activeRequest?.abort()
  activeRequest = null
  sessionToken = null
  tokenInput.value = ''
  connected.value = false
  connecting.value = false
}

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
    message.value = '已连接，可以使用本机会话。'
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
    <footer>会话凭证仅保存在当前页面内存中。</footer>
  </main>
</template>
