// 统一 API 封装: 控制台后端即 console.py(:8030), 生产同源, 开发走 vite proxy
async function post(path, body = {}) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  })
  if (!r.ok) throw new Error(`${path} HTTP ${r.status}`)
  return r.json()
}

export const api = {
  status: async () => (await fetch('/api/status')).json(),
  control: (action, payload = {}) => post(`/control/${action}`, payload),
  scheduleAdd: (payload) => post('/schedule/add', payload),
  scheduleToggle: (payload) => post('/schedule/toggle', payload),
  scriptAdd: (payload) => post('/script/add', payload),
  scriptToggle: (payload) => post('/script/toggle', payload),
  knowledgeAdd: (payload) => post('/knowledge/add', payload),
  danmakuOcr: (action) => post(`/danmaku/ocr/${action}`)
}
