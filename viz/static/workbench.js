/* One renderer for live and archived evidence. No mock data. */
(() => {
'use strict';
const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const json = v => JSON.stringify(v ?? null, (k,x) => typeof x==='string' && (x.startsWith('data:image/') || x.length>10000 && /^[A-Za-z0-9+/=]+$/.test(x)) ? '[图片：见下方预览]' : x, 2);
const label = {cli:'CLI',dom:'DOM',gui:'GUI',yes:'是',no:'否',waiting:'继续等待',passed:'通过',failed:'未通过',unknown:'证据不足',pending:'进行中',progress:'已有进展，子任务未完成',running:'核查中',executed:'已派发',rejected:'已拦截',done:'完成',doing:'进行中',streaming:'正在生成',complete:'完成',not_passed:'未通过',unavailable:'不可用'};
const name = s => label[s] || s || '待确认';
const endings={report_review_failed:'文字结果核查未通过',stalled:'未检测到进展',max_steps:'决策步数耗尽',planning_failed:'规划失败',task_timeout:'任务超时',completion_inconclusive:'完成证据不足',verification_changed:'最终复查未通过',agent_failed:'执行失败'};
const phases={planning:'规划中',decision:'决策中',dispatch:'系统执行中',settlement:'等待环境就绪',step_review:'步骤核查中',final_review:'整项验收中',final_evaluation:'最终复查中',mode_selection:'选择操作模式',initial_observation:'获取初始观察',preprocess:'准备观察'};
const purpose = {report_review:'文字结果与来源核查',report_review_fallback:'独立文字语义复核',mode_selection:'操作模式选择',action_transition:'动作后联合核查',step_review:'子任务完成核查',final_review:'整项任务验收',page_readiness:'页面加载就绪',download_wait:'下载等待判断',paper_identity:'论文身份核查',user_report:'结果撰写',scene_routing:'操作模式选择',task_routing:'任务环境分类',tab_selection:'任务标签页选择',requirement_review:'要求核查'};
const ms = n => n == null ? '—' : n < 1000 ? Math.round(n)+' ms' : (n/1000).toFixed(1)+' s';
const empty = () => ({instruction:'等待任务启动',actions:[],revisions:[],outcome:{},metrics:{},report:{},unassigned_calls:[]});
function downloadFiles(outcome) {
 const v=outcome?.verification||{},facts=(v.completion||v).download_facts||[];
 const files=new Map();
 for(const f of facts)if(f&&f.completed===true&&f.transport==='browser'&&typeof f.path==='string')files.set(f.artifact_id||f.path,{...f,id:f.artifact_id||f.path});
 return [...files.values()];
}
function safeUrl(s) { try {const u=new URL(s,location.origin);return ['http:','https:'].includes(u.protocol)?u.href:'';}catch{return '';}}
function imageSrc(value,run) {
 if(typeof value!=='string')return '';
 if(/^data:image\/(png|jpeg|jpg|webp);base64,/.test(value))return value;
 if(/^assets\/[a-f0-9]{64}\.(png|jpg|webp)$/.test(value)&&run)return '/local/jev-image?run='+encodeURIComponent(run)+'&filename='+encodeURIComponent(value);
 if(value.length>1000&&/^[a-zA-Z0-9+/=\s]+$/.test(value))return 'data:image/png;base64,'+value;
 return '';
}
function mediaIn(value,run,out=[]) {
 if(typeof value==='string'){const src=imageSrc(value,run);if(src&&!out.includes(src))out.push(src);}
 else if(Array.isArray(value))value.forEach(x=>mediaIn(x,run,out));
 else if(value&&typeof value==='object')Object.values(value).forEach(x=>mediaIn(x,run,out));
 return out;
}
const raw = (title,value,run) => `<details class="wb-detail"><summary>${esc(title)}</summary><div class="wb-detail-body"><pre>${esc(typeof value==='string'?value:json(value))}</pre>${mediaIn(value,run).map(src=>`<img class="wb-input-image" alt="实际送入模型的图片" src="${esc(src)}">`).join('')}</div></details>`;
function normalizeAction(a) { if(typeof a==='string'){try{a=JSON.parse(a)}catch{return {raw:a}}}if(a?.type&&!a.action){const {type,args,...rest}=a;return {action:type,args:args||rest};}return a||{}; }
function actionText(a){a=normalizeAction(a);return a.action?`${a.action}(${JSON.stringify(a.args||{})})`:a.raw||'等待决策输出';}
function executionMode(a){
 const action=normalizeAction(a?.action),e=a?.execution_info||{},route=a?.routing||{};
 // browser_dom is historically also the CDP transport for coordinate input.
 // The actual resolution evidence takes precedence over that transport name.
 if(action.action==='cli'||e.channel==='cli')return 'CLI';
 if(e.resolved==='visual'||(a?.grounding?.coord&&!e.target_ref&&!route.target_ref))return 'GUI';
 const channel=e.channel||route.channel||a?.operation_mode||'';
 return /dom/i.test(channel)?'DOM':/gui|desktop|viewport/i.test(channel)?'GUI':({blocked:'已拦截',framework:'框架控制',observe:'观察'}[channel]||channel||'待路由');
}
function actionLabel(value){const a=normalizeAction(value),x=a.args||{};const labels={click:'点击',double_click:'双击',right_click:'右键',type:'输入',goto:'打开',press:'按键',hotkey:'快捷键',scroll:'滚动',cli:'后台操作',observe_browser:'观察网页',observe_desktop:'观察桌面',request_finish:'请求验收'};let target=x.target||x.url||x.key||x.direction||'';if(a.action==='cli')target=[x.operation,x.command||x.path,x.destination?'→ '+x.destination:''].filter(Boolean).join(' ');if(a.action==='type')target=x.text||target;if(a.action==='hotkey')target=(x.keys||[]).join('+');return (labels[a.action]||a.action||'决策')+(target?' '+target:'');}
function callTitle(c){const p=c.purpose||c.actor_role;return ({knowledge_selection:'知识相关性筛选',knowledge_selector:'知识相关性筛选'})[p] || purpose[p] || (p==='planner'?'任务规划':p==='decision'?'动作决策':p==='reviewer'?'独立核查':p||'模型调用');}
function isJev(c){return c.endpoint_type==='systemone'||String(c.model||'').includes('d3')||c.request?.questions||c.payload?.questions||c.response?.answers||c.answers;}
function callView(c,run){const request=c.request||c.payload||c.messages||{};let response=c.response||c.result||c.answers||{};if(typeof response==='string'){try{response=JSON.parse(response)}catch{}}
 const answers=response.answers||c.answers||{};
 return `<details class="wb-detail wb-call"><summary><span>${isJev(c)?'Jev':'模型'} · ${esc(callTitle(c))}</span><small>${ms(c.wall_time_ms??c.latency_ms)}${Object.keys(answers).length?' · '+Object.keys(answers).length+' 个问题':''}</small></summary><div class="wb-detail-body">${Object.entries(answers).map(([k,v])=>`<div class="wb-answer"><b>${esc(({settled:'加载是否就绪',correct:'动作是否正确',next_mode:'下一操作模式',replan:'是否需要重规划',step:'要求是否满足',effect:'动作效果',surface:'操作表面',readiness:'页面加载就绪',task_type:'任务环境',tab:'目标标签页',next:'等待后建议'})[k]||(k.startsWith('coverage_')?'交付要求 '+k.split('_')[1]:k))}</b><span>${esc(name(v.label??v.choice??v.noul??v.score))}</span><small>${esc(json(v.probabilities||{}))}</small></div>`).join('')}${raw('实际输入',request,run)}${raw('实际输出',response,run)}${raw('调用元数据（含用量）',Object.fromEntries(Object.entries(c).filter(([k])=>!['messages','request','payload','response'].includes(k))),run)}</div></details>`;
}
let reportParser;
function reportMarkup(text,sources=[]){
 if(!window.markdownit)return '<pre>'+esc(text||'')+'</pre>';
 if(!reportParser){
  reportParser=window.markdownit({html:false,linkify:true,breaks:false});
  reportParser.validateLink=url=>/^(https?:|mailto:|#)/i.test(url);
  reportParser.renderer.rules.link_open=(tokens,i,options,env,self)=>{
   tokens[i].attrSet('target','_blank');tokens[i].attrSet('rel','noopener noreferrer');
   return self.renderToken(tokens,i,options);
  };
  reportParser.renderer.rules.image=(tokens,i)=>esc(tokens[i].content);
  reportParser.renderer.rules.text=(tokens,i,options,env)=>esc(tokens[i].content).replace(/\[(S\d+)\]/g,(m,id)=>{
   const source=env.sources.find(s=>s.id===id),url=source&&safeUrl(source.url);
   return url?`<a class="wb-citation" href="${esc(url)}" target="_blank" rel="noopener noreferrer">[${id}]</a>`:m;
  });
  reportParser.renderer.rules.table_open=()=>'<div class="wb-table-wrap"><table>';
  reportParser.renderer.rules.table_close=()=>'</table></div>';
 }
 return reportParser.render(String(text||''),{sources});
}
function earlierActionGroups(data,steps,revision){
 const groups=new Map();
 for(const action of data.actions.filter(x=>!steps.some(s=>s.id===x.plan_step_id))){
  const owner=data.revisions.find(r=>r.revision===action.plan_revision)||data.revisions.find(r=>(r.queue?.steps||r.plan?.steps||[]).some(s=>s.id===action.plan_step_id));
  const key=owner?String(owner.revision):'unlinked';
  if(!groups.has(key))groups.set(key,{revision:owner?.revision,actions:[]});
  groups.get(key).actions.push(action);
 }
 return [...groups.values()];
}

class Workbench {
 constructor(root,history=false){root._workbench?.listeners?.abort();root._workbench?.closeNote(false,true);root._workbench=this;this.listeners=new AbortController();this.root=root;this.history=history;this.data=empty();this.selected=null;this.follow=!history;this.tab='checks';this.frame='after';this.planVersion=null;this.seen=new Set();this.run='';this.draw();root.addEventListener('click',e=>this.click(e),{signal:this.listeners.signal});root.addEventListener('change',e=>{if(e.target.matches('[data-plan]')){this.planVersion=e.target.value;this.draw();this.animatePanel('.wb-plan');}if(e.target.matches('[data-action-select]'))this.select(Number(e.target.value));},{signal:this.listeners.signal});root.addEventListener('toggle',e=>{if(e.target.matches('details')){const key=e.target.dataset.key;if(key)this.openState?.set(key,e.target.open);}},{capture:true,signal:this.listeners.signal});}
 mount(data,run){this.fileMessage='';this.fileBusy=null;this.data=data;this.run=run;this.selected=data.actions.at(-1)?.step??null;this.follow=false;this.draw();}
 select(step){this.selected=step;this.follow=false;const a=this.data.actions.find(x=>x.step===step);if(a?.plan_revision!=null)this.planVersion=String(a.plan_revision);if(this.frame==='live')this.frame='after';this.draw();this.animatePanel('.wb-main');}
 animatePanel(selector){
  if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;
  this.root.querySelector(selector)?.animate([{opacity:.35,transform:'translateY(5px)'},{opacity:1,transform:'translateY(0)'}],{duration:320,easing:'cubic-bezier(.2,.8,.2,1)'});
 }
 closeNote(restore=false,immediate=false){
  const note=this.note;if(!note)return;this.note=null;this.noteEvents?.abort();
  this.noteAnchor?.setAttribute('aria-expanded','false');
  if(restore&&this.noteAnchor?.isConnected)this.noteAnchor.focus({preventScroll:true});
  if(immediate||matchMedia('(prefers-reduced-motion: reduce)').matches){note.remove();return;}
  note.getAnimations().forEach(a=>a.cancel());
  note.animate([{opacity:1,transform:'scale(1)'},{opacity:0,transform:'scale(.94) translateY(-5px)'}],{duration:140,easing:'ease-in',fill:'forwards'}).finished.then(()=>note.remove());
 }
 showNote(anchor){
  if(this.note){this.closeNote(true);return;}
  const rev=this.data.revisions.find(r=>String(r.revision)===anchor.dataset.revision);if(!rev)return;
  const task=(rev.queue?.steps||rev.plan?.steps||[]).find(s=>s.id===anchor.dataset.subtask);
  if(!task&&!rev.reason)return;
  const title=task?'子任务详情':'重规划说明';
  const related=task?this.data.actions.flatMap(a=>(a.step_reviews||[]).filter(r=>r.plan_step_id===task.id)):[];
  const detail=task?{目标:task.goal,状态:name(task.status),说明:related.length?'以下为已记录的核查依据':this.data.outcome?.success?'整项任务验收已通过；此子任务没有单独的动作或核查记录，不能补造执行记录。':'尚无关联动作，可查看计划中的预期结果。',预期:task.expected_evidence,核查条件:task.verification,核查记录:related.length?related:task.last_review||null}:rev.reason;
  const note=document.createElement('div');note.className='wb-replan-note';note.setAttribute('role','dialog');note.setAttribute('aria-label',title);note.id='wb-replan-note';
  note.innerHTML=`<header><span>${title} · 计划 ${Number(rev.revision)+1}</span><button aria-label="关闭说明">×</button></header><div class="wb-note-body">${esc(typeof detail==='string'?detail:json(detail))}</div>`;
  document.body.append(note);this.note=note;this.noteAnchor=anchor;anchor.setAttribute('aria-expanded','true');anchor.setAttribute('aria-controls',note.id);
  const rect=anchor.getBoundingClientRect(),width=Math.min(350,innerWidth-24);note.style.width=width+'px';
  const left=Math.max(12,Math.min(rect.left-12,innerWidth-width-12));note.style.left=left+'px';
  const top=Math.max(12,Math.min(rect.bottom+9,innerHeight-note.offsetHeight-12));note.style.top=top+'px';note.style.transformOrigin=`${rect.left+rect.width/2-left}px ${top>=rect.bottom?'0':'100%'} `;
  if(!matchMedia('(prefers-reduced-motion: reduce)').matches)note.animate([{opacity:0,transform:'scale(.9) translateY(-6px)'},{opacity:1,transform:'scale(1) translateY(0)'}],{duration:210,easing:'cubic-bezier(.2,.8,.2,1)'});
  note.querySelector('button').onclick=()=>this.closeNote(true);note.querySelector('button').focus({preventScroll:true});
  this.noteEvents=new AbortController();const opts={signal:this.noteEvents.signal};
  document.addEventListener('pointerdown',e=>{if(!note.contains(e.target)&&!anchor.contains(e.target))this.closeNote();},{...opts,capture:true});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();this.closeNote(true);}},{...opts,capture:true});
  document.addEventListener('scroll',e=>{if(!note.contains(e.target))this.closeNote();},{...opts,capture:true});
  window.addEventListener('resize',()=>this.closeNote(),opts);
 }
 click(e){
  const summary=e.target.closest('summary');
  if(summary&&this.root.contains(summary)&&!e.target.closest('button,a')){
   const detail=summary.parentElement;
   if(detail.matches('details')&&!window.matchMedia('(prefers-reduced-motion: reduce)').matches){
    e.preventDefault();if(detail._animation)return;
    const close=detail.open,start=detail.getBoundingClientRect().height;if(!close)detail.open=true;
    const end=close?summary.getBoundingClientRect().height:detail.getBoundingClientRect().height;
    detail.style.overflow='hidden';detail._animation=detail.animate({height:[start+'px',end+'px'],opacity:[.92,1]}, {duration:180,easing:'ease-out'});
    detail._animation.onfinish=()=>{detail.open=!close;detail.style.overflow='';detail._animation=null;};return;
   }
  }
  const b=e.target.closest('[data-wb]');if(!b)return;const cmd=b.dataset.wb;
  if(cmd==='note'||cmd==='subtask'){this.showNote(b);return;}
  if(cmd==='select')this.select(Number(b.dataset.step));
  else if(cmd==='prev'||cmd==='next'){const i=this.data.actions.findIndex(a=>a.step===this.selected);const a=this.data.actions[i+(cmd==='prev'?-1:1)];if(a)this.select(a.step);}
  else if(cmd==='live'){this.follow=true;this.planVersion=null;this.selected=this.data.actions.at(-1)?.step??null;this.frame='live';this.draw();}
  else if(cmd==='frame'){this.frame=b.dataset.frame;this.draw();this.animatePanel('.wb-shot');}
  else if(cmd==='tab'){this.tab=b.dataset.tab;this.draw();this.animatePanel('.wb-evidence');}
  else if(cmd==='report'){this.tab='report';this.draw();this.animatePanel('.wb-evidence');const panel=this.root.querySelector('.wb-evidence');if(panel)panel.scrollTop=0;}
  else if(cmd==='stop'){fetch('/run/stop',{method:'POST'}).then(r=>r.json()).then(r=>{if(r.ok){this.data.outcome={success:false,termination_reason:'用户中止'};this.follow=false;this.draw();}});}
  else if(cmd==='result-file'){this.openResultFile(b);}
  else if(cmd==='save-report'){const blob=new Blob([this.data.report?.text||this.data.outcome.final_answer||''],{type:'text/markdown;charset=utf-8'});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download='任务结果.md';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  else if(cmd==='zoom'){const img=this.root.querySelector('.wb-shot img');if(!img)return;const dialog=document.createElement('dialog');dialog.className='wb-zoom';dialog.innerHTML=`<button autofocus>关闭 ×</button><img alt="原始比例截图" src="${esc(img.src)}">`;const close=()=>{if(dialog.dataset.closing)return;dialog.dataset.closing='true';if(matchMedia('(prefers-reduced-motion: reduce)').matches){dialog.close();return;}dialog.animate([{opacity:1,transform:'scale(1)'},{opacity:0,transform:'scale(.97)'}],{duration:140,fill:'forwards'}).finished.then(()=>dialog.close());};dialog.querySelector('button').onclick=close;dialog.addEventListener('cancel',e=>{e.preventDefault();close();});dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);dialog.showModal();if(!matchMedia('(prefers-reduced-motion: reduce)').matches)dialog.animate([{opacity:0,transform:'scale(.96)'},{opacity:1,transform:'scale(1)'}],{duration:180,easing:'ease-out'});}
 }
 update(fn){fn();if(this.follow)this.selected=this.data.actions.at(-1)?.step??null;if(!this.pending){this.pending=true;requestAnimationFrame(()=>{this.pending=false;this.draw();});}}
 draw(){
  this.closeNote(false,true);
  const animated=['.wb-heading h2','.wb-meta b','.wb-current','.wb-stages','.wb-plan','.wb-action-line','.wb-evidence'];
  const previousText=new Map(animated.map(k=>[k,this.root.querySelector(k)?.textContent]));
  const rootScroll=this.root.scrollTop,planScroll=this.root.querySelector('.wb-plan')?.scrollTop||0;
  const oldScroll=this.root.querySelector('.wb-evidence')?.scrollTop||0;
  this.openState=new Map([...this.root.querySelectorAll('details[data-key]')].map(x=>[x.dataset.key,x.open]));
  const d=this.data,a=d.actions.find(x=>x.step===this.selected),idx=d.actions.indexOf(a),m=d.metrics||{},finished=Object.hasOwn(d.outcome||{},'success');
  const rev=this.planVersion===null?d.revisions.at(-1):d.revisions.find(r=>String(r.revision)===this.planVersion)||d.revisions.at(-1);
  const pastQueue=!this.follow&&a?.execution_queue&&rev&&a.execution_queue.revision===rev.revision?a.execution_queue:null;
  const queue=pastQueue||rev?.queue,steps=queue?.steps||rev?.plan?.steps||[];
  const status=finished?(d.outcome.success?'已完成':'未完成 · '+(endings[d.outcome.termination_reason]||d.outcome.termination_reason||'')):(this.history&&d.incomplete?'运行记录尚未结束':d.phase||'运行中');
  const action=normalizeAction(a?.action),route=a?.routing||{};
  const mode=executionMode(a);
  const obs=this.frame==='before'?a?.observation:a?.next_observation;
  const surface=(a?.next_observation||a?.observation)?.info?.active_surface;
  const live=this.frame==='live'&&this.follow&&!finished&&surface!=='desktop'&&d.liveFrame;
  const shot=live?imageSrc(d.liveFrame.screenshot):imageSrc(obs?.screenshot,this.run);
  const url=live?d.liveFrame.url:obs?.info?.url||obs?.context?.find(x=>x.kind==='browser_ax')?.url||'';
  const reviews=a?.step_reviews||[],calls=a?.calls?.length?a.calls:a?.decision_calls||[];
  const e=a?.execution_info||{};
  const finalReview=[...reviews].reverse().find(r=>['whole_task','final'].includes(r.plan_step_id));
  const currentReview=finalReview||a?.step_verification||reviews.at(-1);
  const reviewStatus=currentReview?.status;
  const stage=[[mode==='CLI'?'环境信息':'观察',a?.observation?'done':'idle'],
   ['决策',a?.valid_action===false?'blocked':a?.policy_output?'done':'active'],
   [mode==='CLI'?'后台路由':'定位 / 路由',route.channel==='reobserve'?'blocked':route.channel||a?.grounding||e.channel?'done':'idle'],
   [action.action==='request_finish'?'提交验收':'系统执行',action.action==='request_finish'&&currentReview?'done':['rejected','failed','uncertain'].includes(e.status)?'blocked':['pending','in_progress'].includes(e.status)?'active':e.status==='executed'?'done':'idle'],
   [(finalReview?'整项验收':'独立核查')+' · '+(reviewStatus?name(reviewStatus):'待核查'),reviewStatus==='passed'?'done':reviewStatus==='running'?'active':['failed','unknown'].includes(reviewStatus)?'blocked':'idle']];

  this.root.innerHTML=`<section class="wb"><header class="wb-heading"><div><h2 title="${esc(d.instruction)}">${esc(d.instruction)}</h2><div class="wb-meta"><b class="${finished&&!d.outcome.success?'wb-warn':''}">${esc(status)}</b><span data-elapsed>${m.elapsed_s!=null?Number(m.elapsed_s).toFixed(1)+' 秒':d.startedAt?Math.max(0,Date.now()/1000-d.startedAt).toFixed(1)+' 秒':'计时中'}</span><span>输入 ${m.calls||m.input_tokens?Number(m.input_tokens||0).toLocaleString():'—'} / 输出 ${m.calls||m.output_tokens?Number(m.output_tokens||0).toLocaleString():'—'} token</span><span>${d.actions.length} 次决策 · ${d.revisions.length>1?d.revisions.length-1:0} 次重规划</span>${this.history?'<span>历史回看</span>':''}</div></div><div class="wb-heading-actions">${d.report?.text||d.outcome?.final_answer||downloadFiles(d.outcome).length?'<button data-wb="report">查看结果 ↗</button>':''}${!this.history&&!finished?'<button data-wb="stop" '+(window.AgentWorkbench.runnerActive?'':'disabled')+' title="中止由 Studio 启动的当前任务">中止任务</button>':''}</div></header>
   <div class="wb-layout"><aside class="wb-plan"><div class="wb-panel-title">${pastQueue?'该动作后进度':'子任务进度'} <div class="wb-plan-tools">${rev?.reason?`<button class="wb-note-trigger" data-wb="note" data-revision="${esc(rev.revision)}" aria-label="查看重规划说明" aria-expanded="false" title="查看重规划说明"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M11 3H4a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M7 7h3M7 11h6M7 14h4M13 3l4 4M13 3v4h4"/></svg></button>`:''}<select data-plan aria-label="计划版本"><button><selectedcontent></selectedcontent></button>${d.revisions.map((r,i)=>`<option value="${esc(r.revision)}" ${r===rev?'selected':''}>${r.legacy?'历史计划（未记录版本）':'计划 '+(Number(r.revision)+1)}${i===d.revisions.length-1?' · 最新':''}</option>`).join('')}</select></div></div>
   ${steps.map((s,i)=>{const group=d.actions.filter(x=>x.plan_step_id===s.id);return `<section class="wb-subtask"><button class="wb-subtask-title" data-wb="subtask" data-subtask="${esc(s.id)}" data-revision="${esc(rev.revision)}" aria-label="查看子任务 ${i+1} 详情"><span class="wb-state ${s.status==='done'?'done':''}">${s.status==='done'?'✓':s.status==='doing'?'◉':i+1}</span><span class="wb-goal" title="${esc(s.goal||s.title||s.description||'子任务')}">${esc(s.goal||s.title||s.description||'子任务')}</span></button><div class="wb-subtask-actions">${group.map(x=>`<button data-wb="select" data-step="${x.step}" class="${x===a?'selected':''}" title="${esc(actionText(x.action))}"><span>${x.step}</span><span class="wb-action-label">${esc(actionLabel(x.action))}</span><small title="${esc(name(x.execution_info?.status))}">${x.execution_info?.status==='rejected'?'!':x.execution_info?.status==='executed'?'·':'○'}</small></button>`).join('')||`<small>${s.phase==='report'?`<button data-wb="report" ${!d.report?.text?'disabled':''}>${d.report?.text?'框架交付 · '+(d.report.verification_status?name(d.report.verification_status):'待验收')+' · 查看结果':'等待框架生成结果'}</button>`:s.status==='done'?'系统已核查；无单独动作记录':finished?'未记录独立完成核查 · 点击查看':'尚未记录动作'}</small>`}</div></section>`}).join('')||'<p class="wb-muted">等待规划结果</p>'}
   ${earlierActionGroups(d,steps,rev).map(g=>`<details class="wb-detail wb-plan-history" data-key="history-${g.revision??'unlinked'}"><summary><span>${g.revision!=null?'计划 '+(g.revision+1)+' 的动作记录':'未关联子任务的动作记录'}</span><small>${g.actions.length} 次</small></summary><div class="wb-detail-body"><p class="wb-muted">${g.revision!=null?'这些动作发生在该版本计划下，重新规划后仍保留。点击可回看当时的计划和执行结果。':'已记录的决策，历史数据缺少子任务关联；不表示尚未执行。'}</p>${g.actions.map(x=>`<button class="wb-unassigned ${x===a?'selected':''}" data-wb="select" data-step="${x.step}" title="${esc(actionText(x.action))}"><span>${x.step}</span><span class="wb-action-label">${esc(actionLabel(x.action))}</span><small>${esc(x.execution_info?.dispatched?'已派发':x.execution_info?.status==='rejected'?'已拦截':normalizeAction(x.action).action==='request_finish'?'请求验收':'查看记录')}</small></button>`).join('')}</div></details>`).join('')}

   ${d.unassigned_calls?.length?`<details class="wb-detail" data-key="run-calls"><summary>规划与运行级调用 · ${d.unassigned_calls.length}</summary>${d.unassigned_calls.map(c=>callView(c,this.run)).join('')}</details>`:''}
   ${d.knowledge?.entries?.length?raw('规划采用的知识',d.knowledge,this.run):''}
   </aside><main class="wb-main" aria-label="动作画面"><nav class="wb-action-nav"><div class="wb-action-controls"><button data-wb="prev" ${idx<=0?'disabled':''} aria-label="上一动作">← 上一动作</button><select data-action-select aria-label="选择动作" title="${esc('动作 '+(a?.step??'—')+' · '+actionLabel(a?.action))}"><button><selectedcontent></selectedcontent></button>${d.actions.map(x=>`<option value="${x.step}" ${x===a?'selected':''}>动作 ${x.step} · ${esc(actionLabel(x.action))}</option>`).join('')}</select><button data-wb="next" ${idx<0||idx>=d.actions.length-1?'disabled':''}>下一动作 →</button></div>${!this.history?`<button data-wb="live" class="${this.follow?'active':''}">${this.follow?'● 跟随最新':'回到最新动作'}</button>`:'<span class="wb-muted">← → 切换动作</span>'}</nav>
   <div class="wb-current"><strong>${esc(a?.plan_step?.goal||'动作详情')}</strong><span class="wb-mode">${esc(mode)}</span><small>${a?.plan_revision!=null?'计划 '+(a.plan_revision+1):'未记录计划版本'}</small></div>
   <ol class="wb-stages">${stage.map(([n,status])=>`<li class="${status}" title="${esc(n)}" aria-label="${esc(n)}">${status==='done'?'✓':status==='blocked'?'!':status==='active'?'◉':'○'} ${esc(n)}</li>`).join('')}</ol>
   ${mode==='CLI'?`<section class="wb-terminal" aria-label="CLI 后台执行"><header><span>CLI · ${esc(e.operation||action.args?.operation||'后台操作')}</span><span>${esc(name(e.status)||'等待执行')}${e.exit_code!=null?' · exit '+esc(e.exit_code):''} · ${ms(e.latency_ms)}</span></header><div class="wb-terminal-cwd">${esc(e.cwd||a?.observation?.info?.cli_capability?.workspace||'默认工程目录')}</div><pre class="wb-terminal-command">${esc(e.command||actionText(action))}</pre>${e.stdout?`<pre>${esc(e.stdout)}</pre>`:''}${e.stderr?`<pre class="wb-terminal-error">${esc(e.stderr)}</pre>`:''}${e.file||e.entries?`<pre>${esc(JSON.stringify(e.file||e.entries,null,2))}</pre>`:''}${e.stdout_truncated||e.stderr_truncated?'<small>输出较长，已截断</small>':''}<footer>${e.effect_verified?'框架已回读文件操作结果；任务验收单独显示':e.status?'执行器已返回；完成情况见下方独立核查':'命令等待派发或执行中'}</footer></section>`:`<section class="wb-screen"><div class="wb-screen-head"><span title="${esc(url)}">${esc(url||'当前环境')}</span><div>${['before','after',...(!this.history&&!finished?['live']:[])].map(v=>`<button data-wb="frame" data-frame="${v}" class="${this.frame===v?'active':''}" ${v==='live'&&!this.follow?'disabled':''}>${{before:'动作前',after:'动作后',live:'实时画面'}[v]}</button>`).join('')}<button data-wb="zoom" ${!shot?'disabled':''}>放大 ↗</button></div></div><div class="wb-shot">${shot?`<img alt="${live?'受控浏览器实时预览':'该动作的'+(this.frame==='before'?'前':'后')+'观察截图'}" src="${esc(shot)}">`:`<div class="wb-muted">${this.frame==='live'?'等待受控浏览器实时画面；桌面任务展示动作观察截图':this.frame==='after'?'动作后截图尚未记录':'暂无动作前截图'}</div>`}</div><div class="wb-shot-caption">${live?'实时预览 · '+new Date(d.liveFrame.captured_at*1000).toLocaleTimeString()+' · 约每秒刷新；不作为动作验收证据':'动作 '+(a?.step??'—')+' · '+(this.frame==='before'?'决策前观察':'执行后观察')+' · 保持原始比例'}<span>${ms(a?.timing_ms?.total)}</span></div></section>`}
   </main><aside class="wb-inspector" aria-label="动作输入输出与核查"><div class="wb-inspector-heading">动作 ${a?.step??'—'} · 详情 <span>${esc(mode)}</span></div><div class="wb-action-line"><code>${esc(actionText(action))}</code><span>${esc(action.action==='request_finish'?(finalReview?'验收已返回':'已提交验收'):name(e.status))}${e.dispatched===false?' · 未派发':''}</span></div>
   <nav class="wb-tabs">${[['checks','执行核查'],['input','模型输入'],['output','模型输出'],['report','任务结果']].map(([v,t])=>`<button data-wb="tab" data-tab="${v}" class="${this.tab===v?'active':''}">${t}${v==='report'&&d.report?.status==='streaming'?' ●':''}</button>`).join('')}</nav>
   <div class="wb-evidence">${this.tab==='input'?`${raw('实际送入决策模型的完整 messages',a?.policy_input||'尚未记录',this.run)}${raw('决策模板与实际执行模式',a?.prompt_metadata||{},this.run)}${raw('动作前原始观察 · AX / DOM',a?.observation?.context||[],this.run)}${(a?.decision_calls||[]).map(c=>callView(c,this.run)).join('')}`:this.tab==='output'?`${raw('决策模型原始输出',a?.policy_output||'等待模型返回',this.run)}${raw(e.status?'框架动作记录':'模型提议动作（待框架校验）',action,this.run)}${raw('定位结果与执行通道',{grounding:a?.grounding,routing:a?.routing},this.run)}`:this.tab==='report'?this.reportHTML():`${e.reason?`<p>${esc(e.reason)}</p>`:''}${raw('系统派发与执行结果',e.status?e:a?.execution||a?.outcome||'尚未执行',this.run)}${reviews.length?reviews.map((r,i)=>raw(`${r.method==='code'?'代码核查':'独立核查'} · ${name(r.status)} · ${r.goal||r.reason||''}`,r,this.run)).join(''):a?.step_verification?.status?raw('步骤核查',a.step_verification,this.run):'<p class="wb-muted">本动作尚无核查结论</p>'}${calls.filter(c=>c.actor_role!=='decision'&&c.actor_role!=='planner').map(c=>callView(c,this.run)).join('')}${raw('框架等待与动作耗时',{operations:a?.framework_operations,timing_ms:a?.timing_ms},this.run)}`}</div>
   </aside></div></section>`;
  this.root.querySelectorAll('details').forEach((x,i)=>{if(!x.dataset.key)x.dataset.key=this.tab+':'+i;if(this.openState.has(x.dataset.key))x.open=this.openState.get(x.dataset.key);});
  const evidence=this.root.querySelector('.wb-evidence');if(evidence)evidence.scrollTop=oldScroll;
  this.root.scrollTop=rootScroll;const plan=this.root.querySelector('.wb-plan');if(plan)plan.scrollTop=planScroll;
  for(const key of animated){const el=this.root.querySelector(key);if(el&&previousText.get(key)!==el.textContent)this.animatePanel(key);}
 }
 async openResultFile(button){
  if(this.fileBusy)return;
  const run=this.run;this.fileBusy=button.dataset.file;this.fileMessage='正在请求执行电脑…';this.draw();
  try{
   const response=await fetch('/local/result-file',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run,scope:this.history?'history':'live',artifact_id:button.dataset.file,operation:button.dataset.operation})});
   const result=await response.json();
   if(this.run===run)this.fileMessage=response.ok?(result.ok?result.message:result.error):'请求失败，请检查执行电脑连接。';
  }catch{if(this.run===run)this.fileMessage='无法连接执行电脑，请检查连接后重试。';}
  finally{if(this.run===run){this.fileBusy=null;this.draw();this.animatePanel('.wb-file-message');}}
 }
 filesHTML(){const files=downloadFiles(this.data.outcome);if(!files.length)return '';
  return `<section class="wb-result-files" aria-label="下载文件"><header>下载文件 <small>在执行任务的电脑上打开</small></header>${files.map(f=>`<div class="wb-result-file"><div><strong>${esc(f.path.split(/[\\/]/).pop())}</strong><small>${f.bytes!=null?esc((f.bytes/1048576).toFixed(2))+' MB · ':''}${f.status==='passed'?'文件已核实':'下载记录 · 文件核查未通过'}</small><span title="${esc(f.path)}">${esc(f.path)}</span></div><nav><button data-wb="result-file" data-file="${esc(f.id)}" data-operation="open" ${this.fileBusy?'disabled':''}>打开文件 ↗</button><button data-wb="result-file" data-file="${esc(f.id)}" data-operation="reveal" ${this.fileBusy?'disabled':''}>所在文件夹 ↗</button></nav></div>`).join('')}<p class="wb-file-message" role="status" aria-live="polite">${esc(this.fileMessage||'')}</p></section>`;
 }
 reportHTML(){const r=this.data.report||{},text=r.text||this.data.outcome.final_answer;return `<div class="wb-report">${this.filesHTML()}<div class="wb-report-status"><span>整项任务结果 · </span>${text?'<button data-wb="save-report">保存文字结果 ↓</button>':''}${esc(r.status==='complete'?'正文已生成':name(r.status||'等待结果'))}${r.version>1?' · 第 '+r.version+' 版':''}${r.verification_status?' · 验收：'+esc(name(r.verification_status)):''}${r.error?' · '+esc(r.error):''}</div><div class="wb-report-text formatted ${r.status==='streaming'?'streaming':''}" aria-live="polite">${reportMarkup(text||'分析任务将在读取来源后，于此处流式生成结果。',r.sources||[])}</div><div class="wb-sources">${(r.sources||[]).map(s=>`<a href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener noreferrer">[${esc(s.id)}] ${esc(s.title||s.url)} ↗</a>`).join('')}</div>${r.sources?.length?raw('已观察的来源摘录（归档证据）',r.sources,this.run):''}</div>`;}
 ingest(p){const event=p.event;if(event==='workbench_start'){if(this.data.startedAt&&p.ts<this.data.startedAt)return;if(this.run!==p.run_id){this.fileMessage='';this.fileBusy=null;this.data=empty();this.run=p.run_id;this.seen.clear();this.follow=true;this.frame='live';this.selected=null;this.planVersion=null;}this.data.instruction=p.instruction;this.data.startedAt=p.ts;this.data.phase='准备环境与规划';this.draw();return;}
  if(p.run_id&&this.run&&p.run_id!==this.run)return;
  if(!this.run)return;
  const key=p.record?.event_id||[p.ts,event,p.step,p.sequence].join(':');if(this.seen.has(key))return;this.seen.add(key);
  if(event==='live_frame'){this.data.liveFrame=p;if(this.follow&&this.frame==='live'&&((this.data.actions.at(-1)?.next_observation||this.data.actions.at(-1)?.observation)?.info?.active_surface!=='desktop')){const url=this.root.querySelector('.wb-screen-head>span');if(url){url.textContent=p.url;url.title=p.url;}const img=this.root.querySelector('.wb-shot img');if(img){img.src=imageSrc(p.screenshot);const caption=this.root.querySelector('.wb-shot-caption');if(caption)caption.firstChild.textContent='实时预览 · '+new Date(p.captured_at*1000).toLocaleTimeString()+' · 不作为动作验收证据';return;}this.update(()=>{});}return;}
  if(event==='report_delta'){const r=this.data.report;if(r.version!==p.version)return;if((r.sequence||0)>=p.sequence)return;r.sequence=p.sequence;r.text=p.text??((r.text||'')+p.delta);if(this.tab==='report'){const el=this.root.querySelector('.wb-report-text');if(el){const panel=this.root.querySelector('.wb-evidence');const atEnd=panel&&panel.scrollHeight-panel.scrollTop-panel.clientHeight<70;el.innerHTML=reportMarkup(r.text,r.sources||[]);if(!matchMedia('(prefers-reduced-motion: reduce)').matches)el.lastElementChild?.animate([{opacity:.65},{opacity:1}],{duration:160});if(atEnd)panel.scrollTop=panel.scrollHeight;}}return;}
  this.update(()=>{
   const d=this.data,rec=p.record||{};const type=rec.event_type;
   const get = n => {let a=d.actions.find(a=>a.step===n);if(!a){a={step:n,calls:[],step_reviews:[]};d.actions.push(a);d.actions.sort((a,b)=>a.step-b.step);}return a;};
   if(event==='workbench_record'){
    if(type==='action_context'){Object.assign(get(rec.step),rec);d.phase='决策中';}
    if(type==='transition'||type==='transition_finalized'){const t=rec.transition||{};Object.assign(get(t.step),t);d.phase='执行中';}
    if(type==='plan_revision'){d.revisions.push(rec);d.phase='计划已就绪';}
    if(type==='model_call'){const c=rec.call||{};const u=c.usage||c.response?.usage||{};d.metrics.calls=(d.metrics.calls||0)+1;d.metrics.input_tokens=(d.metrics.input_tokens||0)+(u.prompt_tokens??u.input_tokens??0);d.metrics.output_tokens=(d.metrics.output_tokens||0)+(u.completion_tokens??u.output_tokens??0);const n=c.decision_step||Number((rec.parent_decision_id||'').replace('a_',''));if(n){const a=get(n);if(!a.calls.some(x=>x.call_id&&x.call_id===c.call_id))a.calls.push(c);if(c.actor_role==='decision'){a.policy_input=c.messages;a.policy_output=c.response;try{a.action=JSON.parse(c.response).next_action}catch{}}}else d.unassigned_calls.push(c);}
    if(type==='execution'){const n=Number((rec.parent_decision_id||'').replace('a_',''));if(n)Object.assign(get(n),{execution_info:rec.execution_info,action:rec.action,next_observation:rec.next_observation});d.phase='核查中';}
    if(type==='route'){const n=Number((rec.parent_decision_id||'').replace('a_',''));if(n)Object.assign(get(n),{routing:rec.routing,operation_mode:rec.operation_mode});}
    if(type==='episode_error'){d.outcome={success:false,termination_reason:rec.error_type||'运行异常'};d.metrics.elapsed_s=rec.elapsed_s;this.follow=false;this.frame='after';}
    if(type==='episode_end'){d.outcome=rec.outcome||{};d.metrics={...d.metrics,elapsed_s:rec.elapsed_s};this.follow=false;this.frame='after';}
   }
   if(event==='execution_queue'&&d.revisions.length)d.revisions.at(-1).queue=p.queue||p;
   if(event==='step_verification'){const n=p.decision_step||p.step;if(n){const a=get(n);a.step_verification=p;const i=a.step_reviews.findIndex(r=>r.review_id===p.review_id&&r.method===p.method);if(i>=0)a.step_reviews[i]=p;else a.step_reviews.push(p);}}
   if(event==='framework_wait')d.phase=p.status==='completed'?'等待结束':'框架等待 · '+({pending_download:'文件下载',page_loading:'页面加载',visual_readiness:'画面就绪'}[p.reason]||p.reason||'环境就绪');
   if(event==='run_metrics')d.metrics=p.metrics;
   if(type==='action_context'&&rec.observation?.info?.active_surface==='desktop'){this.frame='after';d.liveFrame=null;}
   if(event==='report_start'){d.report={...p,text:'',status:'streaming'};if(this.follow)this.tab='report';d.phase='整理任务结果';}
   if(event==='report_complete')d.report={...p};
   if(event==='report_error')d.report={...d.report,status:'failed',error:p.reason,text:p.text||d.report.text};
   if(event==='report_verification')d.report.verification_status=p.status;
   if(event==='phase_timing'&&p.status==='running')d.phase=phases[p.phase]||p.phase;
  });
 }
}
let live=null,historical=null;
function ensureLive(){if(!live){const root=document.createElement('div');root.id='live-workbench';document.getElementById('view-live').prepend(root);live=new Workbench(root);document.getElementById('timeline').hidden=true;}return live;}
setInterval(()=>{if(live?.follow&&live.frame==='live'&&live.data.liveFrame){const age=Date.now()/1000-live.data.liveFrame.captured_at;const cap=live.root.querySelector('.wb-shot-caption');if(cap&&age>4)cap.firstChild.textContent='实时画面更新延迟 · '+Math.floor(age)+' 秒前 · 不作为动作验收证据';}if(live?.data.startedAt&&!Object.hasOwn(live.data.outcome,'success')){const e=live.root.querySelector('[data-elapsed]');if(e)e.textContent=Math.max(0,Date.now()/1000-live.data.startedAt).toFixed(1)+' 秒';}},1000);
window.AgentWorkbench={runnerActive:false,updateRunner:s=>{window.AgentWorkbench.runnerActive=['running','starting'].includes(s.status);const b=live?.root.querySelector('[data-wb=stop]');if(b)b.disabled=!window.AgentWorkbench.runnerActive;},ingest:p=>ensureLive().ingest(p),mountHistory:(run,data)=>{const root=document.getElementById('local-body');historical=new Workbench(root,true);historical.mount(data,run);},reset:()=>{if(live){live.data=empty();live.run='';live.draw();}},Workbench};
document.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight'].includes(e.key)||e.altKey||e.ctrlKey||e.metaKey||e.target.closest('input,textarea,select,[contenteditable=true],dialog'))return;const w=document.getElementById('view-live').classList.contains('active')?live:document.getElementById('view-local').classList.contains('active')?historical:null;if(w&&w.root.querySelector('.wb')){const button=w.root.querySelector(`[data-wb="${e.key==='ArrowLeft'?'prev':'next'}"]`);if(button&&!button.disabled){e.preventDefault();button.click();}}});
})();
