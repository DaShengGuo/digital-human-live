import { defineStore } from 'pinia'
import { api } from '../api'

// 直播间全局状态: 2s 轮询 /api/status 聚合快照, 控制动作走同一入口
export const useLiveStore = defineStore('live', {
  state: () => ({
    status: null,
    lastError: '',
    lastUpdated: 0,
    pollTimer: null,
    controlled: false     // 人工接管中
  }),
  getters: {
    phase: (s) => s.status?.phase || '—',
    ok: (s) => !!s.status?.ok,
    gpuMem: (s) => (s.status?.gpu ? `${s.status.gpu.mem_used}/${s.status.gpu.mem_total}G` : '—'),
    gpuTemp: (s) => s.status?.gpu?.temp ?? null,
    pending: (s) => s.status?.pending ?? 0,
    speaking: (s) => !!s.status?.session?.speaking,
    degraded: (s) => !!s.status?.degraded,
    directorState: (s) => s.status?.director?.state || '—',
    danmaku: (s) => s.status?.danmaku || {},
    knowledge: (s) => s.status?.knowledge || [],
    scripts: (s) => s.status?.scripts || [],
    schedules: (s) => s.status?.schedules || [],
    events: (s) => s.status?.events || []
  },
  actions: {
    startPolling(intervalMs = 2000) {
      this.stopPolling()
      this.refresh()
      this.pollTimer = setInterval(() => this.refresh(), intervalMs)
    },
    stopPolling() {
      if (this.pollTimer) clearInterval(this.pollTimer)
      this.pollTimer = null
    },
    async refresh() {
      try {
        this.status = await api.status()
        this.lastError = ''
        this.lastUpdated = Date.now()
      } catch (e) {
        this.lastError = String(e.message || e)
      }
    },
    async control(action, payload = {}) {
      const r = await api.control(action, payload)
      await this.refresh()
      return r
    }
  }
})
