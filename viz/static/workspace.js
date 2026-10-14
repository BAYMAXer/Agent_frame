'use strict';

const byId = id => document.getElementById(id);
const viewNames = {run:'任务配置',live:'实时运行',local:'历史轨迹',chat:'模型对话'};
let activeView = 'run';
let followLatest = true;
let ioTab = null;
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
const inspectorPanels = new Map();
let renderedEventScope = '';
let followedEventKey = '';
let forceFollowScroll = false;
let lastEventScrollTop = 0;
let expectedEventScrollTop = null;
const draftKey = 'agent-studio.windows.config.v1';
const draftFields = ['local-task','download-dir','local-maxsteps','execution-mode','browser-mode','ax-max-chars','screenshot-max-edge','run-model','run-model-url','run-ground','run-ground-url','run-ground-type','model-timeout','ground-timeout','step-timeout','planner-attempts'];
const typeLabels = {decision:'决策模型',grounding:'定位模型',format_retry:'格式重试',verify:'完成核对',reflect:'执行反思',code_agent:'子 Agent',planning:'任务规划',region_selection:'网页选区',subtask_verification:'子任务核验',uncertain_verification:'操作效果核验'};
const eventLabels = {task_start:'任务开始',step:'开始执行步骤',plan_update:'任务计划更新',browser_observation:'浏览器观察',browser_selection:'浏览器区域选择',executing:'正在执行动作',executed:'动作执行结果',task_end:'任务结束',error:'执行异常',interrupted:'已停止运行'};

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
  setText('summary-mode',byId('execution-mode').value==='browser_first'?'浏览器优先':'通用桌面');
  setText('summary-browser-mode',byId('browser-mode').value==='normal'?'普通浏览器':'无痕浏览器');
  setText('execution-mode-help',byId('execution-mode').value==='browser_first'?'使用独立浏览器，按网页结构定位和执行':'操作本机应用，使用真实鼠标与键盘');
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
  if (!['incognito','normal'].includes(byId('browser-mode').value)) byId('browser-mode').value='incognito';
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
  refreshPromptPreview();
}
function refreshPromptPreview() {
  const browserOnly=byId('execution-mode').value==='browser_first';
  api('/experiments/default-prompt?os_name=windows&browser_only='+browserOnly).then(data => {
    setText('preview-system-prompt',data.ok?data.prompt:'提示词读取失败：'+(data.error || '未知错误'));
  }).catch(() => setText('preview-system-prompt','提示词预览暂时不可用'));
}
draftFields.forEach(id => byId(id).addEventListener('input',() => { updateSummary(); saveDraft(); }));
byId('run-model').addEventListener('change',() => { applyPreset('decision'); updateSummary(); saveDraft(); });
byId('run-ground').addEventListener('change',() => { applyPreset('grounding'); updateSummary(); saveDraft(); });
byId('execution-mode').addEventListener('change',refreshPromptPreview);
document.querySelectorAll('[data-task]').forEach(button => button.addEventListener('click',() => {
  byId('local-task').value=button.dataset.task;
  byId('execution-mode').value=button.dataset.mode || 'browser_first';
  updateSummary(); refreshPromptPreview(); byId('local-task').focus(); saveDraft();
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
    download_dir:byId('download-dir').value.trim(),
    execution_mode:byId('execution-mode').value,
    browser_mode:byId('browser-mode').value,
    planner_attempts:integerField('planner-attempts','规划尝试次数',10),
    ax_max_chars:integerField('ax-max-chars','AX 字符预算',1000000),
    screenshot_max_edge:integerField('screenshot-max-edge','截图最长边',16384),
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
  finally { button.disabled=false; button.textContent='测试模型连接'; }
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
  forceFollowScroll=follow;
  const button=byId('btn-follow'); button.classList.toggle('paused',!follow); button.setAttribute('aria-pressed',String(follow)); button.textContent=follow?'● 跟随最新':'Ⅱ 跟随已暂停';
  if (follow && taskGroups.length) { switchToGroup(taskGroups.length-1); const group=taskGroups[currentGroup]; group.selectedKey=group.entries?.at(-1)?.key; refreshWorkspace(); }
}
byId('btn-follow').addEventListener('click',() => setFollow(!followLatest));
function actionText(value) { return typeof value==='string'?value:value==null?'':JSON.stringify(value); }
function entryTitle(entry) {
  if (entry.kind==='model') return entry.prompt || typeLabels[entry.type] || '模型调用';
  if (entry.event==='task_start') return entry.instruction || '任务开始';
  if (entry.event==='browser_observation') {
    const selection=entry.prompt_metadata?.browser_selection;
    return selection?'网页结构 '+(selection.original_count ?? selection.original ?? '—')+' → '+(selection.selected_count ?? selection.selected ?? '—')+' 节点'+(entry.plan_step_id!=null?' · 子任务 '+entry.plan_step_id:''):'浏览器观察已更新';
  }
  if (entry.action) return actionText(entry.action);
  if (entry.error) return actionText(entry.error);
  return eventLabels[entry.event] || entry.event || '状态更新';
}
function entryMatches(entry,filter,query) {
  const decision=entry.kind==='model' && ['decision','format_retry'].includes(entry.type);
  const error=Boolean(entry.error) || entry.event==='error' || ['rejected','uncertain'].includes(entry.status);
  if (filter==='decision' && !decision || filter==='grounding' && entry.type!=='grounding' || filter==='status' && entry.kind!=='status' || filter==='error' && !error) return false;
  if (['planning','region_selection'].includes(filter) && (entry.kind!=='model' || entry.type!==filter)) return false;
  if (filter==='verification' && (entry.kind!=='model' || !['verify','subtask_verification','uncertain_verification','final_verification'].includes(entry.type))) return false;
  return !query || [entryTitle(entry),entry.prompt,entry.response,entry.raw_response,entry.step,entry.event,entry.type].join(' ').toLowerCase().includes(query.toLowerCase());
}
function scrollEventList(top) {
  const list=byId('live-events'); list.scrollTop=top;
  lastEventScrollTop=list.scrollTop; expectedEventScrollTop=list.scrollTop;
}
function renderEventList() {
  const list=byId('live-events'), group=taskGroups[currentGroup], entries=group?.entries || [];
  const filter=byId('live-filter').value, query=byId('live-search').value.trim();
  const visible=entries.filter(entry => entryMatches(entry,filter,query));
  setText('event-count',visible.length+' / '+entries.length);
  const scope=JSON.stringify([group?.config.ts,currentGroup,filter,query]);
  const sameScope=scope===renderedEventScope;
  const scrollTop=sameScope?list.scrollTop:0;
  const bounds=list.getBoundingClientRect();
  const anchor=sameScope?[...list.children].find(node => node.dataset.key && node.getBoundingClientRect().bottom>bounds.top):null;
  const anchorOffset=anchor?anchor.getBoundingClientRect().top-bounds.top:0;
  const existing=sameScope?new Map([...list.children].filter(node => node.dataset.key).map(node => [node.dataset.key,node])):new Map();
  renderedEventScope=scope;
  if (!visible.length) {
    const emptyText=entries.length?'没有匹配的事件，试试其他筛选条件。':'任务启动后，执行步骤将在这里实时出现。';
    if (list.firstElementChild?.textContent!==emptyText || !list.firstElementChild?.classList.contains('empty')) { const empty=document.createElement('div'); empty.className='empty'; empty.textContent=emptyText; list.replaceChildren(empty); }
    return;
  }
  const visibleKeys=new Set(visible.map(entry => entry.key));
  [...list.children].forEach(node => { if (!sameScope || !visibleKeys.has(node.dataset.key)) node.remove(); });
  const nodes=[];
  visible.forEach(entry => {
    let button=existing.get(entry.key);
    if (!button) {
      button=document.createElement('button'); button.className='event-item'; button.dataset.key=entry.key;
      button.addEventListener('click',() => { setFollow(false); group.selectedKey=button.dataset.key; refreshWorkspace(); });
    }
    const error=Boolean(entry.error) || entry.event==='error' || ['rejected','uncertain'].includes(entry.status);
    const label=entry.kind==='model'?(typeLabels[entry.type] || '模型调用'):(eventLabels[entry.event] || entry.event);
    const time=entry.ts?new Date(entry.ts*1000).toLocaleTimeString('zh-CN',{hour12:false}):'';
    const title=String(entryTitle(entry));
    const displayTitle=title.length>180?title.slice(0,180)+'…':title;
    const html='<div class="event-top"><i class="event-dot '+(error?'error':entry.type==='grounding'?'grounding':entry.kind==='model'?'decision':'')+'"></i><span>'+esc(label)+'</span><span class="event-time">'+esc(time)+'</span></div><div class="event-title">'+esc(displayTitle)+'</div><div class="event-bottom"><span>'+(entry.step!=null?'STEP '+esc(entry.step):'TASK')+'</span><span'+(error?' class="error-text"':'')+'>'+(entry.pending?'等待模型响应':error?'异常 / 未确认':entry.elapsed!=null?esc(formatElapsed(entry.elapsed)):entry.status?esc(entry.status):'已记录')+'</span></div>';
    if (button._eventHtml!==html) { button.innerHTML=html; button._eventHtml=html; }
    button.classList.toggle('selected',entry.key===group.selectedKey); button.setAttribute('aria-pressed',String(entry.key===group.selectedKey));
    nodes.push(button);
  });
  nodes.forEach((node,index) => { if (list.children[index]!==node) list.insertBefore(node,list.children[index] || null); });
  const selectionKey=scope+'|'+group.selectedKey;
  if (followLatest && activeView==='live' && (forceFollowScroll || selectionKey!==followedEventKey)) {
    const selected=list.querySelector('.selected');
    if (selected) {
      const row=selected.getBoundingClientRect(),container=list.getBoundingClientRect();
      if (row.bottom>container.bottom) scrollEventList(list.scrollTop+row.bottom-container.bottom+8);
      else if (row.top<container.top) scrollEventList(list.scrollTop+row.top-container.top-8);
    }
    followedEventKey=selectionKey; forceFollowScroll=false;
  } else {
    const retainedAnchor=anchor?.isConnected?anchor:null;
    const delta=retainedAnchor?retainedAnchor.getBoundingClientRect().top-list.getBoundingClientRect().top-anchorOffset:0;
    scrollEventList(scrollTop+delta);
  }
}
const eventList=byId('live-events');
eventList.addEventListener('wheel',event => { if (event.deltaY<0 && followLatest) setFollow(false); },{passive:true});
eventList.addEventListener('keydown',event => { if (['ArrowUp','PageUp','Home'].includes(event.key) || event.key===' ' && event.shiftKey) setFollow(false); });
eventList.addEventListener('scroll',() => {
  const top=eventList.scrollTop;
  if (expectedEventScrollTop!=null && Math.abs(top-expectedEventScrollTop)<2) expectedEventScrollTop=null;
  else if (followLatest && top<lastEventScrollTop-2) setFollow(false);
  lastEventScrollTop=top;
},{passive:true});
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
function browserContextText(entry,planPayload) {
  const lines=[];
  const plan=entry?.plan_snapshot || planPayload?.plan;
  const steps=(Array.isArray(plan)?plan:plan?.steps || []).filter(step => step && typeof step==='object');
  const stepId=entry?.plan_step_id ?? planPayload?.current_plan_step_id ?? plan?.current_plan_step_id ?? plan?.current_step_id;
  const current=(stepId!=null?steps.find(step => String(step.id)===String(stepId)):null) || steps.find(step => ['in_progress','doing'].includes(step.status));
  if (current) lines.push('当前子任务 '+(current.id || current.idx || '')+'：'+(current.goal || ''));
  const selection=entry?.prompt_metadata?.browser_selection;
  if (selection) {
    lines.push('决策输入 · AX 节点 '+(selection.original_count ?? selection.original ?? selection.total ?? '—')+' → '+(selection.selected_count ?? selection.selected ?? '—')+' · '+(selection.serialized_chars ?? '—')+' 字符');
    if (selection.snapshot_id) lines.push('输入快照：'+selection.snapshot_id);
    if (selection.roi) lines.push('关注区域 ROI：'+JSON.stringify(selection.roi));
    if (selection.reason) lines.push('选择 / 恢复：'+selection.reason);
    if (selection.ax_quality) lines.push('AX 状态：'+selection.ax_quality+(selection.visual_required?' · 使用浏览器截图兜底':''));
    if (selection.region_cached) lines.push('选区：复用当前快照结果');
    if (selection.omitted_groups?.length) lines.push('省略的区域：'+selection.omitted_groups.join('、'));
    if (selection.selection_ms!=null || selection.region_ms!=null) lines.push('选区耗时：'+formatElapsed(Number(selection.selection_ms ?? selection.region_ms)/1000));
  }
  const capture=entry?.observation?.info?.capture || entry?.capture;
  const axCapture=entry?.observation?.info?.ax_capture;
  if (axCapture) lines.push('AX 采集：'+axCapture.attempts+' 次 · 有效内容节点 '+axCapture.meaningful_node_count+' · '+formatElapsed(Number(axCapture.readiness_ms || 0)/1000)+(axCapture.reason?' · '+axCapture.reason:''));
  if (capture) {
    const size=value => Array.isArray(value)?value.join(' × '):typeof value==='object' && value?value.width+' × '+value.height:String(value ?? '—');
    const original=capture.original_size || capture.source_size || capture.viewport_css_size || capture.viewport;
    const input=capture.input_size || capture.screenshot_size;
    if (original || input) lines.push('操作后观察 · 浏览器截图 '+size(original)+' → 输入 '+size(input));
    if (capture.viewport_css_size) lines.push('CSS 视口：'+size(capture.viewport_css_size));
    if (capture.snapshot_id) lines.push('观察快照：'+capture.snapshot_id);
    if (capture.timing_ms!=null) lines.push('网页捕获：'+formatElapsed(Number(capture.timing_ms)/1000));
  }
  const execution=entry?.execution_info || entry?.execution || {};
  if (execution.actionability) lines.push('点击位置：'+execution.actionability.point_source+' · 检查 '+execution.actionability.candidates_checked+' 个候选点');
  if (execution.occlusion) {
    const blocker=execution.occlusion.blocker || {};
    lines.push('遮挡者：'+[blocker.role,blocker.tag,blocker.name].filter(Boolean).join(' · '));
    if (execution.occlusion.suggestion) lines.push('恢复建议：'+execution.occlusion.suggestion);
  }
  if (execution.recovery) lines.push('程序恢复：'+execution.recovery.kind+' · '+(execution.recovery.succeeded?'已解决':'未解决'));
  if (entry?.phase) lines.push('阶段：'+entry.phase);
  if (entry?.previous_phase_elapsed_ms!=null) lines.push('上一阶段 '+(entry.previous_phase || '')+'：'+formatElapsed(Number(entry.previous_phase_elapsed_ms)/1000));
  if (entry?.timing_ms) lines.push('阶段耗时：'+Object.entries(entry.timing_ms).map(([name,ms]) => name+' '+formatElapsed(Number(ms)/1000)).join(' · '));
  return lines.join('\n');
}
function renderBrowserContext() {
  const group=taskGroups[currentGroup],entry=selectedEntry();
  if (!group) { setText('browser-context-meta','等待观察'); writeInspectorPanel('browser-context','等待浏览器子任务与观察数据','empty'); return; }
  const candidates=(group.entries || []).filter(item => entry?.step==null || String(item.step)===String(entry.step));
  const evidence=[...candidates].reverse().find(item => item.prompt_metadata?.browser_selection);
  const timing=[...candidates].reverse().find(item => item.timing_ms);
  const observation=[...candidates].reverse().find(item => item.observation?.info?.capture || item.capture);
  const execution=[...candidates].reverse().find(item => item.execution_info);
  const details={...evidence,...entry,
    prompt_metadata:entry?.prompt_metadata?.browser_selection?entry.prompt_metadata:evidence?.prompt_metadata,
    observation:entry?.observation?.info?.capture?entry.observation:observation?.observation,
    capture:entry?.capture || observation?.capture,
    execution_info:entry?.execution_info || execution?.execution_info,
    timing_ms:entry?.timing_ms || timing?.timing_ms};
  const selection=details.prompt_metadata?.browser_selection;
  setText('browser-context-meta',selection?(selection.selected_count ?? selection.selected ?? '—')+' AX 节点':details.phase || '暂无选区');
  writeInspectorPanel('browser-context',browserContextText(details,group.planPayload) || '此步骤尚无浏览器区域选择数据',group.config.ts+':'+entry?.key);
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
    stage.innerHTML='<div class="stage-empty"><div class="screen-icon"></div>'+(!taskGroups.length?'等待屏幕观察':'此步骤使用网页结构，或尚未捕获截图')+'<br><span>浏览器决策可使用 AX 文本，截图随视觉调用更新</span></div>'; setText('screen-meta','WAITING'); setText('screen-caption','最近一次模型截图'); return;
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
  if (!entry) {
    if (['failed','done'].includes(runState.status)) return '进程已退出，尚未产生模型调用。\n\n选择“进程日志”查看启动失败或退出的原因。';
    return tab==='input'?'选择左侧调用，查看发送给模型的输入。':tab==='output'?'模型返回的结果会与输入同时显示在这里。':'选择左侧调用，查看对应的完整消息与事件详情。';
  }
  if (tab==='event') return JSON.stringify(entry,(key,value) => ['key','imageCacheKey'].includes(key)?undefined:value,2);
  if (tab==='messages') return entry.messages?.length?JSON.stringify(entry.messages,null,2):entry.kind==='model'?'本次调用未记录完整 messages。可在“输入”查看已捕获的文本。':'执行状态事件没有模型 messages。';
  if (tab==='input') {
    if (entry.prompt) return actionText(entry.prompt);
    if (entry.kind==='model' && entry.messages?.length) {
      const user=[...entry.messages].reverse().find(message => message.role==='user');
      if (typeof user?.content==='string') return user.content;
      if (Array.isArray(user?.content)) return user.content.filter(part => part.type==='text').map(part => part.text || '').join('\n\n') || '当前调用使用图片输入，可在完整消息中查看图片引用。';
    }
    return (entry.event==='task_start'?entry.instruction:'') || '该执行事件没有模型输入。';
  }
  if (entry.kind==='model') return entry.pending?'模型请求已发送，等待响应…':(entry.raw_response?actionText(entry.raw_response)+'\n\n解析结果：\n'+actionText(entry.response):actionText(entry.response)) || (entry.error?'模型调用失败，未返回文本。':'模型返回空文本。');
  const details={};
  for (const key of ['action','status','error','result','success','reward','plan','verification','execution','execution_info','prompt_metadata','observation','plan_step_id','timing_ms','phase']) if (entry[key]!=null) details[key]=entry[key];
  return (eventLabels[entry.event] || entry.event || '状态事件')+(Object.keys(details).length?'\n\n'+JSON.stringify(details,null,2):'\n\n'+(entry.instruction || '事件已记录。'));
}
function writeInspectorPanel(id,text,scope) {
  const panel=byId(id), previous=inspectorPanels.get(id);
  const sameScope=previous?.scope===scope;
  if (sameScope && panel.textContent===String(text)) return;
  const top=sameScope?panel.scrollTop:0, left=sameScope?panel.scrollLeft:0;
  if (panel.textContent!==String(text)) panel.textContent=text;
  panel.scrollTop=top; panel.scrollLeft=left;
  inspectorPanels.set(id,{scope});
}
function positionInspectorDetails() {
  if (ioTab==null || activeView!=='live') return;
  const anchor=document.querySelector('.monitor-center').getBoundingClientRect();
  const width=Math.min(anchor.width || 540,window.innerWidth-24);
  const left=Math.max(12,Math.min(anchor.left,window.innerWidth-width-12));
  const top=Math.max(12,Math.min(anchor.top,window.innerHeight-210));
  const height=Math.max(150,Math.min(Math.max(anchor.height,200),window.innerHeight-top-12));
  Object.assign(byId('io-details').style,{left:left+'px',top:top+'px',width:width+'px',height:height+'px'});
}
function closeInspectorDetails(returnFocus=false) {
  const previousTab=ioTab; ioTab=null;
  document.querySelectorAll('[data-io-tab]').forEach(item => { item.classList.remove('active'); item.setAttribute('aria-expanded','false'); });
  renderInspector();
  if (returnFocus && previousTab) document.querySelector('[data-io-tab="'+previousTab+'"]').focus({preventScroll:true});
}
function renderInspector() {
  const entry=selectedEntry();
  const scope=taskGroups[currentGroup]?.config.ts+':'+entry?.key;
  setText('io-type',entry?.kind==='model'?(typeLabels[entry.type] || 'MODEL'):'EVENT');
  setText('io-meta',entry?(entry.model || (entry.kind==='model'?'模型调用':'执行状态'))+(entry.step!=null?' · STEP '+entry.step:''):'选择一条调用或执行事件');
  writeInspectorPanel('io-input',ioText(entry,'input'),scope);
  writeInspectorPanel('io-output',ioText(entry,'output'),scope);
  byId('io-details').hidden=ioTab==null;
  if (ioTab!=null) {
    setText('io-detail-title',{messages:'完整消息 · 图片以引用与元数据展示',event:'原始事件 JSON',logs:'进程日志 · 最近 200 行'}[ioTab]);
    writeInspectorPanel('io-content',ioText(entry,ioTab),ioTab==='logs'?'process':scope+':'+ioTab);
    positionInspectorDetails();
  }
  const pieces=[];
  if (entry?.elapsed!=null) pieces.push('耗时 '+formatElapsed(entry.elapsed));
  if (entry?.phase) pieces.push('阶段 '+entry.phase);
  if (entry?.timing_ms) pieces.push(...Object.entries(entry.timing_ms).map(([name,ms]) => name+' '+formatElapsed(Number(ms)/1000)));
  if (entry?.usage) {
    const input=entry.usage.prompt_tokens ?? entry.usage.input_tokens,output=entry.usage.completion_tokens ?? entry.usage.output_tokens;
    if (input!=null) pieces.push('输入 '+input+' tokens'); if (output!=null) pieces.push('输出 '+output+' tokens');
  }
  if (entry?.pending) pieces.push('等待真实模型结果');
  setText('io-foot',pieces.join(' · ') || '实时更新 · 向上滚动轨迹会暂停跟随');
}
document.querySelectorAll('[data-io-tab]').forEach(button => button.addEventListener('click',() => {
  ioTab=ioTab===button.dataset.ioTab?null:button.dataset.ioTab;
  document.querySelectorAll('[data-io-tab]').forEach(item => { const active=item.dataset.ioTab===ioTab; item.classList.toggle('active',active); item.setAttribute('aria-expanded',String(active)); });
  renderInspector(); if (ioTab==='logs') loadProcessOutput();
}));
byId('btn-close-io').addEventListener('click',() => closeInspectorDetails(true));
window.addEventListener('resize',positionInspectorDetails);
document.querySelectorAll('[data-copy-io]').forEach(button => button.addEventListener('click',async () => {
  try { await navigator.clipboard.writeText(byId('io-'+button.dataset.copyIo).textContent); toast('已复制模型'+(button.dataset.copyIo==='input'?'输入':'输出')); }
  catch { toast('复制失败，请选中文本后手动复制'); }
}));
['io-input','io-output','io-content'].forEach(id => {
  byId(id).addEventListener('wheel',event => { if (event.deltaY<0 && followLatest) setFollow(false); },{passive:true});
  byId(id).addEventListener('keydown',event => { if (['ArrowUp','PageUp','Home'].includes(event.key)) setFollow(false); });
});
async function loadProcessOutput() {
  try { const data=await api('/run/output'); processOutput=(data.output || []).join('\n'); setText('run-output',processOutput || '等待进程输出…'); if (ioTab==='logs' || !taskGroups.length) renderInspector(); }
  catch { /* Keep the last visible output while reconnecting. */ }
}
byId('btn-copy-io').addEventListener('click',async () => { try { await navigator.clipboard.writeText(byId('io-content').textContent); toast('已复制当前内容'); } catch { toast('复制失败，请选中文本后手动复制'); } });
function refreshWorkspace() {
  renderEventList(); renderGroupStatus(); renderScreen(); renderInspector(); renderBrowserContext();
  const group=taskGroups[currentGroup];
  const host=byId('live-plan'), html=group?.planCard?.outerHTML || '';
  if (host._planHtml!==html) { const top=host.scrollTop; host.innerHTML=html; host._planHtml=html; host.scrollTop=top; }
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
  if (group && snapshot.browser_observation) consumeLog(snapshot.browser_observation);
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
document.addEventListener('keydown',event => { if (event.key==='Escape') { byId('overlay').style.display='none'; byId('chat-new-modal').style.display='none'; closeInspectorDetails(true); } });
window.addEventListener('beforeunload',() => { clearTimeout(pollTimer); if (socket) { socket.onclose=null; socket.close(); } });

initConfiguration(); updateRunControls(); switchView('run'); refreshWorkspace(); loadHistory(); connect(); pollRunStatus();
setInterval(loadHistory,2500);
