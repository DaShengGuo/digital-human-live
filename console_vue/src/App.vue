<script setup>
import { useLiveStore } from './stores/live'
import { computed } from 'vue'

const live = useLiveStore()
live.startPolling()

const phaseType = computed(() => {
  const map = { live: 'success', idle: 'info', error: 'danger', degraded: 'warning' }
  return map[live.phase] || 'info'
})
</script>

<template>
  <el-container style="height: 100vh">
    <el-aside width="200px" class="aside">
      <div class="logo">dhlive 控制台</div>
      <el-menu router :default-active="$route.path">
        <el-menu-item index="/"><el-icon><Monitor /></el-icon>运行状态</el-menu-item>
        <el-menu-item index="/interact"><el-icon><ChatDotRound /></el-icon>直播互动</el-menu-item>
        <el-menu-item index="/content"><el-icon><Notebook /></el-icon>话术与知识库</el-menu-item>
      </el-menu>
      <div class="foot">
        <el-tag :type="phaseType" size="small">{{ live.phase }}</el-tag>
        <div v-if="live.lastError" class="err">{{ live.lastError }}</div>
      </div>
    </el-aside>
    <el-main><router-view /></el-main>
  </el-container>
</template>

<style>
body { margin: 0; font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; }
.aside { border-right: 1px solid #e4e7ed; display: flex; flex-direction: column; }
.logo { font-weight: 700; padding: 16px; }
.foot { margin-top: auto; padding: 12px; }
.err { color: #f56c6c; font-size: 12px; margin-top: 6px; word-break: break-all; }
.card-gap { margin-bottom: 16px; }
</style>
