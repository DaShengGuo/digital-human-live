<script setup>
// 运行状态: 状态卡 + 进程/会话 + 事件流 + 控制条(接管/暂停/打断/口播)
import { ref, computed } from 'vue'
import { useLiveStore } from '../stores/live'
import { ElMessage } from 'element-plus'

const live = useLiveStore()
const sayText = ref('')
const busy = ref(false)

const procs = computed(() => {
  const p = live.status?.procs || {}
  return Object.entries(p).map(([name, v]) => ({ name, ...v }))
})
const eventsDesc = computed(() => [...live.events].reverse())

async function doControl(action, tip) {
  busy.value = true
  try {
    const r = await live.control(action)
    ElMessage.success(`${tip} OK`)
    return r
  } catch (e) {
    ElMessage.error(`${tip} 失败: ${e.message}`)
  } finally {
    busy.value = false
  }
}

async function say() {
  if (!sayText.value.trim()) return
  busy.value = true
  try {
    await live.control('say', { text: sayText.value })
    ElMessage.success('已进入口播队列')
    sayText.value = ''
  } catch (e) {
    ElMessage.error(String(e.message))
  } finally { busy.value = false }
}
</script>

<template>
  <div>
    <el-row :gutter="12" class="card-gap">
      <el-col :span="4"><el-card shadow="never"><div class="k">阶段</div><div class="v">{{ live.phase }}</div></el-card></el-col>
      <el-col :span="4"><el-card shadow="never"><div class="k">GPU 显存</div><div class="v">{{ live.gpuMem }}</div></el-card></el-col>
      <el-col :span="4"><el-card shadow="never"><div class="k">温度</div><div class="v">{{ live.gpuTemp ? live.gpuTemp + '℃' : '—' }}</div></el-card></el-col>
      <el-col :span="4"><el-card shadow="never"><div class="k">待播队列</div><div class="v">{{ live.pending }}</div></el-card></el-col>
      <el-col :span="4"><el-card shadow="never"><div class="k">说话中</div><div class="v">{{ live.speaking ? '是' : '否' }}</div></el-card></el-col>
      <el-col :span="4"><el-card shadow="never"><div class="k">导演</div><div class="v">{{ live.directorState }}<el-tag v-if="live.degraded" type="warning" size="small" style="margin-left:4px">已降级</el-tag></div></el-card></el-col>
    </el-row>

    <el-card shadow="never" class="card-gap">
      <template #header>控制</template>
      <el-space wrap>
        <el-button :disabled="busy" @click="doControl('takeover', '人工接管')">人工接管</el-button>
        <el-button :disabled="busy" @click="doControl('release', '恢复自动')">恢复自动</el-button>
        <el-button :disabled="busy" @click="doControl('pause', '暂停')">暂停</el-button>
        <el-button :disabled="busy" @click="doControl('resume', '恢复')">恢复</el-button>
        <el-button type="warning" :disabled="busy" @click="doControl('interrupt', '打断')">打断口播</el-button>
        <el-button type="danger" :disabled="busy" @click="doControl('manual_end', '结束本场')">结束本场</el-button>
      </el-space>
      <div style="margin-top:12px; display:flex; gap:8px">
        <el-input v-model="sayText" placeholder="输入要口播的文本…" @keyup.enter="say" />
        <el-button type="primary" :disabled="busy" @click="say">口播</el-button>
      </div>
    </el-card>

    <el-row :gutter="12">
      <el-col :span="10">
        <el-card shadow="never">
          <template #header>进程</template>
          <el-table :data="procs" size="small">
            <el-table-column prop="name" label="进程" />
            <el-table-column prop="state" label="状态" />
            <el-table-column prop="pid" label="PID" width="80" />
          </el-table>
          <div v-if="live.status?.llm" style="margin-top:8px; font-size:13px">
            LLM: {{ live.status.llm.enabled ? `${live.status.llm.provider}/${live.status.llm.model}` : '未启用' }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="14">
        <el-card shadow="never">
          <template #header>事件流（最近 {{ eventsDesc.length }} 条）</template>
          <VirtualList :items="eventsDesc" :height="320" :row-height="30" v-slot="{ item }">
            <div class="evt">
              <span class="t">{{ new Date((item.ts || 0) * 1000).toLocaleTimeString() }}</span>
              <span class="tag">{{ item.kind }}</span>
              <span>{{ item.msg }}</span>
            </div>
          </VirtualList>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.k { color: #909399; font-size: 12px; }
.v { font-size: 18px; font-weight: 600; }
.evt { font-size: 13px; line-height: 30px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; padding: 0 8px; }
.evt .t { color: #909399; margin-right: 8px; }
.evt .tag { color: #409eff; margin-right: 8px; }
</style>
