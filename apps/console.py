# -*- coding: utf-8 -*-
"""console — 本地中文控制台（阶段E, 单页 HTTP 服务, 无前端框架）。

提供: 直播状态/日程/话术/运行记录/人工控制。
只展示真实状态; 证据缺失显示为灰/红, 不显示绿色成功。
用法: python -m apps.console --profile fallback_8g
"""
import os
import sys
import json
import time
import threading
import urllib.request
import argparse
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from apps import common_config, storage   # noqa: E402

log = logging.getLogger('console')

# Vue3 控制台构建产物(console_vue/dist): 存在则优先托管, 缺失回退内联 PAGE
_DIST = os.path.join(_REPO, 'console_vue', 'dist')
_MIME = {'.js': 'application/javascript', '.css': 'text/css',
         '.html': 'text/html; charset=utf-8', '.svg': 'image/svg+xml',
         '.png': 'image/png', '.ico': 'image/x-icon', '.woff2': 'font/woff2'}

PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>数字人直播控制台</title>
<style>
body{font-family:system-ui;background:#111;color:#eee;margin:0;padding:16px}
h2{color:#8cf;margin:12px 0 6px} table{border-collapse:collapse;width:100%;margin-bottom:12px}
td,th{border:1px solid #444;padding:4px 8px;font-size:13px}
.ok{color:#4c4}.bad{color:#f66}.gray{color:#999}
button{padding:4px 10px;margin:2px;font-size:13px}
textarea,input{width:100%;background:#222;color:#eee;border:1px solid #555}
#status span{margin-right:16px}
</style></head><body>
<h2>直播状态</h2><div id="status">加载中…</div>
<h2>人工控制</h2>
<button onclick="api('/control/pause')">暂停讲解</button>
<button onclick="api('/control/resume')">恢复讲解</button>
<button onclick="if(confirm('停止当前播报?'))api('/control/interrupt')">停止当前回答</button>
<button onclick="if(confirm('人工接管将锁定自动派发, 确认?'))api('/control/takeover')">人工接管</button>
<button onclick="api('/control/release')">解除接管</button>
<button onclick="if(confirm('人工结束当前场次? 定时器不会自动重启该场。'))api('/control/manual_end')">人工结束场次</button>
插播: <input id="saytext" style="width:50%"><button onclick="sayNow()">立即插播</button>
<h2>智能问答（LLM chat）</h2>
<div id="llmstat" class="gray" style="font-size:12px">加载中…</div>
提问: <input id="chat_text" style="width:50%" placeholder="大模型生成回答并口播; 带货价格/承诺请用知识库">
<button onclick="chatNow()">让数字人回答</button>
<h2>声音与会话</h2>
<div id="sndsess">加载中…</div>
<button onclick="audioprobe(this)">声音链路自检(约1分钟)</button>
<pre id="probe" style="background:#222;padding:6px;font-size:12px;display:none;white-space:pre-wrap"></pre>
<h2>实时互动（弹幕）</h2>
<div class="gray" style="font-size:12px">来源说明: manual=人工粘贴；<b>ocr=自动读直播伴侣窗口（阶段G, 需保持伴侣窗口可见）</b>；mock=模拟器（开发测试）；real=平台接口（未接入）。</div>
<div style="margin-top:6px">
  <button onclick="ocrCtl('start')">启动弹幕OCR</button>
  <button onclick="ocrCtl('stop')">停止OCR</button>
  <span id="ocrstat" class="gray" style="font-size:12px"></span>
</div>
用户 <input id="dm_user" style="width:12%" placeholder="昵称(选填)">
内容 <input id="dm_text" style="width:45%" placeholder="粘贴或输入一条弹幕">
<button onclick="ingestDm('manual')">录入真实弹幕</button>
<button onclick="ingestDm('mock')">模拟一条</button>
<table id="danmaku"></table>
<h2>知识库（FAQ）</h2>
标题 <input id="kb_title" style="width:18%">
关键词 <input id="kb_kw" style="width:18%" placeholder="逗号分隔, 检索用">
分类 <input id="kb_cat" style="width:10%">
<div>正文(口播回答) <textarea id="kb_body" rows="2"></textarea></div>
来源 <input id="kb_src" style="width:25%" placeholder="来源说明(价格/时效需注明)">
<button onclick="addKb()">添加并启用</button>
<table id="knowledge"></table>
<h2>直播计划</h2><table id="schedules"></table>
名称 <input id="sch_name" style="width:15%"> 时间 <input id="sch_time" style="width:8%" placeholder="20:00">
时长(分) <input id="sch_dur" style="width:6%" value="60">
<button onclick="addSchedule()">添加(每天)</button>
<h2>话术库</h2><table id="scripts"></table>
新话术: <textarea id="sc_content" rows="3"></textarea>
<button onclick="addScript()">添加并启用</button>
<h2>运行记录</h2><table id="events"></table>
<script>
async function api(p, body){
  const r = await fetch(p, {method:'POST', headers:{'Content-Type':'application/json'},
    body: body ? JSON.stringify(body) : '{}'});
  return r.json();
}
async function sayNow(){
  const t = document.getElementById('saytext').value;
  if(!t) return alert('输入插播文本');
  const r = await api('/control/say', {text: t});
  alert(r.ok ? ('已受理 '+r.job_id) : ('失败: '+(r.msg||r.code)));
}
async function chatNow(){
  const t = document.getElementById('chat_text').value;
  if(!t) return alert('输入问题');
  const r = await api('/control/chat', {text: t});
  alert(r.ok ? '已交给大模型流式口播' : ('失败: '+(r.msg||r.code)));
}
async function audioprobe(btn){
  btn.disabled=true; btn.textContent='自检中…(约1分钟)';
  const p=document.getElementById('probe'); p.style.display='block';
  p.style.color='#eee'; p.textContent='运行中…（首次会先合成测试句, 请稍等）';
  try{
    const r = await api('/control/audioprobe', {});
    p.textContent = r.output || r.msg || '无输出';
    p.style.color = r.ok ? '#4c4' : '#f66';
  }catch(e){ p.textContent='自检失败: '+e; p.style.color='#f66'; }
  btn.disabled=false; btn.textContent='声音链路自检(约1分钟)';
}
async function addSchedule(){
  const name=document.getElementById('sch_name').value;
  const st=document.getElementById('sch_time').value;
  const du=parseInt(document.getElementById('sch_dur').value||'60');
  if(!name||!st) return alert('名称和时间必填');
  const r = await api('/schedule/add', {name, start_time: st, duration_min: du});
  if(r.ok) load();
}
async function addScript(){
  const c = document.getElementById('sc_content').value;
  if(!c) return alert('内容必填');
  const r = await api('/script/add', {name: '脚本'+Date.now()%1000, content: c});
  if(r.ok) load();
}
async function toggleSchedule(id, en){ await api('/schedule/toggle', {id, enabled: en}); load(); }
async function toggleScript(id, en){ await api('/script/toggle', {id, enabled: en}); load(); }
async function ingestDm(src){
  const t = document.getElementById('dm_text').value;
  if(!t) return alert('弹幕内容必填');
  const u = document.getElementById('dm_user').value || (src==='mock' ? '模拟用户' : '匿名');
  const r = await api('/danmaku/ingest', {source: src, user: u, text: t});
  if(r.ok) document.getElementById('dm_text').value='';
  else alert('录入失败: '+(r.msg||''));
}
async function ocrCtl(a){
  const r = await api('/danmaku/ocr/'+a, {});
  if(r && r.ok===false) alert(a==='start'?('启动失败: '+(r.msg||'')):('停止失败: '+(r.msg||'')));
  load();
}
async function addKb(){
  const title=document.getElementById('kb_title').value;
  const body=document.getElementById('kb_body').value;
  if(!title||!body) return alert('标题与正文必填');
  const r = await api('/knowledge/add', {title, body,
    keywords: document.getElementById('kb_kw').value,
    category: document.getElementById('kb_cat').value,
    source: document.getElementById('kb_src').value, allow_speak: true});
  if(r.ok){ document.getElementById('kb_title').value=''; document.getElementById('kb_body').value=''; load(); }
}
async function load(){
  try{
    const s = await (await fetch('/api/status')).json();
    const ph = s.phase||'?', sess = s.session||{};
    const cls = v => v==='live'||v==='ok'||v===true?'ok':(v==='failed'||v===false?'bad':'gray');
    const pf = s.platform||{};
    document.getElementById('status').innerHTML =
      `<span>阶段: <b class="${cls(ph)}">${ph}</b></span>`+
      `<span>会话: <b class="${cls(sess.state)}">${sess.state||'无'}</b></span>`+
      `<span>收帧: ${sess.frames_received??'—'}</span>`+
      `<span>GPU: ${s.gpu? s.gpu.mem_used+'MiB/'+s.gpu.temp+'C':'—'}</span>`+
      `<span>队列: ${s.pending??'—'}</span>`+
      `<span>导演: ${s.director||'—'}</span><br>`+
      `<span>直播伴侣: <b class="${pf.running?'ok':'bad'}">${pf.running?'运行中':'未运行(需人工打开登录)'}</b></span>`+
      `<span>平台开播状态: <b class="gray">${pf.live||'unknown'}</b>（伴侣无自动查询, 需人工确认）</span>`;
    // 声音与会话诊断（2026-09-13）: 绑定粘性 + 多标签页抢声音可见化
    const vs = s.vendor_sessions;
    let snd = `绑定会话: <b>${sess.sessionid||'无'}</b>（${sess.state||'—'}） · `+
              `vendor 活跃会话: <b>${vs? vs.length : '—'}</b>`;
    if(vs && vs.length>1)
      snd += `<br><b class="bad">⚠ 活跃会话 ${vs.length} 个 — 多余标签页会分走声音, 只保留直播用的 8010 页面, 其余关闭</b>`;
    if(!sess.sessionid || sess.state==='failed')
      snd += `<br><b class="bad">会话未绑定/已断开 → 打开 http://127.0.0.1:8010/index.html 点「开始连接」</b>`;
    document.getElementById('sndsess').innerHTML = snd;
    document.getElementById('llmstat').innerHTML = (s.llm && s.llm.enabled)
      ? `已启用: provider=${s.llm.provider} model=${s.llm.model}（不走队列, 大模型流式生成后口播）`
      : '未启用 — 开启方法见 docs/operations.md 六·八（未启用时 chat 会报上游失败, 知识库/插播不受影响）';
    document.getElementById('schedules').innerHTML =
      '<tr><th>ID</th><th>名称</th><th>规则</th><th>开始</th><th>时长</th><th>启用</th></tr>'+
      (s.schedules||[]).map(x=>`<tr><td>${x.id}</td><td>${x.name}</td><td>${x.repeat_rule}</td>`+
        `<td>${x.start_time}</td><td>${x.duration_min}分</td>`+
        `<td><button onclick="toggleSchedule(${x.id},${x.enabled?0:1})">${x.enabled?'停用':'启用'}</button></td></tr>`).join('');
    document.getElementById('scripts').innerHTML =
      '<tr><th>ID</th><th>名称</th><th>内容</th><th>启用</th></tr>'+
      (s.scripts||[]).map(x=>`<tr><td>${x.id}</td><td>${x.name}</td><td>${x.content.slice(0,60)}</td>`+
        `<td><button onclick="toggleScript(${x.id},${x.enabled?0:1})">${x.enabled?'停用':'启用'}</button></td></tr>`).join('');
    document.getElementById('events').innerHTML =
      '<tr><th>时间</th><th>类型</th><th>详情</th></tr>'+
      (s.events||[]).map(e=>`<tr><td>${new Date(e.ts*1000).toLocaleTimeString()}</td>`+
        `<td>${e.kind}</td><td>${(e.detail||'').slice(0,80)}</td></tr>`).join('');
    const dm = s.danmaku||{};
    const st = dm.stats||{};
    document.getElementById('danmaku').innerHTML =
      `<tr><th colspan=5>统计: 收${st.received||0} 答${st.answered||0} 略${st.ignored||0} `+
      `过期${st.expired||0} 合并${st.merged||0} 重复${st.dup||0} 失败${st.failed||0}`+
      `（接管中自动回答暂停, pending保留）</th></tr>`+
      '<tr><th>时间</th><th>来源</th><th>用户</th><th>内容</th><th>状态</th></tr>'+
      (dm.recent||[]).map(x=>`<tr><td>${new Date(x.ts*1000).toLocaleTimeString()}</td>`+
        `<td>${x.source}</td><td>${x.user}</td><td>${x.text.slice(0,40)}</td>`+
        `<td class="${x.decision==='answered'?'ok':(x.decision==='pending'?'gray':'')}">${x.decision}${x.reason?('('+x.reason.slice(0,20)+')'):''}</td></tr>`).join('');
    const oc = s.danmaku_ocr||{};
    document.getElementById('ocrstat').innerHTML = oc.running
      ? ('<b class="ok">OCR运行中</b> pid='+(oc.pid||'-')+
         ' · 累计投递 '+(oc.lines_total||0)+' 条'+
         (oc.last_cycle_s!=null?(' · 单轮 '+oc.last_cycle_s+'s'):'')+
         (oc.window_ok===false?(' · <b class="bad">取不到伴侣窗口: '+(oc.note||'')+'</b>'):'')+
         (oc.last_text?(' · 末条: '+String(oc.last_text).slice(0,20)):''))
      : ('<span class="gray">OCR未运行'+(oc.note?(' · '+oc.note):'')+'</span>');
    const kb = s.knowledge||[];
    document.getElementById('knowledge').innerHTML =
      '<tr><th>ID</th><th>标题</th><th>分类</th><th>版本</th><th>时效</th><th>口播</th><th>启用</th></tr>'+
      kb.map(x=>`<tr><td>${x.id}</td><td>${x.title}</td><td>${x.category||''}</td>`+
        `<td>v${x.version}</td><td>${x.valid_until? new Date(x.valid_until*1000).toLocaleDateString():'长期'}</td>`+
        `<td>${x.allow_speak?'是':'否'}</td><td>${x.enabled?'启用':'停用'}</td></tr>`).join('');
  }catch(e){
    document.getElementById('status').innerHTML = '<b class="bad">编排器不可达: '+e+'</b>';
  }
}
load(); setInterval(load, 3000);
</script></body></html>"""


class Console:
    def __init__(self, orch_port: int):
        self.orch_port = orch_port

    def _orch(self, path: str, method='GET', body=None):
        url = f'http://127.0.0.1:{self.orch_port}{path}'
        data = json.dumps(body or {}).encode() if method == 'POST' else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read().decode())


def make_handler(console: Console):
    def json_resp(h, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        h.send_response(code)
        h.send_header('Content-Type', 'application/json; charset=utf-8')
        h.send_header('Content-Length', str(len(b)))
        h.end_headers()
        h.wfile.write(b)

    class Handler(BaseHTTPRequestHandler):
        def _body(self):
            n = int(self.headers.get('Content-Length') or 0)
            try:
                return json.loads(self.rfile.read(n) or b'{}')
            except Exception:
                return {}

        def do_GET(self):
            if self.path == '/' or self.path == '/index.html':
                idx = os.path.join(_DIST, 'index.html')
                if os.path.exists(idx):        # Vue 构建版优先
                    with open(idx, 'rb') as f:
                        b = f.read()
                else:
                    b = PAGE.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif self.path.startswith('/assets/'):
                fp = os.path.normpath(os.path.join(
                    _DIST, self.path.lstrip('/')))
                ok = fp.startswith(_DIST) and os.path.exists(fp)
                if not ok:
                    return json_resp(self, {'msg': 'not found'}, 404)
                ext = os.path.splitext(fp)[1].lower()
                with open(fp, 'rb') as f:
                    b = f.read()
                self.send_response(200)
                self.send_header('Content-Type',
                                 _MIME.get(ext, 'application/octet-stream'))
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif self.path == '/api/status':
                try:
                    s = console._orch('/healthz')
                    st = console._orch('/status')
                    s['pending'] = st.get('pending')
                    s['jobs'] = st.get('jobs')
                    try:
                        s['director'] = (DIRECTOR_API.get('status') or (lambda: {}))()
                    except Exception:
                        s['director'] = {'state': DIRECTOR_STATE.get('state', '—')}
                    s['platform'] = dict(PLATFORM_STATE)
                    s['platform_caps'] = _caps_safe()
                    lt = (PROFILE_CFG.get('obj') or {}).get('livetalking', {}) or {}
                    s['llm'] = {'enabled': bool(lt.get('llm_provider')),
                                'provider': lt.get('llm_provider', ''),
                                'model': lt.get('llm_model', '')}
                    s['vendor_sessions'] = st.get('vendor_sessions')
                    s['schedules'] = storage.list_schedules()
                    s['scripts'] = storage.list_scripts()
                    s['events'] = storage.recent_events(30)
                    pipe = PIPELINE.get('obj')
                    s['danmaku'] = pipe.snapshot() if pipe else {}
                    s['danmaku_ocr'] = _ocr_status()
                    s['knowledge'] = _knowledge_list_safe()
                except Exception as e:
                    return json_resp(self, {'error': str(e)}, 502)
                json_resp(self, s)
            else:
                json_resp(self, {'msg': 'not found'}, 404)

        def do_POST(self):
            b = self._body()
            try:
                if self.path == '/control/pause':
                    DIRECTOR_API['pause'](); json_resp(self, {'ok': True})
                elif self.path == '/control/resume':
                    DIRECTOR_API['resume'](); json_resp(self, {'ok': True})
                elif self.path == '/control/interrupt':
                    json_resp(self, console._orch('/interrupt', 'POST'))
                elif self.path == '/control/takeover':
                    DIRECTOR_API['takeover'](); json_resp(self, {'ok': True})
                elif self.path == '/control/release':
                    DIRECTOR_API['release'](); json_resp(self, {'ok': True})
                elif self.path == '/control/manual_end':
                    # 人工结束: 场次置 manual_end, 调度器不自动重启（阶段H）
                    sched = SCHED.get('thread')
                    if sched:
                        sid = sched.manual_end_active('人工从控制台结束')
                        json_resp(self, {'ok': True, 'ended': sid})
                    else:
                        json_resp(self, {'ok': False, 'msg': '调度器未运行'})
                elif self.path == '/control/say':
                    json_resp(self, console._orch('/say', 'POST', b))
                elif self.path == '/control/chat':
                    # LLM 智能问答: 编排器 /chat 透传 vendor type=chat（不排队）
                    json_resp(self, console._orch('/chat', 'POST', b))
                elif self.path == '/control/audioprobe':
                    json_resp(self, _audio_probe())
                elif self.path == '/schedule/add':
                    new_sc = {'id': -1,
                              'name': b.get('name', '未命名'),
                              'repeat_rule': b.get('repeat_rule', 'daily'),
                              'start_time': b.get('start_time', '20:00'),
                              'duration_min': int(b.get('duration_min', 60)),
                              'profile': b.get('profile', 'fallback_8g')}
                    # 阶段F: 保存前冲突检测（单直播间, 重叠即拒绝）
                    sched = SCHED.get('thread')
                    if sched:
                        conflict = sched.find_conflict(new_sc)
                        if conflict:
                            json_resp(self, {'ok': False, 'code': 'schedule_conflict',
                                             'msg': f'与计划 {conflict[0]}[{conflict[1]}] 时间重叠, '
                                                    f'首版不并行开播'}, 409)
                            return
                    sid = storage.add_schedule(
                        new_sc['name'], new_sc['repeat_rule'], new_sc['start_time'],
                        new_sc['duration_min'], new_sc['profile'],
                        b.get('script_file'), b.get('knowledge_version'))
                    storage.log_event('info', f'新增直播计划 id={sid}')
                    json_resp(self, {'ok': True, 'id': sid})
                elif self.path == '/schedule/toggle':
                    storage.set_schedule_enabled(b['id'], bool(b.get('enabled')))
                    json_resp(self, {'ok': True})
                elif self.path == '/session/manual_end':
                    # 人工结束当前场次: 最高优先级, 定时器/看门狗不得重启该场
                    sched = SCHED.get('thread')
                    ended = sched.manual_end_active() if sched else None
                    json_resp(self, {'ok': True, 'ended_session': ended})
                elif self.path == '/script/add':
                    sid = storage.add_script(b.get('name', '未命名'),
                                             b.get('content', ''))
                    storage.log_event('info', f'新增话术 id={sid}')
                    json_resp(self, {'ok': True, 'id': sid})
                elif self.path == '/script/toggle':
                    storage.execute('UPDATE scripts SET enabled=?, updated_at=? WHERE id=?',
                                    (1 if b.get('enabled') else 0, time.time(), b['id']))
                    json_resp(self, {'ok': True})
                elif self.path == '/danmaku/ocr/start':
                    json_resp(self, _ocr_start())
                elif self.path == '/danmaku/ocr/stop':
                    json_resp(self, _ocr_stop())
                elif self.path == '/danmaku/ingest':
                    # 弹幕入口: source ∈ manual|ocr|mock（real 需平台自动接入, 当前不可用）
                    pipe = PIPELINE.get('obj')
                    if not pipe:
                        json_resp(self, {'ok': False, 'msg': '管线未启动'})
                    else:
                        json_resp(self, pipe.ingest(
                            b.get('source', 'manual'), b.get('user', ''),
                            b.get('text', '')))
                elif self.path == '/knowledge/add':
                    from apps import knowledge
                    kid = knowledge.add_entry(
                        b.get('title', '未命名'), b.get('body', ''),
                        b.get('category', ''), b.get('keywords', ''),
                        b.get('source', ''), float(b.get('valid_until', 0) or 0),
                        bool(b.get('allow_speak', True)))
                    storage.log_event('info', f'新增知识条目 id={kid} {b.get("title","")[:20]}')
                    json_resp(self, {'ok': True, 'id': kid})
                else:
                    json_resp(self, {'msg': 'not found'}, 404)
            except Exception as e:
                json_resp(self, {'ok': False, 'msg': str(e)}, 500)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


DIRECTOR_API = {}
DIRECTOR_STATE = {'state': '未启动'}
PLATFORM_STATE = {'name': '—', 'running': None, 'live': 'unknown'}
SCHED = {'thread': None}
PIPELINE = {'obj': None}
# 阶段G: 弹幕 OCR worker（独立解释器进程, 见 apps/danmaku_ocr.py）
OCR_STATE = {'proc': None, 'profile': None, 'started_at': 0.0, 'error': ''}
PROFILE_CFG = {'obj': None}


def _ocr_status():
    """给控制台页面用的 OCR 状态: 进程状态 + worker 自己写的 status 文件。"""
    import json as _json
    import os as _os
    st = {'running': False, 'window_ok': None, 'lines_total': 0, 'note': '',
          'pid': None, 'last_cycle_s': None, 'lines_last_cycle': None}
    p = OCR_STATE.get('proc')
    if p is not None and p.poll() is None:
        st['running'] = True
        st['pid'] = p.pid
    elif OCR_STATE.get('error'):
        st['note'] = OCR_STATE['error']
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    f = _os.path.join(repo, 'logs', 'danmaku_ocr.status.json')
    if _os.path.isfile(f):
        try:
            d = _json.load(open(f, encoding='utf-8'))
            if time.time() - float(d.get('ts') or 0) < 30:      # 只认新鲜数据
                st.update({k: d.get(k) for k in
                           ('window_ok', 'lines_total', 'note', 'last_cycle_s',
                            'lines_last_cycle', 'last_text') if k in d})
        except Exception:
            pass
    return st


def _ocr_start():
    """拉起 OCR worker: 用配置里指定的解释器（装了 rapidocr 的那个, 非项目 venv）。"""
    import os as _os
    import subprocess as _sp
    if OCR_STATE.get('proc') is not None and OCR_STATE['proc'].poll() is None:
        return {'ok': True, 'msg': '已在运行', 'pid': OCR_STATE['proc'].pid}
    cfg = PROFILE_CFG.get('obj') or {}
    oc = cfg.get('danmaku_ocr') or {}
    py = oc.get('python') or 'python'
    if not _os.path.isfile(py):
        OCR_STATE['error'] = f'OCR 解释器不存在: {py}（改配置 danmaku_ocr.python）'
        return {'ok': False, 'msg': OCR_STATE['error']}
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    logf = open(_os.path.join(repo, 'logs', 'danmaku_ocr.log'), 'ab')
    # 用 -m 包名方式跑: sys.path[0] 是 cwd(仓库根), 不会让 apps/platform.py
    # 遮蔽标准库 platform（脚本方式运行时会, 见 danmaku_ocr.py 顶部注释）
    cmd = [py, '-X', 'utf8', '-m', 'apps.danmaku_ocr',
           '--profile', OCR_STATE.get('profile') or 'fallback_8g']
    try:
        OCR_STATE['proc'] = _sp.Popen(cmd, cwd=repo, stdout=logf, stderr=logf)
        OCR_STATE['started_at'] = time.time()
        OCR_STATE['error'] = ''
        storage.log_event('info', f'启动弹幕OCR pid={OCR_STATE["proc"].pid}')
        return {'ok': True, 'pid': OCR_STATE['proc'].pid}
    except Exception as e:
        OCR_STATE['error'] = f'启动失败: {e}'
        return {'ok': False, 'msg': OCR_STATE['error']}


def _ocr_stop():
    p = OCR_STATE.get('proc')
    if p is None or p.poll() is not None:
        OCR_STATE['proc'] = None
        return {'ok': True, 'msg': '未在运行'}
    try:
        p.terminate()
        p.wait(timeout=5)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    OCR_STATE['proc'] = None
    OCR_STATE['error'] = ''
    storage.log_event('info', '停止弹幕OCR')
    return {'ok': True}


def _audio_probe():
    """服务端音频链路自检（scripts/probe_audio_path.py）:
    用与页面完全相同的 WebRTC 方式连 LiveTalking, 派发测试句并统计收到的
    音频帧与 RMS。退出码 0=服务端有音频(问题只可能在页面/系统音量/伴侣采集),
    2=没收到音频帧(服务端链路问题), 1=连接/协议错误。"""
    import subprocess as _sp
    cfg = PROFILE_CFG.get('obj') or {}
    py = (cfg.get('paths') or {}).get('python_livetalking', '')
    py = common_config.resolve_path('', py) if py else sys.executable
    if not os.path.isfile(py):
        py = sys.executable
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        r = _sp.run([py, '-X', 'utf8', os.path.join('scripts', 'probe_audio_path.py')],
                    cwd=repo, capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=180)
        out = ((r.stdout or '') + '\n' + (r.stderr or '')).strip()
        return {'ok': r.returncode == 0, 'code': r.returncode,
                'output': '\n'.join(out.splitlines()[-25:])}
    except _sp.TimeoutExpired:
        return {'ok': False, 'msg': '自检超时（>180s）'}
    except Exception as e:
        return {'ok': False, 'msg': str(e)}


def _caps_safe():
    try:
        from apps.platform import get_adapter
        return get_adapter('douyin_companion').capabilities()
    except Exception as e:
        return {'error': str(e)}


def _knowledge_list_safe():
    try:
        from apps import knowledge
        return [{'id': k['id'], 'title': k['title'], 'category': k['category'],
                 'valid_until': k['valid_until'], 'allow_speak': k['allow_speak'],
                 'version': k['version'], 'enabled': k['enabled']}
                for k in knowledge.list_entries()]
    except Exception as e:
        return {'error': str(e)}


def _platform_probe(console):
    """周期探测平台进程（不阻塞状态接口）。"""
    import threading as _t

    def _loop():
        from apps.platform import get_adapter
        ad = get_adapter('douyin_companion')
        while True:
            try:
                running = ad.companion_running()
                PLATFORM_STATE.update({'name': ad.name, 'running': running,
                                       'live': ad.live_status()})
            except Exception:
                pass
            time.sleep(10)

    _t.Thread(target=_loop, daemon=True).start()


def main():
    global DIRECTOR_API, DIRECTOR_STATE
    ap = argparse.ArgumentParser(description='数字人直播本地控制台')
    ap.add_argument('--profile', default=None)
    ap.add_argument('--port', type=int, default=8030)
    ap.add_argument('--no-scheduler', action='store_true', help='禁用定时调度（调试）')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    profile = common_config.load_profile(args.profile)
    PROFILE_CFG['obj'] = profile                    # 供弹幕OCR等按配置拉起子进程
    OCR_STATE['profile'] = args.profile or 'fallback_8g'
    orch_port = profile.get('orchestrator', {}).get('listen_port', 8020)
    console = Console(orch_port)

    # 导演随控制台启动（复用编排器 HTTP /say /interrupt 作为执行接口）
    from apps.director import Director

    class _OrchStub:
        RUN_ID = 'console'

    def _say(text):
        try:
            return console._orch('/say', 'POST', {'text': text})
        except Exception as e:
            return {'ok': False, 'msg': str(e)}

    def _interrupt():
        try:
            return console._orch('/interrupt', 'POST')
        except Exception:
            return {}

    def _status():
        try:
            return console._orch('/status')
        except Exception:
            return {}

    idle_gap = float(profile.get('director', {}).get('idle_gap_s', 300))
    director = Director(_OrchStub(), _say, _interrupt, status_fn=_status,
                        idle_gap_s=idle_gap)
    DIRECTOR_API.update({'pause': director.pause, 'resume': director.resume,
                         'takeover': director.takeover, 'release': director.release,
                         'status': director.status})
    threading.Thread(target=director.run_forever, daemon=True).start()
    DIRECTOR_STATE['state'] = '运行中'

    # 弹幕管线（阶段G）: 人工粘贴/模拟器接入; 接管时暂停自动生成回答但保留 pending
    from apps.danmaku import DanmakuPipeline
    pipeline = DanmakuPipeline(_say)
    _orig_takeover, _orig_release = director.takeover, director.release

    def _tk():
        pipeline.takeover.set(); _orig_takeover()

    def _rl():
        _orig_release(); pipeline.takeover.clear()

    DIRECTOR_API.update({'takeover': _tk, 'release': _rl})
    pipeline.start()
    PIPELINE['obj'] = pipeline

    # 平台探测（直播伴侣进程/状态, 10s 一轮, 不阻塞 UI）
    _platform_probe(console)

    # 定时调度（阶段F）: 开播动作=预检+置ready并提示人工在伴侣点开播
    if not args.no_scheduler:
        from apps.scheduler import LiveScheduler

        def _launch(sc, run_id):
            # 预检: 编排器 live + 平台伴侣在运行; 两者缺一不可开播
            try:
                phase = console._orch('/healthz').get('phase')
            except Exception:
                return False, '编排器不可达, 无法开播'
            if phase != 'live':
                return False, f'编排器未就绪(phase={phase}), 不开播'
            if not PLATFORM_STATE.get('running'):
                return False, '抖音直播伴侣未运行, 需人工打开并登录'
            storage.log_event('info',
                              f'到点开播预检通过: 请在伴侣中点击「开始视频直播」, '
                              f'数字人画面由虚拟摄像头/窗口源提供（run={run_id}）', run_id)
            return True, '预检通过, 等待人工在伴侣点开播（能力=MANUAL, 如实记录）'

        def _end(run_id):
            # 下播无法自动: 人工在伴侣点停止, 调度只记录并结束本地话术
            storage.log_event('info', f'计划场次到点: 数字人讲解已停止(需人工在伴侣停止直播)', run_id)

        sched = LiveScheduler(_launch, _end)
        sched.start()
        SCHED['thread'] = sched

    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(console))
    log.info('控制台: http://127.0.0.1:%s  (编排器 %s)', args.port, orch_port)
    server.serve_forever()


if __name__ == '__main__':
    main()
