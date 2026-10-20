/* Human demonstrations are independent of model inference. Each request is serialized
   against the observation actually shown in the mirror; DOM text is never HTML. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {session: null, status: 'idle', observation: null, steps: [], busy: false,
    click: null, wheel: null, pointer: null, composing: false, task: '', lastPoint: null,
    drafts: new Map(), restored: false, frameReady: false};
  const active = () => ['recording', 'active', 'running'].includes(state.status);
  const stopped = () => ['stopped', 'completed', 'error'].includes(state.status);
  const pending = () => Boolean(state.click || state.wheel || state.pointer);
  const dialogReady = () => active() && !state.busy && !pending() && !!state.observation?.dialog;
  const ready = () => active() && !state.busy && !!state.observation && state.frameReady && !state.observation.dialog;
  const actionLabels = {click:'单击', double_click:'双击', right_click:'右键点击', drag_and_drop:'拖拽',
    scroll:'滚动', type:'输入文字', press:'按键', hotkey:'组合键', goto:'导航', switch_tab:'切换标签页', handle_dialog:'处理对话框'};
  const controlIds = ['capture-url','capture-go','capture-text','capture-send-text','capture-overwrite',
    'capture-dialog-cancel','capture-dialog-accept','capture-dialog-input'];
  const element = (tag, className, text) => {const e=document.createElement(tag); if(className)e.className=className;if(text!==undefined)e.textContent=text;return e;};
  function showError(message) {$('capture-error').textContent = String(message || '');$('capture-error').hidden = !message;}
  function setBusy(value) {state.busy=value; updateControls();}
  function updateControls() {
    const enabled=ready()&&!pending();
    controlIds.forEach(id => $(id).disabled=!enabled);
    ['capture-dialog-cancel','capture-dialog-accept','capture-dialog-input'].forEach(id=>$(id).disabled=!dialogReady());
    document.querySelectorAll('[data-capture-key],[data-capture-hotkey],#capture-tabbar button').forEach(e=>e.disabled=!enabled);
    $('capture-start').disabled=state.busy || active() || state.status==='paused';
    $('capture-refresh').disabled=!state.session||state.busy||pending()||stopped();
    $('capture-pause').disabled=state.busy||pending()||stopped();
    $('capture-stop').disabled=state.busy||pending()||stopped();
    $('capture-new').disabled=state.busy;
    $('capture-pause').textContent=state.status==='paused'?'继续采集':'暂停';
    $('capture-pause').hidden=stopped(); $('capture-stop').hidden=stopped(); $('capture-new').hidden=!stopped();
    $('capture-busy').hidden=!state.busy||!state.observation;
    $('capture-paused-overlay').hidden=state.status!=='paused';
    $('capture-stage').setAttribute('aria-disabled',String(!ready()));
    $('capture-browser-status').textContent=state.busy?'正在保存…':state.status==='paused'?'已暂停':active()?'操作将被记录':stopped()?'示范已结束':'等待开始';
    const labels={idle:'未开始',recording:'正在采集',active:'正在采集',running:'正在采集',paused:'已暂停',stopped:'已结束',completed:'已结束',error:'采集异常'};
    $('capture-state').textContent=labels[state.status]||state.status;
    $('capture-state').className='capture-state'+(active()?' recording':'');
    const exportable=Boolean(state.session)&&stopped()&&state.steps.length>0&&!state.busy;
    $('capture-export-jsonl').disabled=!exportable; $('capture-export-zip').disabled=!exportable;
    document.querySelectorAll('.step-review-actions button').forEach(e=>e.disabled=state.busy||pending());
  }
  async function api(path, body) {
    const options=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
    const response=await fetch(path,options);
    let data;
    try {data=await response.json();}catch(_){throw new Error(`服务返回了无法解析的响应（HTTP ${response.status}）。`);}
    if(!response.ok||data.ok===false)throw new Error(data.error||data.detail||`请求失败（HTTP ${response.status}）`);
    return data;
  }
  function normalizeObservation(obs) {
    if(!obs){
      state.observation=null;state.frameReady=false;$('capture-frame').hidden=true;$('capture-frame').removeAttribute('src');
      $('capture-stage').classList.remove('has-frame');$('capture-placeholder').hidden=false;$('capture-dialog').hidden=true;
      if(state.session&&!stopped())showError('当前页面观察不可用，请刷新画面后再操作。');return;
    }
    state.observation=obs;
    const image=$('capture-frame');
    const source=obs.screenshot||'';
    if(source) {
      // Only image data URLs and same-origin screenshot endpoints are accepted.
      if(source.startsWith('data:image/')||source.startsWith('/')) {
        if(image.getAttribute('src')!==source){state.frameReady=false;image.src=source;}
        else state.frameReady=image.complete&&image.naturalWidth>0;
        image.hidden=false;$('capture-placeholder').hidden=true;$('capture-stage').classList.add('has-frame');
      }else {state.frameReady=false;showError('截图格式不受支持，请刷新观察。');}
    }else {state.frameReady=false;image.hidden=true;$('capture-placeholder').hidden=false;$('capture-stage').classList.remove('has-frame');}
    $('capture-url').value=obs.url||'';
    $('capture-dimensions').textContent=obs.width&&obs.height?`${obs.width} × ${obs.height} · CSS viewport`:'页面截图与 AX Tree 同步记录';
    const tabs=Array.isArray(obs.tabs)?obs.tabs:[];
    const bar=$('capture-tabbar');bar.replaceChildren();bar.hidden=tabs.length<2;
    tabs.forEach(tab=>{
      const id=tab.id||tab.tab_id||tab.page_id;
      const button=element('button',(tab.active||tab.current||id===obs.page_id)?'active':'',tab.title||tab.url||'标签页');
      button.type='button';button.title=tab.url||tab.title||'';
      button.addEventListener('click',()=>sendAction({kind:'switch_tab',tab_id:id}));bar.append(button);
    });
    const dialog=obs.dialog;
    $('capture-dialog').hidden=!dialog;
    if(dialog) {
      $('capture-dialog-text').textContent=dialog.message||dialog.text||'页面请求确认';
      $('capture-dialog-input').hidden=dialog.type!=='prompt';
      $('capture-dialog-input').value=dialog.default_value||dialog.defaultValue||'';
    }
  }
  function apply(data) {
    if(data.session_id)state.session=data.session_id;
    if(data.status)state.status=data.status;
    if(data.task)state.task=typeof data.task==='string'?data.task:state.task;
    if(Object.prototype.hasOwnProperty.call(data,'observation'))normalizeObservation(data.observation);
    if(Array.isArray(data.steps)){state.steps=data.steps;renderSteps();}
    else if(data.step&&typeof data.step==='object'){
      const idx=state.steps.findIndex(s=>String(s.step)===String(data.step.step));
      if(idx<0)state.steps.push(data.step);else state.steps[idx]=data.step;renderSteps();
    }
    $('capture-session-bar').hidden=!state.session;
    $('capture-setup').hidden=!!state.session;
    $('capture-session-task').textContent=state.task||'人工操作示范';
    $('capture-session-id').textContent=state.session?'SESSION / '+state.session:'';
    if(data.verification) {
      const v=data.verification;const labels={passed:'通过',failed:'未通过',unknown:'暂无充分证据'};
      $('capture-verification').textContent='系统验收：'+(labels[v.status]||v.status||'暂无充分证据')+(v.reason?'\n'+v.reason:'');
      $('capture-verification').classList.toggle('passed',v.status==='passed');
    }
    updateControls();
  }
  function actionStatus(data) {
    const latest=data.step||data.steps?.[data.steps.length-1];
    const execution=data.execution||latest?.execution||latest?.result||latest?.execution_info;
    const status=execution?.status||latest?.execution_status;
    if(['rejected','uncertain','failed','error'].includes(status)){showError(`此操作${status==='uncertain'?'结果不确定':'未成功'}，已作为步骤记录。${execution?.error?' '+execution.error:''} 请检查画面后再操作。`);return false;}
    return true;
  }
  function requestId() {return globalThis.crypto?.randomUUID?.()||`r_${Date.now()}_${Math.random().toString(36).slice(2)}`;}
  async function sendAction(action) {
    if((action.kind==='handle_dialog'?!dialogReady():!ready())||pending())return false;
    setBusy(true);showError('');
    try {
      const data=await api('/collect/action',{session_id:state.session,observation_id:state.observation.id,
        request_id:requestId(),...action});
      apply(data);return actionStatus(data);
    }catch(error){showError(error.message+' 如页面已经变化，请先点镜像右上角刷新观察。');return false;}
    finally {setBusy(false);}
  }
  function point(event) {
    const image=$('capture-frame');const rect=image.getBoundingClientRect();
    if(!rect.width||!rect.height||!state.observation)return null;
    if(event.clientX<rect.left||event.clientX>=rect.right||event.clientY<rect.top||event.clientY>=rect.bottom)return null;
    const width=Number(state.observation.width)||image.naturalWidth;
    const height=Number(state.observation.height)||image.naturalHeight;
    if(!width||!height)return null;
    return {x:Math.max(0,Math.min(width-1,Math.round((event.clientX-rect.left)/rect.width*width))),
      y:Math.max(0,Math.min(height-1,Math.round((event.clientY-rect.top)/rect.height*height)))};
  }
  const stage=$('capture-stage');
  stage.addEventListener('contextmenu',e=>e.preventDefault());
  stage.addEventListener('dragstart',e=>e.preventDefault());
  stage.addEventListener('pointerdown',e=>{
    if(!ready()||state.wheel||e.button>2||$('capture-frame').hidden)return;
    const p=point(e);if(!p)return;
    if(state.click&&Math.hypot(p.x-state.click.x,p.y-state.click.y)>8)return;
    e.preventDefault();stage.focus({preventScroll:true});
    state.pointer={...p,clientX:e.clientX,clientY:e.clientY,button:e.button,id:e.pointerId};
    stage.setPointerCapture(e.pointerId);updateControls();
  });
  stage.addEventListener('pointercancel',()=>{state.pointer=null;updateControls();});
  stage.addEventListener('pointerup',e=>{
    const down=state.pointer;state.pointer=null;
    if(!down||!ready()){updateControls();return;}
    const up=point(e);if(!up){updateControls();return;}
    if(stage.hasPointerCapture(e.pointerId))stage.releasePointerCapture(e.pointerId);
    const moved=Math.hypot(e.clientX-down.clientX,e.clientY-down.clientY)>6;
    state.lastPoint={x:up.x,y:up.y};
    if(moved&&down.button===0) {
      if(state.click){updateControls();return;}
      sendAction({kind:'drag_and_drop',x:down.x,y:down.y,x2:up.x,y2:up.y});return;
    }
    if(down.button===2) {
      if(state.click){updateControls();return;}
      sendAction({kind:'right_click',...up});return;
    }
    if(down.button!==0){updateControls();return;}
    if(state.click) {
      clearTimeout(state.click.timer);state.click=null;
      sendAction({kind:'double_click',...up});
    }else {
      const item={...up,timer:null};
      const commit=()=>{if(state.pointer){item.timer=setTimeout(commit,60);return;}state.click=null;sendAction({kind:'click',x:item.x,y:item.y});};
      item.timer=setTimeout(commit,250);
      state.click=item;updateControls();
    }
  });
  stage.addEventListener('dblclick',e=>e.preventDefault());
  async function flushWheel(next=null) {
    const item=state.wheel;if(!item)return;
    clearTimeout(item.timer);state.wheel=null;
    const success=await sendAction({kind:'scroll',x:item.x,y:item.y,direction:item.direction,
      amount:Math.min(30,Math.max(1,Math.round(item.delta/300)))});
    // A reversal received inside the debounce window becomes a separate action
    // bound to the fresh observation, never a concurrently dispatched request.
    if(next&&success&&!state.frameReady) {
      await new Promise(resolve=>{
        const frame=$('capture-frame');const done=()=>{clearTimeout(timer);frame.removeEventListener('load',done);frame.removeEventListener('error',done);resolve();};
        const timer=setTimeout(done,5000);frame.addEventListener('load',done,{once:true});frame.addEventListener('error',done,{once:true});
      });
    }
    if(next&&success&&ready()) {
      await sendAction({kind:'scroll',x:next.x,y:next.y,direction:next.direction,
        amount:Math.min(30,Math.max(1,Math.round(next.delta/300)))});
    }else if(next)showError('上一组滚动或画面更新未完成，后续滚动没有执行；请检查画面后重试。');
    updateControls();
  }
  stage.addEventListener('wheel',e=>{
    e.preventDefault();
    if(!ready()||state.click||state.pointer||!e.deltaY)return;
    const p=point(e);if(!p)return;
    const delta=Math.abs(e.deltaY)*(e.deltaMode===1?16:e.deltaMode===2?Number(state.observation.height)||720:1);
    const direction=e.deltaY<0?'up':'down';
    const item={...p,direction,delta,timer:null};
    if(state.wheel&&(state.wheel.direction!==direction||Math.hypot(p.x-state.wheel.x,p.y-state.wheel.y)>10)) {
      flushWheel(item);return;
    }
    if(state.wheel){clearTimeout(state.wheel.timer);state.wheel.delta+=delta;}
    else state.wheel=item;
    state.wheel.timer=setTimeout(()=>flushWheel(),300);updateControls();
  },{passive:false});
  stage.addEventListener('keydown',e=>{
    if(!ready()||pending()||e.isComposing)return;
    if(e.key==='Control'||e.key==='Alt'||e.key==='Meta'||e.key==='Shift')return;
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='v')return; // paste is staged in the text composer
    if(e.ctrlKey||e.metaKey||e.altKey) {
      e.preventDefault();const keys=[];if(e.ctrlKey)keys.push('Control');if(e.metaKey)keys.push('Meta');if(e.altKey)keys.push('Alt');if(e.shiftKey)keys.push('Shift');keys.push(e.key);
      sendAction({kind:'hotkey',keys});return;
    }
    if(['Enter','Tab','Escape','Backspace','Delete','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End','PageUp','PageDown'].includes(e.key)) {
      e.preventDefault();sendAction({kind:'press',key:e.key});
    }else if(e.key.length===1) {
      e.preventDefault();$('capture-text').focus();$('capture-text').value+=e.key;
    }
  });
  stage.addEventListener('paste',e=>{
    if(!ready()||pending())return;const text=e.clipboardData?.getData('text/plain');if(!text)return;
    e.preventDefault();$('capture-text').value+=$('capture-text').value?'\n'+text:text;$('capture-text').focus();
  });
  document.querySelectorAll('[data-capture-key]').forEach(b=>b.addEventListener('click',()=>sendAction({kind:'press',key:b.dataset.captureKey})));
  document.querySelectorAll('[data-capture-hotkey]').forEach(b=>b.addEventListener('click',()=>sendAction({kind:'hotkey',keys:b.dataset.captureHotkey.split('+')})));
  const composer=$('capture-text');
  composer.addEventListener('compositionstart',()=>state.composing=true);
  composer.addEventListener('compositionend',()=>state.composing=false);
  composer.addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)&&!e.isComposing&&!state.composing){e.preventDefault();submitText();}});
  async function submitText() {
    if(state.composing||!composer.value||!ready()||pending())return;
    const text=composer.value;
    const ok=await sendAction({kind:'type',text,overwrite:$('capture-overwrite').checked});
    if(ok&&composer.value===text)composer.value='';
  }
  $('capture-send-text').addEventListener('click',submitText);
  $('capture-navigation').addEventListener('submit',e=>{e.preventDefault();const url=$('capture-url').value.trim();if(url)sendAction({kind:'goto',url});});
  $('capture-dialog-cancel').addEventListener('click',()=>sendAction({kind:'handle_dialog',accept:false}));
  $('capture-dialog-accept').addEventListener('click',()=>sendAction({kind:'handle_dialog',accept:true,prompt_text:$('capture-dialog-input').value}));
  $('capture-start').addEventListener('click',async()=>{
    if(state.busy||active())return;
    const task=$('capture-task').value.trim();if(!task){showError('请先填写本次示范的任务描述。');$('capture-task').focus();return;}
    let completion;const raw=$('capture-completion').value.trim();
    try {if(raw)completion=JSON.parse(raw);}catch(_){showError('系统验收条件需要有效的 JSON。');return;}
    showError('');setBusy(true);
    try {
      state.task=task;
      const body={task,start_url:$('capture-start-url').value.trim(),endpoint:$('capture-endpoint').value.trim()||'http://127.0.0.1:9222',
        launch_browser:document.querySelector('[name="capture-browser-mode"]:checked').value==='launch',headless:!$('capture-show-window').checked};
      if(completion)body.completion_spec=completion;
      const data=await api('/collect/start',body);state.steps=[];state.drafts.clear();apply(data);
    }catch(error){showError(error.message);}
    finally{setBusy(false);}
  });
  async function lifecycle(kind) {
    if(!state.session||state.busy||pending())return;
    showError('');setBusy(true);
    try{apply(await api('/collect/'+kind,{session_id:state.session}));}
    catch(error){showError(error.message);}finally{setBusy(false);}
  }
  $('capture-pause').addEventListener('click',()=>lifecycle(state.status==='paused'?'resume':'pause'));
  $('capture-stop').addEventListener('click',()=>lifecycle('stop'));
  $('capture-refresh').addEventListener('click',async()=>{
    if(!state.session||state.busy||pending())return;showError('');setBusy(true);
    try{apply(await api('/collect/frame?session_id='+encodeURIComponent(state.session)));}
    catch(error){showError(error.message);}finally{setBusy(false);}
  });
  $('capture-new').addEventListener('click',()=>{
    if(state.busy||!stopped())return;
    state.session=null;state.status='idle';state.observation=null;state.frameReady=false;state.steps=[];state.drafts.clear();state.lastPoint=null;
    $('capture-frame').hidden=true;$('capture-frame').removeAttribute('src');$('capture-stage').classList.remove('has-frame');
    $('capture-placeholder').hidden=false;$('capture-tabbar').hidden=true;$('capture-dialog').hidden=true;
    $('capture-setup').hidden=false;$('capture-session-bar').hidden=true;$('capture-url').value='';
    $('capture-verification').textContent='系统验收：尚未执行';$('capture-verification').classList.remove('passed');
    renderSteps();updateControls();showError('');$('capture-task').focus();
  });
  function stepAction(step) {
    const a=step.action||step.policy_action||step.raw_action||{};
    return {kind:a.kind||a.action||a.type||step.kind||'操作',args:a.args||a};
  }
  function renderSteps() {
    const list=$('capture-steps');const oldOpen=new Set(Array.from(list.querySelectorAll('details[open]')).map(e=>e.dataset.step));
    const nearBottom=list.scrollHeight-list.scrollTop-list.clientHeight<70;
    list.replaceChildren();$('capture-step-count').textContent=state.steps.length;
    $('capture-reviewed-count').textContent='已审阅 '+state.steps.filter(s=>s.reviewed||s.keep===true||s.keep===false).length;
    if(!state.steps.length){const empty=element('div','timeline-empty');empty.append(element('span','timeline-empty-line'),element('strong','','还没有操作步骤'),element('p','','开始后，你的操作会出现在这里。\n鼠标移动不会单独记录。'));list.append(empty);return;}
    state.steps.forEach((step,index)=>{
      const id=String(step.step??step.id??index+1);const {kind,args}=stepAction(step);
      const card=element('details','capture-step'+(step.keep===true?' kept':step.keep===false?' discarded':''));card.dataset.step=id;
      card.open=oldOpen.has(id)||index===state.steps.length-1;
      const summary=element('summary','capture-step-head');summary.append(element('span','step-number',String(index+1).padStart(2,'0')),
        element('span','step-action-title',actionLabels[kind]||kind),element('span','step-review-status',step.keep===true?'已保留':step.keep===false?'已丢弃':'待审阅'));card.append(summary);
      const visibleArgs={...args};delete visibleArgs.observation_metadata;
      card.append(element('pre','step-args',JSON.stringify(visibleArgs,null,1).slice(0,600)));
      if(['rejected','uncertain','failed','error'].includes(step.execution_status))card.append(element('p','step-semantic-warning execution-warning',step.execution_status==='uncertain'?'执行结果不确定，请核对前后截图。':'执行未成功，请核对前后截图。'));
      if(step.semantic_error)card.append(element('p','step-semantic-warning',step.semantic_error));
      if(step.sft_exclude_reason)card.append(element('p','step-semantic-warning','此操作已归档，但当前动作格式无法准确表达它，不会导出为训练样本。'));
      const shots=element('div','step-shots');
      [['screenshot','操作前'],['next_screenshot','操作后']].forEach(([key,label])=>{
        if(!step[key]||!state.session)return;
        const figure=element('figure','');const img=element('img','step-thumbnail');
        img.alt=label+'截图';img.loading='lazy';
        img.src='/collect/asset?'+new URLSearchParams({session_id:state.session,name:String(step[key]).split('/').pop()});
        img.addEventListener('click',()=>{const overlay=$('overlay');$('overlay-img').src=img.src;overlay.style.display='flex';});
        figure.append(img,element('figcaption','',label));shots.append(figure);
      });
      if(shots.childElementCount)card.append(shots);
      const draft=state.drafts.get(id)||{target:step.target||args.target||'',intent:step.intent||'',expected_effect:step.expected_effect||''};state.drafts.set(id,draft);
      [['target','语义目标','例如：搜索框、论文的 PDF 下载链接'],['intent','操作意图','这一步要做什么'],['expected_effect','预期变化','执行后应该看到什么']].forEach(([key,label,placeholder])=>{
        const labelEl=element('label','',label);labelEl.htmlFor='capture-step-'+id+'-'+key;
        const field=element(key==='target'?'input':'textarea','');field.id=labelEl.htmlFor;if(field.tagName==='TEXTAREA')field.rows=1;
        field.value=draft[key]||'';field.placeholder=placeholder;field.addEventListener('input',()=>{draft[key]=field.value;});card.append(labelEl,field);
      });
      const buttons=element('div','step-review-actions');const keep=element('button','studio-btn primary',step.keep===true?'保存修改':'保留此步');
      const discard=element('button','studio-btn secondary discard','丢弃');keep.type=discard.type='button';
      keep.addEventListener('click',()=>review(id,true));discard.addEventListener('click',()=>review(id,false));buttons.append(keep,discard);card.append(buttons);list.append(card);
    });
    if(nearBottom)requestAnimationFrame(()=>list.scrollTop=list.scrollHeight);
  }
  async function review(step,keep) {
    if(state.busy||pending())return;showError('');setBusy(true);
    try {
      const data=await api('/collect/review',{session_id:state.session,step,keep,...state.drafts.get(step)});
      if(!data.steps&&!data.step) {const record=state.steps.find(s=>String(s.step??s.id)===step);if(record)Object.assign(record,{keep,reviewed:true,...state.drafts.get(step)});renderSteps();}
      apply(data);
    }catch(error){showError(error.message);}finally{setBusy(false);}
  }
  async function exportData(format) {
    if(!state.session||state.busy||!stopped())return;
    showError('');setBusy(true);
    try {
      const url='/collect/export?'+new URLSearchParams({session_id:state.session,format,reviewed_only:String($('capture-reviewed-only').checked)});
      const response=await fetch(url);
      if(!response.ok||response.headers.get('content-type')?.includes('application/json')) {
        let data={};try{data=await response.json();}catch(_){}
        throw new Error(data.error||data.detail||`导出失败（HTTP ${response.status}）`);
      }
      const blob=await response.blob();const href=URL.createObjectURL(blob);const link=document.createElement('a');
      link.href=href;link.download=`agent-frame-${state.session}.${format}`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(href),30000);
    }catch(error){showError(error.message);}finally{setBusy(false);}
  }
  $('capture-export-jsonl').addEventListener('click',()=>exportData('jsonl'));
  $('capture-export-zip').addEventListener('click',()=>exportData('zip'));
  $('capture-frame').addEventListener('load',()=>{state.frameReady=Boolean(state.observation?.screenshot);updateControls();});
  $('capture-frame').addEventListener('error',()=>{state.frameReady=false;updateControls();showError('浏览器截图无法显示，请刷新当前观察后继续。');});
  window.CaptureStudio={async open(){
    if(state.busy||pending()||state.restored)return;state.restored=true;
    try{const data=await api('/collect/status');if(data.session_id)apply(data);}
    catch(error){showError('采集服务不可用：'+error.message);state.restored=false;}
  }};
  updateControls();
})();
