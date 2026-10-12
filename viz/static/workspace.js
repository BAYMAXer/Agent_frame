'use strict';

const byId = id => document.getElementById(id);
const viewNames = {run:'任务配置',live:'实时运行',local:'历史轨迹',chat:'模型对话'};
let activeView = 'run';
let followLatest = true;
let ioTab = 'output';
let runState = {status:'idle'};
let launchPending = false;
let stopPending = false;
let pollTimer = null;
let pollRequest = 0;
let socket = null;
let modelPresets = {decision_models:[],grounding_models:[]};
let historyRuns = [];
let detailOpen = false;
let processOutput = '';
const liveImageCache = new Map();
const orphanLogs = new Map();
const draftKey = 'agent-studio.windows.config.v1';
const draftFields = ['local-task','local-maxsteps','run-model','run-model-url','run-ground','run-ground-url','run-ground-type','model-timeout','ground-timeout','step-timeout'];
const typeLabels = {decision:'决策模型',grounding:'定位模型',format_retry:'格式重试',verify:'完成核对',reflect:'执行反思',code_agent:'子 Agent'};
const eventLabels = {task_start:'任务开始',step:'开始执行步骤',plan_update:'任务计划更新',executing:'正在执行动作',executed:'动作执行结果',task_end:'任务结束',error:'执行异常',interrupted:'已停止运行'};

function toast(message) {
  document.querySelector('.toast')?.remove();
  const el=document.createElement('div'); el.className='toast'; el.textContent=message; el.setAttribute('role','status'); document.body.appendChild(el);
  setTimeout(() => el.remove(),3000);
}
function feedback(message,error=false) {
  const el=byId('form-feedback'); el.hidden=!message; el.textContent=message || ''; el.classList.toggle('error',error);
}
async function api(url,options) {
  const response=await fetch(url,options);
  if (!response.ok) throw new Error('HTTP '+response.status);
  return response.json();
}
function switchView(view) {
  if (!viewNames[view]) return;
  activeView=view;
  Object.keys(viewNames).forEach(name => {
    byId('view-'+name).classList.toggle('active',name===view);
    byId('nav-'+name).classList.toggle('active',name===view);
    byId('nav-'+name).setAttribute('aria-current',name===view?'page':'false');
  });
  byId('statusbar').style.display=view==='live'?'flex':'none';
  setText('view-title',viewNames[view]);
  if (view==='local') loadLocalRuns();
  if (view==='chat') initChatView();
  if (view==='live') refreshWorkspace();
}
Object.keys(viewNames).forEach(view => byId('nav-'+view).addEventListener('click',() => switchView(view)));
byId('btn-open-live').addEventListener('click',() => switchView('live'));
byId('btn-prev').addEventListener('click',() => { setFollow(false); switchToGroup(currentGroup-1); });
byId('btn-next').addEventListener('click',() => { setFollow(false); switchToGroup(currentGroup+1); });

function readDraft() {
  try { const draft=JSON.parse(localStorage.getItem(draftKey)||'{}'); return draft && typeof draft==='object' && !Array.isArray(draft)?draft:{}; }
  catch { return {}; }
}
function saveDraft() {
  const draft=Object.fromEntries(draftFields.map(id => [id,byId(id).value]));
  try { localStorage.setItem(draftKey,JSON.stringify(draft)); setText('draft-status','已自动保存 · API Key 不保存'); }
  catch { setText('draft-status','草稿保存不可用，当前配置仍可运行'); }
}
function updateSummary() {
  setText('summary-model',byId('run-model').value || '待配置');
  setText('summary-ground',byId('run-ground').value || '待配置');
  setText('summary-budget','最多 '+byId('local-maxsteps').value+' 步 · 单步 '+byId('step-timeout').value+' 秒');
}
function applyPreset(type) {
  const decision=type==='decision';
  const model=byId(decision?'run-model':'run-ground');
  const preset=(decision?modelPresets.decision_models:modelPresets.grounding_models).find(item => item.name===model.value);
  if (!preset) return;
  byId(decision?'run-model-url':'run-ground-url').value=preset.url || '';
  byId(decision?'run-model-apikey':'run-ground-apikey').value=preset.api_key || 'EMPTY';
  if (!decision) byId('run-ground-type').value=preset.type || 'ui-tars';
}
async function initConfiguration() {
  const draft=readDraft();
  draftFields.forEach(id => { if (typeof draft[id]==='string') byId(id).value=draft[id]; });
  updateSummary();
  try {
    const data=await api('/model-presets');
    modelPresets={decision_models:data.decision_models || [],grounding_models:data.grounding_models || []};
    for (const type of ['decision','grounding']) {
      const decision=type==='decision';
      const presets=decision?modelPresets.decision_models:modelPresets.grounding_models;
      const list=byId(decision?'decision-model-options':'ground-model-options');
      list.replaceChildren(...presets.map(preset => { const option=document.createElement('option'); option.value=preset.name; return option; }));
      const modelId=decision?'run-model':'run-ground';
      if (!byId(modelId).value && presets.length) {
        byId(modelId).value=(decision?presets[0]:(presets.find(p => p.type==='ui-venus2') || presets[0])).name;
        applyPreset(type);
      } else {
        // Restore credentials from the named preset, while retaining draft URL/protocol edits.
        const preset=presets.find(p => p.name===byId(modelId).value);
        if (preset) byId(decision?'run-model-apikey':'run-ground-apikey').value=preset.api_key || 'EMPTY';
      }
    }
    if (data.error) feedback('模型预设读取失败，可手动填写模型和地址。',true);
  } catch { feedback('模型预设读取失败，可手动填写模型和地址。',true); }
  updateSummary();
  api('/experiments/default-prompt?os_name=windows&browser_only=false').then(data => {
    setText('preview-system-prompt',data.ok?data.prompt:'提示词读取失败：'+(data.error || '未知错误'));
  }).catch(() => setText('preview-system-prompt','提示词预览暂时不可用'));
}
draftFields.forEach(id => byId(id).addEventListener('input',() => { updateSummary(); saveDraft(); }));
byId('run-model').addEventListener('change',() => { applyPreset('decision'); updateSummary(); saveDraft(); });
byId('run-ground').addEventListener('change',() => { applyPreset('grounding'); updateSummary(); saveDraft(); });
document.querySelectorAll('[data-task]').forEach(button => button.addEventListener('click',() => {
  byId('local-task').value=button.dataset.task; byId('local-task').focus(); saveDraft();
}));

function integerField(id,label,maximum) {
  const value=Number(byId(id).value);
  if (!Number.isInteger(value) || value<1 || value>maximum) { byId(id).focus(); throw new Error(label+'必须是 1–'+maximum+' 的整数'); }
  return value;
}
function modelConfiguration() {
  const config={model:byId('run-model').value.trim(),model_url:byId('run-model-url').value.trim(),model_api_key:byId('run-model-apikey').value,
    ground_model:byId('run-ground').value.trim(),ground_url:byId('run-ground-url').value.trim(),ground_api_key:byId('run-ground-apikey').value,ground_type:byId('run-ground-type').value};
  for (const [name,id,label] of [['model','run-model','决策模型名称'],['ground_model','run-ground','定位模型名称']]) {
    if (!config[name]) { byId(id).focus(); throw new Error('请填写'+label); }
  }
  for (const [name,id,label] of [['model_url','run-model-url','决策模型'],['ground_url','run-ground-url','定位模型']]) {
    try { const url=new URL(config[name]); if (!['http:','https:'].includes(url.protocol)) throw new Error(); }
    catch { byId(id).focus(); throw new Error(label+' API 地址必须是有效的 HTTP 或 HTTPS 地址'); }
  }
  return config;
}
function configuration() {
  const task=byId('local-task').value.trim();
  if (!task) { byId('local-task').focus(); throw new Error('请先描述希望 Agent 完成的任务'); }
  return {mode:'local',task,...modelConfiguration(),max_steps:integerField('local-maxsteps','最大步数',100),
    model_timeout:integerField('model-timeout','决策超时',3600),ground_timeout:integerField('ground-timeout','定位超时',3600),step_timeout:integerField('step-timeout','单步超时',3600)};
}
function updateRunControls() {
  const running=['running','starting'].includes(runState.status);
  byId('btn-local-start').disabled=running || launchPending;
  byId('btn-local-start').textContent=launchPending?'正在启动…':running?'任务运行中':'开始运行 →';
  byId('btn-stop').disabled=stopPending;
  byId('btn-interrupt').disabled=stopPending;
  byId('btn-interrupt').style.display=running?'':'none';
  byId('run-running').hidden=!runState.task && !running;
  byId('btn-stop').hidden=!running;
  const labels={idle:'已停止',starting:'启动中',running:'运行中',done:'进程已结束',failed:'异常终止'};
  setText('run-cur-status',labels[runState.status] || runState.status);
  setText('run-cur-task',runState.task || '—');
  setText('run-cur-step',(runState.current_step || 0)+' / '+(runState.max_steps || '—'));
  setText('run-cur-elapsed',Number(runState.elapsed || 0).toFixed(1)+' s');
  setText('run-cur-pid',runState.pid?'PID '+runState.pid:'');
  byId('run-progress-fill').style.width=Math.min(100,100*(runState.current_step || 0)/(runState.max_steps || 1))+'%';
}
function resetLiveView() {
  taskGroups=[]; currentGroup=-1; totalTasks=1; seenKeys.clear(); orphanLogs.clear(); liveImageCache.clear(); fullSizeShots.clear(); processOutput='';
  byId('timeline').replaceChildren(); statusState={}; setFollow(true); refreshWorkspace();
}
byId('btn-local-start').addEventListener('click',async () => {
  if (launchPending || ['running','starting'].includes(runState.status)) return;
  let cfg; try { cfg=configuration(); } catch (error) { feedback(error.message,true); return; }
  launchPending=true; updateRunControls(); feedback('正在启动本地 Windows 任务…');
  try {
    const data=await api('/run/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
    if (!data.ok) throw new Error(data.error || '任务启动失败');
    ++pollRequest; resetLiveView(); runState={status:'starting',task:cfg.task,max_steps:cfg.max_steps,current_step:0};
    feedback('任务已启动，可在实时工作区查看过程。'); switchView('live');
    await loadHistory(); await pollRunStatus();
  } catch (error) { feedback('启动失败：'+error.message,true); toast('启动失败：'+error.message); }
  finally { launchPending=false; updateRunControls(); }
});
byId('btn-test').addEventListener('click',async () => {
  const button=byId('btn-test'); if (button.disabled) return;
  let cfg;
  try {
    const model=byId('run-model').value.trim(), model_url=byId('run-model-url').value.trim();
    const url=new URL(model_url); if (!model || !['http:','https:'].includes(url.protocol)) throw new Error('请填写决策模型名称和有效 API 地址');
    cfg={model,model_url,model_api_key:byId('run-model-apikey').value};
  } catch (error) { feedback(error.message==='Invalid URL'?'请填写有效的决策模型 API 地址':error.message,true); return; }
  button.disabled=true; button.textContent='正在测试连接…'; feedback('正在发送视觉测试请求…');
  try { const data=await api('/run/test-connection',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)}); if (!data.ok) throw new Error(data.error || 'HTTP '+data.status); feedback('连接成功 · 视觉请求正常'+(data.content?'：'+data.content:'')); }
  catch (error) { feedback('连接失败：'+error.message,true); }
  finally { button.disabled=false; button.textContent='测试决策模型连接'; }
});
async function stopRun() {
  if (stopPending) return; stopPending=true; updateRunControls();
  try {
    const data=await api('/run/stop',{method:'POST'}); if (!data.ok) throw new Error(data.error || '停止失败');
    const group=taskGroups.at(-1);
    if (group) {
      group.entries.filter(entry => entry.pending).forEach(entry => { entry.pending=false; entry.error=true; entry.response='运行已停止，未收到完整模型结果。'; });
      group.processStatus='idle';
    }
    ++pollRequest; runState={...runState,status:'idle'}; await loadHistory(); feedback('运行已停止，已记录的轨迹可继续查看。'); toast('运行已停止'); refreshWorkspace(); pollRunStatus();
  } catch (error) { toast('停止失败：'+error.message); feedback('停止失败：'+error.message,true); }
  finally { stopPending=false; updateRunControls(); }
}
byId('btn-stop').addEventListener('click',stopRun);
byId('btn-interrupt').addEventListener('click',stopRun);
async function pollRunStatus() {
  const request=++pollRequest;
  clearTimeout(pollTimer);
  try {
    const snapshot=await api('/run/status'); if (request!==pollRequest) return;
    runState=snapshot; updateRunControls(); recoverPlanSnapshot(runState);
    const group=taskGroups.at(-1); if (group) group.processStatus=runState.status;
    if (runState.task || ['running','starting','failed'].includes(runState.status)) await loadProcessOutput();
    renderGroupStatus();
  } catch { /* Connection indicator follows the socket; keep the last known process state. */ }
  finally { if (request===pollRequest) pollTimer=setTimeout(pollRunStatus,2000); }
}

function entryKey(payload) {
  if (payload.kind==='model' && payload.id!=null) return 'model:'+payload.id;
  if (payload.event==='plan_update') return 'plan';
  return [payload.kind,payload.event,payload.step,payload.ts].join(':');
}
function workspaceLog(group,payload) {
  if (!group.entries) group.entries=[];
  if (payload.event==='plan_update') {
    const revision=Number(payload.revision ?? payload.plan?.revision);
    if (Number.isFinite(revision) && group.planRevision!=null && revision<group.planRevision) return;
    group.planPayload=payload;
  }
  const key=entryKey(payload);
  const existing=group.entries.find(entry => entry.key===key);
  if (existing && existing.pending===false && payload.pending===true) return;
  const entry={...existing,...payload,key};
  if (payload.kind==='model') entry.pending=payload.pending===true;
  if (payload.event==='interrupted') {
    group.entries.filter(item => item.pending).forEach(item => { item.pending=false; item.error=true; item.response='运行已停止，未收到完整模型结果。'; });
  }
  // URLs supplied by the server preserve screenshots across reconnects without retaining base64 in every entry.
  if (payload.screenshot && !entry.screenshot_url) {
    const cacheKey=group.config.ts+':'+key;
    liveImageCache.set(cacheKey,'data:image/png;base64,'+payload.screenshot);
    while (liveImageCache.size>40) liveImageCache.delete(liveImageCache.keys().next().value);
    entry.imageCacheKey=cacheKey;
  }
  delete entry.screenshot;
  if (existing) group.entries[group.entries.indexOf(existing)]=entry;
  else group.entries.push(entry);
  if (group.entries.length>600) group.entries.splice(0,group.entries.length-600);
  if (followLatest && group===taskGroups[currentGroup]) group.selectedKey=group.entries.at(-1)?.key;
  if (!group.selectedKey && group.entries.length) group.selectedKey=group.entries.at(-1).key;
  if (group===taskGroups[currentGroup]) refreshWorkspace();
}
function selectedEntry() {
  const group=taskGroups[currentGroup];
  return group?.entries?.find(entry => entry.key===group.selectedKey) || null;
}
function setFollow(follow) {
  followLatest=follow;
  const button=byId('btn-follow'); button.classList.toggle('paused',!follow); button.setAttribute('aria-pressed',String(follow)); button.textContent=follow?'● 跟随最新':'Ⅱ 跟随已暂停';
  if (follow && taskGroups.length) { switchToGroup(taskGroups.length-1); const group=taskGroups[currentGroup]; group.selectedKey=group.entries?.at(-1)?.key; refreshWorkspace(); }
}
byId('btn-follow').addEventListener('click',() => setFollow(!followLatest));
function actionText(value) { return typeof value==='string'?value:value==null?'':JSON.stringify(value); }
function entryTitle(entry) {
  if (entry.kind==='model') return entry.prompt || typeLabels[entry.type] || '模型调用';
  if (entry.event==='task_start') return entry.instruction || '任务开始';
  if (entry.action) return actionText(entry.action);
  if (entry.error) return actionText(entry.error);
  return eventLabels[entry.event] || entry.event || '状态更新';
}
function entryMatches(entry,filter,query) {
  const decision=entry.kind==='model' && entry.type!=='grounding';
  const error=Boolean(entry.error) || entry.event==='error' || ['rejected','uncertain'].includes(entry.status);
  if (filter==='decision' && !decision || filter==='grounding' && entry.type!=='grounding' || filter==='status' && entry.kind!=='status' || filter==='error' && !error) return false;
  return !query || [entryTitle(entry),entry.prompt,entry.response,entry.raw_response,entry.step,entry.event,entry.type].join(' ').toLowerCase().includes(query.toLowerCase());
}
function renderEventList() {
  const list=byId('live-events'), group=taskGroups[currentGroup], entries=group?.entries || [];
  const filter=byId('live-filter').value, query=byId('live-search').value.trim();
  const visible=entries.filter(entry => entryMatches(entry,filter,query));
  setText('event-count',visible.length+' / '+entries.length);
  const scrollTop=list.scrollTop;
  if (!visible.length) { list.innerHTML='<div class="empty">'+(entries.length?'没有匹配的事件，试试其他筛选条件。':'任务启动后，执行步骤将在这里实时出现。')+'</div>'; return; }
  const fragment=document.createDocumentFragment();
  visible.forEach(entry => {
    const button=document.createElement('button'); button.className='event-item'; button.dataset.key=entry.key;
    button.classList.toggle('selected',entry.key===group.selectedKey); button.setAttribute('aria-pressed',String(entry.key===group.selectedKey));
    const error=Boolean(entry.error) || entry.event==='error' || ['rejected','uncertain'].includes(entry.status);
    const label=entry.kind==='model'?(typeLabels[entry.type] || '模型调用'):(eventLabels[entry.event] || entry.event);
    const time=entry.ts?new Date(entry.ts*1000).toLocaleTimeString('zh-CN',{hour12:false}):'';
    button.innerHTML='<div class="event-top"><i class="event-dot '+(error?'error':entry.type==='grounding'?'grounding':entry.kind==='model'?'decision':'')+'"></i><span>'+esc(label)+'</span><span class="event-time">'+esc(time)+'</span></div><div class="event-title">'+esc(entryTitle(entry))+'</div><div class="event-bottom"><span>'+(entry.step!=null?'STEP '+esc(entry.step):'TASK')+'</span><span'+(error?' class="error-text"':'')+'>'+(entry.pending?'等待模型响应':error?'异常 / 未确认':entry.elapsed!=null?esc(formatElapsed(entry.elapsed)):entry.status?esc(entry.status):'已记录')+'</span></div>';
    button.addEventListener('click',() => { setFollow(false); group.selectedKey=entry.key; refreshWorkspace(); });
    fragment.appendChild(button);
  });
  list.replaceChildren(fragment);
  if (followLatest) list.querySelector('.selected')?.scrollIntoView({block:'nearest'});
  else list.scrollTop=scrollTop;
}
['live-search','live-filter'].forEach(id => byId(id).addEventListener(id==='live-search'?'input':'change',renderEventList));
function renderGroupStatus() {
  const group=taskGroups[currentGroup];
  if (!group) {
    setText('st-taskno-text','—'); setText('st-step','—'); setText('st-example','—'); setText('st-instruction',runState.task || '等待启动任务'); setText('st-result','—');
    if (runState.status==='failed') { setStatus('failed','启动失败'); setText('st-result','在任务配置中查看进程日志'); }
    else if (runState.status==='done') { setStatus('ended','进程已结束'); setText('st-result','未收到任务记录，查看进程日志'); }
    else if (['starting','running'].includes(runState.status)) setStatus('running','正在初始化');
    else setStatus('ended','空闲');
    return;
  }
  const entries=group.entries || [];
  const lastStep=[...entries].reverse().find(entry => entry.step!=null);
  setText('st-step',(lastStep?.step || 0)+' / '+(group.config.max_steps || runState.max_steps || '?'));
  setText('st-taskno-text',(currentGroup+1)+' / '+taskGroups.length); setText('st-instruction',group.instruction); setText('st-example',group.example_id || '本机任务');
  const end=[...entries].reverse().find(entry => ['task_end','error','interrupted'].includes(entry.event));
  if (end?.event==='interrupted') { setStatus('ended','已停止'); setText('st-result','未完成'); }
  else if (end?.event==='error') { setStatus('failed','执行异常'); setText('st-result','异常终止'); }
  else if (end?.event==='task_end') {
    const success=end.success===true || (end.success==null && Number(end.result)>=1);
    setStatus(success?'done':'failed',success?'已完成':'未完成'); setText('st-result',success?'成功':end.result==null?'异常终止':'未完成');
  } else if (group.processStatus==='failed') { setStatus('failed','进程异常'); setText('st-result','未收到结束结果'); }
  else if (group.processStatus==='done') { setStatus('ended','进程已结束'); setText('st-result','未收到任务结果'); }
  else if (group.processStatus==='idle') { setStatus('ended','已停止'); setText('st-result','未完成'); }
  else { setStatus('running','运行中'); setText('st-result','—'); }
  byId('btn-prev').disabled=currentGroup<=0; byId('btn-next').disabled=currentGroup>=taskGroups.length-1;
}
function screenForEntry(group,entry) {
  if (!entry) return null;
  const index=group.entries.indexOf(entry);
  return group.entries.slice(0,index+1).reverse().find(item => item.screenshot_url || liveImageCache.has(item.imageCacheKey)) || null;
}
let screenIdentity='';
function renderScreen() {
  const group=taskGroups[currentGroup], entry=selectedEntry(), shot=group && screenForEntry(group,entry);
  const stage=byId('screen-stage');
  const source=shot?.screenshot_url || liveImageCache.get(shot?.imageCacheKey);
  const identity=source?(group.config.ts+':'+shot.key+':'+source+':'+shot.response+':'+shot.error):'';
  if (identity===screenIdentity && stage.children.length) return;
  screenIdentity=identity;
  byId('btn-zoom').disabled=!source;
  if (!source || !(source.startsWith('/live/shot/') || source.startsWith('data:image/'))) {
    stage.innerHTML='<div class="stage-empty"><div class="screen-icon"></div>'+(!taskGroups.length?'等待第一张屏幕截图':'此步骤尚无可用截图')+'<br><span>截图会随模型调用实时更新</span></div>'; setText('screen-meta','WAITING'); setText('screen-caption','模型观察到的屏幕'); return;
  }
  const wrap=document.createElement('div'); wrap.className='shot';
  const img=document.createElement('img'); img.alt='Step '+(shot.step ?? '—')+' 模型输入截图'; img.title='点击放大';
  img.onload=() => {
    if (screenIdentity!==identity || !img.isConnected) return;
    const width=img.naturalWidth,height=img.naturalHeight; img.onload=null;
    setText('screen-meta',width+' × '+height);
    if (shot.type==='grounding' && !shot.error) {
      const coord=parseCoord(shot.response);
      if (coord && coord.x>=0 && coord.y>=0 && coord.x<width && coord.y<height) annotateImg(img,coord);
    }
  };
  img.onerror=() => { if (screenIdentity!==identity || !img.isConnected) return; img.onerror=null; stage.innerHTML='<div class="stage-empty">这张实时截图已过期或读取失败。<br>可到历史轨迹查看已保存的屏幕。</div>'; setText('screen-meta','UNAVAILABLE'); byId('btn-zoom').disabled=true; };
  img.src=source; wrap.appendChild(img); stage.replaceChildren(wrap);
  setText('screen-caption','STEP '+(shot.step ?? '—')+' · '+(typeLabels[shot.type] || '模型观察')+(shot!==entry?' · 最近一次可用观察':''));
}
byId('btn-zoom').addEventListener('click',() => byId('screen-stage').querySelector('img')?.click());
function ioText(entry,tab) {
  if (tab==='logs') return processOutput || '等待进程输出…';
  if (!entry) return ['failed','done'].includes(runState.status)?'进程已退出，尚未产生模型调用。\n\n选择“进程日志”查看启动失败或退出的原因。':'选择左侧步骤，查看对应的模型输入、输出和执行结果。';
  if (tab==='event') return JSON.stringify(entry,(key,value) => ['key','imageCacheKey'].includes(key)?undefined:value,2);
  if (tab==='messages') return entry.messages?.length?JSON.stringify(entry.messages,null,2):entry.kind==='model'?'本次调用未记录完整 messages。可在“输入”查看已捕获的文本。':'执行状态事件没有模型 messages。';
  if (tab==='input') return entry.prompt || (entry.event==='task_start'?entry.instruction:'') || '该执行事件没有模型输入。';
  if (entry.kind==='model') return entry.pending?'模型请求已发送，等待响应…':(entry.raw_response?entry.raw_response+'\n\n解析结果：\n'+(entry.response || ''):entry.response) || (entry.error?'模型调用失败，未返回文本。':'模型返回空文本。');
  const details={};
  for (const key of ['action','status','error','result','success','reward','plan','verification','execution']) if (entry[key]!=null) details[key]=entry[key];
  return (eventLabels[entry.event] || entry.event || '状态事件')+(Object.keys(details).length?'\n\n'+JSON.stringify(details,null,2):'\n\n'+(entry.instruction || '事件已记录。'));
}
function renderInspector() {
  const entry=selectedEntry();
  if (ioTab==='logs') { setText('io-type','PROCESS'); setText('io-meta',runState.pid?'当前进程 · PID '+runState.pid:'进程输出'); setText('io-content',ioText(entry,ioTab)); setText('io-foot','最近 200 行 · 每 2 秒更新'); return; }
  setText('io-type',entry?.kind==='model'?(typeLabels[entry.type] || 'MODEL'):'EVENT');
  setText('io-meta',entry?(entry.model || (entry.kind==='model'?'模型调用':'执行状态'))+(entry.step!=null?' · STEP '+entry.step:''):'选择一条调用或执行事件');
  setText('io-content',ioText(entry,ioTab));
  const pieces=[];
  if (entry?.elapsed!=null) pieces.push('耗时 '+formatElapsed(entry.elapsed));
  if (entry?.usage) {
    const input=entry.usage.prompt_tokens ?? entry.usage.input_tokens,output=entry.usage.completion_tokens ?? entry.usage.output_tokens;
    if (input!=null) pieces.push('输入 '+input+' tokens'); if (output!=null) pieces.push('输出 '+output+' tokens');
  }
  if (ioTab==='messages') pieces.push('图片以引用和元数据展示，文本保留原始内容');
  if (entry?.pending) pieces.push('等待真实模型结果');
  setText('io-foot',pieces.join(' · ') || '实时事件通过 WebSocket 更新');
}
document.querySelectorAll('[data-io-tab]').forEach(button => button.addEventListener('click',() => {
  ioTab=button.dataset.ioTab;
    document.querySelectorAll('[data-io-tab]').forEach(item => { item.classList.toggle('active',item===button); item.setAttribute('aria-selected',String(item===button)); }); renderInspector(); if (ioTab==='logs') loadProcessOutput();
}));
async function loadProcessOutput() {
  try { const data=await api('/run/output'); processOutput=(data.output || []).join('\n'); setText('run-output',processOutput || '等待进程输出…'); if (ioTab==='logs' || !taskGroups.length) renderInspector(); }
  catch { /* Keep the last visible output while reconnecting. */ }
}
byId('btn-copy-io').addEventListener('click',async () => { try { await navigator.clipboard.writeText(byId('io-content').textContent); toast('已复制当前内容'); } catch { toast('复制失败，请选中文本后手动复制'); } });
function refreshWorkspace() {
  renderEventList(); renderGroupStatus(); renderScreen(); renderInspector();
  const group=taskGroups[currentGroup];
  byId('live-plan').innerHTML=group?.planCard?.outerHTML || '';
  byId('btn-export').disabled=!group?.entries?.length;
}
byId('btn-export').addEventListener('click',() => {
  const group=taskGroups[currentGroup]; if (!group) return;
  const data={instruction:group.instruction,config:group.config,plan:group.planPayload?.plan,events:group.entries.map(({key,imageCacheKey,...entry}) => entry)};
  const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob);
  const link=document.createElement('a'); link.href=url; link.download='windows-trajectory-'+new Date().toISOString().replace(/[:.]/g,'-')+'.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
  toast('已导出文本轨迹；截图保存在本机运行目录');
});

function consumeLog(payload) {
  const key=[payload.ts,payload.kind,payload.id,payload.pending,payload.type || payload.event,payload.step,payload.revision ?? payload.plan?.revision].join('|');
  if (seenKeys.has(key)) return;
  if (!taskGroups.length && payload.event!=='task_start') {
    orphanLogs.set(key,payload);
    if (orphanLogs.size>200) orphanLogs.delete(orphanLogs.keys().next().value);
    return;
  }
  seenKeys.add(key); if (seenKeys.size>4000) seenKeys.delete(seenKeys.values().next().value);
  addCard(payload);
  if (payload.event==='task_start' && orphanLogs.size) {
    const queued=[...orphanLogs.values()].sort((a,b) => (a.ts || 0)-(b.ts || 0)); orphanLogs.clear();
    queued.filter(event => !event.ts || !payload.ts || event.ts>=payload.ts).forEach(consumeLog);
  }
}
function ensureSnapshotGroup(start) {
  if (!start) return null;
  let group=taskGroups.find(item => item.config.ts===start.ts && item.example_id===start.example_id);
  if (!group) { consumeLog(start); group=taskGroups.find(item => item.config.ts===start.ts && item.example_id===start.example_id); }
  return group;
}
function recoverPlanSnapshot(snapshot) {
  const group=snapshot.task_start?ensureSnapshotGroup(snapshot.task_start):taskGroups.at(-1);
  const update=snapshot.plan_update || (snapshot.plan?{plan:snapshot.plan,current_plan_step_id:snapshot.current_plan_step_id,revision:snapshot.plan_revision}:null);
  if (group && update) {
    const revision=Number(update.revision ?? update.plan?.revision);
    if (Number.isFinite(revision) && group.planRevision!=null && revision<group.planRevision) return;
    updatePlanCard(group,update); group.planPayload=update; if (group===taskGroups[currentGroup]) refreshWorkspace();
  }
}
async function loadHistory() {
  try {
    const data=await api('/history');
    // A fresh start clears server logs. Keep a paused view of the earlier task until the user resumes following.
    for (const payload of data.logs || []) {
      if (data.task_start && payload.ts>=data.task_start.ts) ensureSnapshotGroup(data.task_start);
      consumeLog(payload);
    }
    recoverPlanSnapshot(data);
  } catch { /* The socket reconnect and periodic history refresh retry automatically. */ }
}
function connect() {
  socket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
  socket.onopen=() => { byId('conn').className='conn on'; setText('conn-text','实时已连接'); loadHistory(); };
  socket.onmessage=event => { try { consumeLog(JSON.parse(event.data)); } catch (error) { console.warn('Invalid agent event',error); } };
  socket.onclose=() => { byId('conn').className='conn off'; setText('conn-text','正在重连'); setTimeout(connect,2000); };
  socket.onerror=() => socket.close();
}

loadLocalRuns=async function() {
  detailOpen=false; byId('local-body').innerHTML='<div class="empty">正在读取本机历史轨迹…</div>';
  try { const data=await api('/local/runs'); historyRuns=data.runs || []; filterHistory(); }
  catch { byId('local-body').innerHTML='<div class="empty">历史轨迹读取失败。请检查服务连接后刷新。</div>'; }
};
function filterHistory() {
  if (detailOpen) return;
  const query=byId('history-search').value.trim().toLowerCase(), filter=byId('history-filter').value;
  const rows=historyRuns.filter(run => {
    const success=run.success===true || (run.success==null && run.score!=null && run.score>=1);
    const other=['running','interrupted'].includes(run.status) || (run.success==null && run.score==null);
    if (filter==='success' && (!success || other) || filter==='failed' && (success || other) || filter==='other' && !other) return false;
    return !query || [run.id,run.instruction].join(' ').toLowerCase().includes(query);
  });
  if (!rows.length) { byId('local-body').innerHTML='<div class="empty">'+(historyRuns.length?'没有匹配的历史任务。':'还没有本机运行记录。完成任务后，轨迹会自动保存在这里。')+'</div>'; return; }
  renderLocalList(rows);
}
const originalLoadLocalDetail=loadLocalDetail;
loadLocalDetail=function(id) { detailOpen=true; originalLoadLocalDetail(id); };
byId('history-search').addEventListener('input',() => { detailOpen=false; filterHistory(); });
byId('history-filter').addEventListener('change',() => { detailOpen=false; filterHistory(); });
byId('history-refresh').addEventListener('click',loadLocalRuns);
document.addEventListener('keydown',event => { if (event.key==='Escape') { byId('overlay').style.display='none'; byId('chat-new-modal').style.display='none'; } });
window.addEventListener('beforeunload',() => { clearTimeout(pollTimer); if (socket) { socket.onclose=null; socket.close(); } });

initConfiguration(); updateRunControls(); switchView('run'); refreshWorkspace(); loadHistory(); connect(); pollRunStatus();
setInterval(loadHistory,2500);
