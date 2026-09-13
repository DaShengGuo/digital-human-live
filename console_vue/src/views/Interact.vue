<script setup>
// 直播互动: 弹幕管线快照(虚拟滚动) + OCR 读屏开关
import { computed, ref } from 'vue'
import { useLiveStore } from '../stores/live'
import { api } from '../api'
import { ElMessage } from 'element-plus'
import VirtualList from '../components/VirtualList.vue'

const live = useLiveStore()
const ocrBusy = ref(false)

const stats = computed(() => ({
  total: live.danmaku.total ?? 0,
  answered: live.danmaku.answered ?? 0,
  ignored: live.danmaku.ignored ?? 0,
  pending: live.danmaku.pending ?? 0
}))
const ocrOn = computed(() => !!live.status?.danmaku_ocr?.running)
const recent = computed(() => [...(live.danmaku.recent || [])].reverse())

async function toggleOcr() {
  ocrBusy.value = true
  try {
    await api.danmakuOcr(ocrOn.value ? 'stop' : 'start')
    ElMessage.success(ocrOn.value ? '已停止' : '已启动')
  } catch (e) {
    ElMessage.error(String(e.message))
  } finally { ocrBusy.value = false }
}
</script>

<template>
  <div>
    <el-row :gutter="12" class="card-gap">
      <el-col :span="6"><el-card shadow="never"><div class="k">累计弹幕</div><div class="v">{{ stats.total }}</div></el-card></el-col>
      <el-col :span="6"><el-card shadow="never"><div class="k">已回答</div><div class="v" style="color:#67c23a">{{ stats.answered }}</div></el-card></el-col>
      <el-col :span="6"><el-card shadow="never"><div class="k">已忽略/频控</div><div class="v" style="color:#909399">{{ stats.ignored }}</div></el-card></el-col>
      <el-col :span="6"><el-card shadow="never">
        <div class="k">OCR 读屏</div>
        <el-switch :model-value="ocrOn" :disabled="ocrBusy" @change="toggleOcr" active-text="开" />
      </el-card></el-col>
    </el-row>

    <el-card shadow="never">
      <template #header>弹幕流（{{ recent.length }} 条在快照中，虚拟滚动渲染）</template>
      <VirtualList :items="recent" :height="460" :row-height="34" v-slot="{ item }">
        <div class="dm">
          <span class="user">{{ item.user || '匿名' }}</span>
          <span class="text">{{ item.text }}</span>
          <el-tag size="small" :type="item.decision === 'answered' ? 'success'
            : (item.decision === 'pending' ? 'info' : 'danger')" class="dec">{{ item.decision }}</el-tag>
          <span v-if="item.reason" class="reason">{{ item.reason }}</span>
        </div>
      </VirtualList>
    </el-card>
  </div>
</template>

<style scoped>
.k { color: #909399; font-size: 12px; }
.v { font-size: 18px; font-weight: 600; }
.dm { display: flex; align-items: center; gap: 8px; padding: 0 8px; font-size: 13px; line-height: 34px; }
.dm .user { color: #409eff; min-width: 90px; overflow: hidden; text-overflow: ellipsis; }
.dm .text { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dm .reason { color: #c0c4cc; font-size: 12px; }
</style>
