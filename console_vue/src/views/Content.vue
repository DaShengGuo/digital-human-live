<script setup>
// 话术与知识库: 循环话术管理 + 定时开播日程 + FAQ 条目(RAG 向量检索数据源)
import { ref, computed } from 'vue'
import { useLiveStore } from '../stores/live'
import { api } from '../api'
import { ElMessage } from 'element-plus'

const live = useLiveStore()
const scriptForm = ref({ title: '', body: '', weight: 1 })
const kbForm = ref({ title: '', body: '', keywords: '', category: 'FAQ' })
const scriptToggleBusy = ref(-1)
const kbAdding = ref(false)

const scriptRows = computed(() => live.scripts.map(s => ({
  id: s.id, title: s.title, body: s.body, enabled: !!s.enabled, weight: s.weight
})))
const scheduleRows = computed(() => live.schedules.map(s => ({
  id: s.id, name: s.name, cron: s.cron, enabled: !!s.enabled
})))

async function addScript() {
  if (!scriptForm.value.title || !scriptForm.value.body) return
  try {
    await api.scriptAdd(scriptForm.value)
    Object.assign(scriptForm.value, { title: '', body: '', weight: 1 })
    ElMessage.success('话术已添加')
  } catch (e) { ElMessage.error(String(e.message)) }
}
async function toggleScript(row) {
  scriptToggleBusy.value = row.id
  try { await api.scriptToggle({ id: row.id, enabled: row.enabled ? 0 : 1 }) }
  catch (e) { ElMessage.error(String(e.message)) }
  finally { scriptToggleBusy.value = -1 }
}
async function toggleSchedule(row) {
  try { await api.scheduleToggle({ id: row.id, enabled: row.enabled ? 0 : 1 }) }
  catch (e) { ElMessage.error(String(e.message)) }
}
async function addKnowledge() {
  if (!kbForm.value.title || !kbForm.value.body) return
  kbAdding.value = true
  try {
    await api.knowledgeAdd(kbForm.value)
    Object.assign(kbForm.value, { title: '', body: '', keywords: '', category: 'FAQ' })
    ElMessage.success('已入库，RAG 服务将在 60s 内自动重建索引')
  } catch (e) { ElMessage.error(String(e.message)) }
  finally { kbAdding.value = false }
}
</script>

<template>
  <div>
    <el-row :gutter="12">
      <el-col :span="14">
        <el-card shadow="never" class="card-gap">
          <template #header>循环话术（{{ scriptRows.length }}）</template>
          <el-form :inline="true" size="small">
            <el-form-item><el-input v-model="scriptForm.title" placeholder="标题" style="width:140px" /></el-form-item>
            <el-form-item><el-input v-model="scriptForm.body" placeholder="话术内容" style="width:300px" /></el-form-item>
            <el-button size="small" type="primary" @click="addScript">添加</el-button>
          </el-form>
          <el-table :data="scriptRows" size="small" max-height="320">
            <el-table-column prop="title" label="标题" width="140" />
            <el-table-column prop="body" label="内容" show-overflow-tooltip />
            <el-table-column label="启用" width="90">
              <template #default="{ row }">
                <el-switch :model-value="row.enabled" :loading="scriptToggleBusy === row.id" @change="toggleScript(row)" />
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never">
          <template #header>定时开播日程（{{ scheduleRows.length }}）</template>
          <el-table :data="scheduleRows" size="small">
            <el-table-column prop="name" label="名称" />
            <el-table-column prop="cron" label="计划" />
            <el-table-column label="启用" width="90">
              <template #default="{ row }">
                <el-switch :model-value="row.enabled" @change="toggleSchedule(row)" />
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <el-col :span="10">
        <el-card shadow="never">
          <template #header>知识库条目（{{ live.knowledge.length }}，RAG 向量索引数据源）</template>
          <el-form size="small" label-width="60px">
            <el-form-item label="标题"><el-input v-model="kbForm.title" /></el-form-item>
            <el-form-item label="正文"><el-input v-model="kbForm.body" type="textarea" :rows="2" /></el-form-item>
            <el-form-item label="关键词"><el-input v-model="kbForm.keywords" placeholder="逗号分隔" /></el-form-item>
            <el-form-item label="分类"><el-input v-model="kbForm.category" style="width:120px" />
              <el-button type="primary" style="margin-left:12px" :loading="kbAdding" @click="addKnowledge">入库</el-button>
            </el-form-item>
          </el-form>
          <el-table :data="live.knowledge" size="small" max-height="260">
            <el-table-column prop="title" label="标题" width="130" />
            <el-table-column prop="body" label="正文" show-overflow-tooltip />
            <el-table-column prop="category" label="分类" width="70" />
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
