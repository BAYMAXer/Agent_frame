/* One debugger for text generation, grounding and System One classification. */
(() => {
  'use strict';
  const $=id=>document.getElementById('lab-'+id), STORAGE='yaero_model_lab_v1';
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const defaultQuestions={status:{type:'choice',instructions:'根据任务和可观察证据判断完成状态；证据不足时选择 unknown。',criteria:{passed:'要求已满足',failed:'要求未满足',pending:'操作仍在进行',unknown:'证据不足'}}};
  const presets=[
    {id:'mode',title:'操作模式',description:'AX 可用，选择 DOM',state:'合成调试证据：任务是在校园门户搜索通知。当前为网页搜索页，AX 有 textbox ref=21 与 搜索按钮 ref=22；CDP 可用。没有文件操作需求。',questions:{mode:{type:'choice',instructions:'选择完成当前操作最适合的通道。优先利用可用的网页结构。',criteria:{dom:'网页结构定位',gui:'截图视觉定位',cli:'后台文件或终端操作'}}}},
    {id:'effect',title:'动作核查',description:'动作前后，确认搜索结果',state:'合成调试证据：用户要搜索“招生简章”。动作前搜索框为空；执行 type(text="招生简章", submit_key="Enter")。动作后 URL 为 /search?q=招生简章；AX 主区域显示“搜索结果：招生简章”，出现3个相关链接。执行器未报错。',questions:{effect:{type:'choice',instructions:'仅判断这次搜索动作是否达到预期，不判断整项任务完成。',criteria:{passed:'搜索成功且结果可见',failed:'动作未成功',pending:'仍在加载',unknown:'证据不足'}}}},
    {id:'waiting',title:'加载等待',description:'传输未完成，应继续等',state:'合成调试证据：任务是下载报告。浏览器事件 status=in_progress，received_bytes=5242880，total_bytes=10485760。文件仍为 report.pdf.crdownload，框架尚无 completed artifact。截图只显示“正在下载”。',questions:{settled:{type:'choice',instructions:'当前应继续等待还是结束等待？不要把下载已开始当作已完成。',criteria:{waiting:'传输仍在进行，需要框架等待',complete:'传输已经完成',unknown:'证据不足'}}}},
    {id:'file',title:'改名证据',description:'文件已下载，名称不符',state:'合成调试证据：任务要求下载指定论文并命名为 data.pdf。代码核查：正确论文已下载，文件存在且4156037字节，来源匹配；当前文件名 paper.pdf；data.pdf 不存在。filename_check=failed。',questions:{completion:{type:'choice',instructions:'是否满足用户全部要求？代码明确失败的条件不能被覆盖。',criteria:{passed:'全部要求已满足',failed:'仍有明确未满足的要求',pending:'操作仍在进行',unknown:'证据不足'}}}},
    {id:'knowledge',title:'知识相关性',description:'校园通知与采购知识',state:'合成调试证据：任务=去学校官网查看招生通知。知识 A：校园官网，别名学校官网，正文介绍招生通知入口。知识 B：采购系统，正文介绍供应商与采购订单流程。',questions:{campus:{type:'noul',instructions:'知识 A 是否有助于完成任务？'},procurement:{type:'noul',instructions:'知识 B 是否有助于完成任务？'}}}
  ];
  let sessions=[],activeId=null,loaded=false,busy=false,reading=false,timer=null;
  const keys=new Map(); // Credentials never enter stored session history.
  const current=()=>sessions.find(s=>s.id===activeId);
  const field=(id,fallback='')=>document.getElementById(id)?.value||fallback;
  function defaults(mode){
    if(mode==='jev')return {model:'vllm-sr/d3-flash',url:field('judge-url','http://10.44.101.62:8007/v1/systemone'),provider:'openai'};
    const grounding=mode==='grounding';
    return {model:field(grounding?'run-ground':'run-model',grounding?'qwen3.7-plus':'qwen3.7-flash'),url:field(grounding?'run-ground-url':'run-model-url','https://dashscope.aliyuncs.com/compatible-mode/v1'),provider:'openai'};
  }
  function error(text){$('error').textContent=text;$('error').hidden=!text;}
  function persist(){
    try{localStorage.setItem(STORAGE,JSON.stringify(sessions));}
    catch{error('浏览器存储空间不足；本页仍保留完整记录，刷新可能丢失最近修改。');}
  }
  function create(mode='decision'){
    capture();const s={id:crypto.randomUUID(),name:'新调试',mode,config:defaults(mode),draft:'',images:[],questions:JSON.stringify(defaultQuestions,null,2),system:'',timeout:120,turns:[],messages:[]};
    sessions.push(s);activeId=s.id;persist();render();return s;
  }
  function load(){
    try{const raw=JSON.parse(localStorage.getItem(STORAGE)||'null');if(Array.isArray(raw))sessions=raw.filter(s=>s&&s.id&&Array.isArray(s.turns)&&['decision','grounding','jev'].includes(s.mode));
      else {const old=JSON.parse(localStorage.getItem('agent_chat_sessions_v1')||'[]');if(Array.isArray(old))sessions=old.map(s=>{
        const mode=s.type==='decision'?'decision':'grounding';if(s.key)keys.set(s.id,s.key);
        return {id:s.id,name:s.name,mode,config:{model:s.model,url:s.url,provider:s.provider||'openai'},draft:'',images:[],system:'',timeout:120,questions:JSON.stringify(defaultQuestions,null,2),turns:[],messages:Array.isArray(s.messages)?s.messages:[],legacyMessages:Array.isArray(s.messages)?[...s.messages]:[],legacy:true};
      });}
    }catch{error('无法读取旧调试记录，旧数据未被修改。');}
    loaded=true;if(sessions.length){activeId=sessions.at(-1).id;render();}else create();
  }
  function capture(){const s=current();if(!s)return;s.draft=$('input').value;s.system=$('system').value;s.questions=$('questions').value;s.timeout=Number($('timeout').value);s.config={model:$('model').value.trim(),url:$('url').value.trim(),provider:$('provider').value};keys.set(s.id,$('key').value);}
  function setMode(mode){if(busy||reading)return;capture();const s=current();if(!s||s.mode!==mode){const draft=s?.draft||'',imgs=[...(s?.images||[])];const next=create(mode);next.draft=draft;next.images=imgs;persist();render();}else render();}
  function select(id){if(busy||reading)return;capture();activeId=id;persist();render();}
  function history(){
    $('sessions').innerHTML=sessions.slice().reverse().map(s=>`<div class="lab-session ${s.id===activeId?'selected':''}"><button data-session="${esc(s.id)}" title="${esc(s.name)}"><span>${esc(s.name)}</span><small>${{decision:'对话',grounding:'定位',jev:'Jev'}[s.mode]} · ${s.turns.length||Math.floor(s.messages.length/2)} 次</small></button><button data-delete="${esc(s.id)}" aria-label="删除 ${esc(s.name)}">×</button></div>`).join('');
    $('sessions').querySelectorAll('[data-session]').forEach(b=>b.onclick=()=>select(b.dataset.session));
    $('sessions').querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>{if(busy||reading)return;if(b.dataset.confirm!=='yes'){b.dataset.confirm='yes';b.textContent='确认';b.title='再次点击删除调试记录';return;}capture();sessions=sessions.filter(s=>s.id!==b.dataset.delete);keys.delete(b.dataset.delete);if(activeId===b.dataset.delete)activeId=sessions.at(-1)?.id;if(!sessions.length)create();else {persist();render();}});
  }
  function images(){const s=current();$('clipboard-status').textContent=s?.images?.length?`已附加 ${s.images.length} 张截图`:'⌘V / Ctrl+V · 最多 4 张';$('images').innerHTML=(s?.images||[]).map((u,i)=>`<figure><img tabindex="0" role="button" title="放大截图" src="${esc(u)}" alt="输入截图 ${i+1}"><button data-remove-image="${i}" aria-label="移除截图 ${i+1}">×</button><figcaption>${i+1}</figcaption></figure>`).join('');$('images').querySelectorAll('button').forEach(b=>b.onclick=()=>{if(busy||reading)return;s.images.splice(Number(b.dataset.removeImage),1);persist();images();});}
  function render(){
    const s=current();if(!s)return;history();const jev=s.mode==='jev';
    document.querySelectorAll('[data-lab-mode]').forEach(b=>{b.classList.toggle('active',b.dataset.labMode===s.mode);b.setAttribute('aria-pressed',String(b.dataset.labMode===s.mode));});
    for(const [id,value] of Object.entries({input:s.draft,system:s.system,questions:s.questions,model:s.config.model,url:s.config.url,provider:s.config.provider,key:keys.get(s.id)||'',timeout:s.timeout||120}))$(id).value=value||'';
    $('presets').hidden=!jev;$('questions-panel').hidden=!jev;$('system-panel').hidden=jev;$('provider-field').hidden=jev;$('timeout-field').hidden=!jev;
    $('input-label').textContent=jev?'任务与观察证据':'消息';$('input-hint').textContent=jev?'单次独立判断':'支持多轮对话';$('send').innerHTML=jev?'请求判断 <span>↗</span>':'发送 <span>↗</span>';
    $('model-name').textContent=s.config.model;$('endpoint').textContent=s.config.url;$('request-state').textContent=s.turns.length?'请求已结束':'等待输入';images();results();
  }
  function detail(label,content){return `<details class="lab-disclosure"><summary>${esc(label)}</summary><pre>${esc(typeof content==='string'?content:JSON.stringify(content,null,2))}</pre></details>`;}
  function answerHTML(result){
    const raw=result.raw_response||result;return Object.entries(raw.answers||{}).map(([id,a])=>{
      const chosen=a.type==='choice'?a.choice:a.type==='noul'?`是 · ${(Number(a.noul)*100).toFixed(1)}%`:`等级得分 ${Number(a.score).toFixed(2)}`;
      const normalized=result.answers?.[id];
      const probabilities=a.type==='noul'?{'是':a.noul,'否':1-a.noul}:a.probabilities||{};
      return `<section class="lab-answer"><h3>${esc(id)} <span>${esc(chosen)}</span></h3>${Object.entries(probabilities).map(([label,v])=>`<div class="lab-prob"><span>${esc(a.legend?.[label]||label)}</span><progress max="1" value="${Number(v)||0}"></progress><span>${(Number(v)*100).toFixed(1)}%</span></div>`).join('')}${normalized?`<small>框架采信：${esc(normalized.label)} · ${normalized.selection_policy==='maximum_probability'?'最高概率策略':'阈值与分差策略'}</small>`:''}</section>`;
    }).join('');
  }
  function results(){
    const s=current();if(!s)return;const legacy=s.legacy?(s.legacyMessages||[]).map(m=>`<article class="lab-legacy"><small>${esc(m.role)}</small><p>${esc(typeof m.content==='string'?m.content:(m.content||[]).filter(c=>c.type==='text').map(c=>c.text).join('\n'))}</p>${(Array.isArray(m.content)?m.content:[]).filter(c=>c.type==='image_url'&&/^data:image\/(png|jpeg|jpg|webp);base64,/.test(c.image_url?.url)).map(c=>`<img class="lab-result-image" src="${esc(c.image_url.url)}" alt="历史截图">`).join('')}</article>`).join(''):'';
    $('results').innerHTML=legacy+s.turns.map((t,i)=>{
      const r=t.result||{},u=r.usage||{},input=u.input_tokens??u.prompt_tokens,output=u.output_tokens??u.completion_tokens;
      return `<article class="lab-turn"><header><strong>${t.example?'样例 · ':''}${esc(t.title||'请求 '+(i+1))}</strong><span class="${t.error?'lab-failed':''}">${t.pending?'请求中…':t.error?'未完成':'已返回'}</span></header><p class="lab-turn-input">${esc(t.text)}</p>${t.images?.length?`<div class="lab-turn-images">${t.images.map((url,j)=>`<img tabindex="0" role="button" title="放大截图" src="${esc(url)}" alt="本次请求截图 ${j+1}">`).join('')}</div>`:''}${t.pending?'<div class="lab-thinking"><span class="spinner"></span>等待模型返回…</div>':`<div class="lab-turn-meta">${esc(r.model||t.model)} · 页面 ${Number(t.wall_ms/1000).toFixed(2)} s${r.latency_ms!=null?' · 服务 '+Number(r.latency_ms).toFixed(0)+' ms':''}${input!=null?' · 输入 '+input+' / 输出 '+(output??'未报告')+' token':''}</div>${t.error?`<p class="lab-error-text">${esc(t.error)}</p>`:t.mode==='jev'?answerHTML(r):`<div class="lab-text-result">${esc(r.response||'（空响应）')}</div>${r.annotation_image?`<img class="lab-result-image" src="${esc(r.annotation_image)}" alt="定位标注">`:''}`}${t.mode==='jev'?'<p class="lab-footnote">选项概率是未校准模型评分，不等于真实成功率。</p>':''}${detail('本次实际输入',t.request)}${detail('原始响应',r)}`}</article>`;
    }).join('')||'<div class="lab-empty"><svg><use href="#i-chat"/></svg><p>输入观察，查看模型如何回应</p><span>结果、耗时与原始请求会显示在这里</span></div>';
    $('results').scrollTop=$('results').scrollHeight;
  }
  function controls(disabled){
    busy=disabled;document.querySelectorAll('[data-lab-mode],#lab-new,#lab-send,#lab-upload,#lab-files,#lab-preset-buttons button,#lab-sessions button,#lab-images button,#lab-connection input,#lab-connection select').forEach(el=>el.disabled=disabled||reading);
    $('input').readOnly=$('system').readOnly=$('questions').readOnly=disabled;
  }
  async function send(example=null){
    if(busy||reading)return;capture();const s=current();if(!s)return;
    error('');let questions=null;try {
      if(!s.draft.trim()&&!s.images.length)throw Error('请填写文字或添加截图。');
      if(!s.config.model||!s.config.url)throw Error('请在连接设置中填写模型和服务地址。');
      if(s.mode==='jev'){
        questions=JSON.parse(s.questions);if(!questions||typeof questions!=='object'||Array.isArray(questions)||!Object.keys(questions).length)throw Error('questions 需要非空 JSON 对象。');
        if(!Number.isFinite(s.timeout)||s.timeout<1||s.timeout>600)throw Error('超时应在 1–600 秒之间。');
      }
    }catch(e){error(e.message);return;}
    const text=s.draft,imgs=[...s.images],mode=s.mode,config={...s.config};let payload;
    let apiKey=keys.get(s.id)||'';
    if(mode==='jev')payload={config:{model:config.model,url:config.url,api_key:apiKey,timeout:s.timeout},state:text,images:imgs,questions};
    else {
      if(!apiKey)apiKey=config.url.includes('dashscope.aliyuncs.com')?'env:DASHSCOPE_API_KEY':'EMPTY';
      const content=[...imgs.map(url=>({type:'image_url',image_url:{url}})),{type:'text',text}];
      payload={engine:{engine_type:config.provider,model:config.model,base_url:config.url},messages:[...(s.system?[{role:'system',content:s.system}]:[]),...s.messages,{role:'user',content}],generation:{temperature:0,max_new_tokens:4096},api_key:apiKey,call_type:mode};
    }
    const request=JSON.parse(JSON.stringify(payload));delete request.api_key;if(request.config)delete request.config.api_key;
    const turn={title:example?.title||'请求 '+(s.turns.length+1),example:!!example,mode,model:config.model,text,images:imgs,request,pending:true};s.turns.push(turn);s.name=example?.title||text.slice(0,24)||'截图调试';
    if(mode!=='jev'){s.draft='';s.images=[];$('input').value='';images();}
    controls(true);history();controls(true);results();const started=performance.now();
    timer=setInterval(()=>$('request-state').textContent='请求中 · '+((performance.now()-started)/1000).toFixed(1)+' s',100);
    try {
      const response=await fetch(mode==='jev'?'/jev/decide':'/debug/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const result=await response.json();turn.result=result;
      if(!response.ok||!result.ok)throw Error(typeof result.detail==='string'?result.detail:result.error||result.reason||JSON.stringify(result.detail||'请求未完成'));
      if(mode!=='jev'){s.messages.push(payload.messages.at(-1),{role:'assistant',content:result.response||''});}
    }catch(e){turn.error=e.message;}
    finally {clearInterval(timer);turn.pending=false;turn.wall_ms=performance.now()-started;controls(false);persist();history();results();$('request-state').textContent=turn.error?'请求失败':'已返回 · '+(turn.wall_ms/1000).toFixed(2)+' s';}
  }
  async function addFiles(fileList){
    if(busy||reading)return;const files=[...fileList],s=current();if(!files.length||!s)return;
    if(s.images.length+files.length>4||files.some(f=>!['image/png','image/jpeg','image/webp'].includes(f.type)||f.size>9*1024*1024)){error('最多 4 张 PNG / JPEG / WebP 截图，每张不超过 9 MiB。');return;}
    error('');capture();reading=true;controls(false);
    try {const urls=await Promise.all(files.map(f=>new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=()=>reject(Error('截图读取失败'));r.readAsDataURL(f);})));s.images.push(...urls);images();persist();$('clipboard-status').textContent=`已添加 ${files.length} 张截图 · 共 ${s.images.length} 张`;}
    catch(e){error(e.message);}finally{reading=false;controls(false);}
  }
  function sampleImages(id){
    if(!['effect','waiting'].includes(id))return [];
    return (id==='effect'?['before','after']:['after']).map(phase=>{
      const c=document.createElement('canvas');c.width=960;c.height=540;const x=c.getContext('2d');
      x.fillStyle='#f5f7fa';x.fillRect(0,0,960,540);x.fillStyle='#fff';x.fillRect(30,28,900,480);
      x.fillStyle='#203653';x.font='24px sans-serif';x.fillText(id==='effect'?'校园通知 · 搜索':'文件下载',65,90);
      x.font='16px sans-serif';x.fillStyle='#8695a7';x.fillText('合成截图 · 调试样例 · '+(phase==='before'?'动作前':'动作后'),65,475);
      if(id==='effect'){
        x.strokeStyle='#cbd6e3';x.strokeRect(65,125,680,50);x.fillStyle='#2576e6';x.fillRect(765,125,110,50);x.fillStyle='#fff';x.fillText('搜索',800,157);
        x.fillStyle='#4b5c70';x.fillText(phase==='before'?'请输入搜索词':'招生简章',85,157);
        if(phase==='after'){x.fillText('搜索结果：招生简章',65,225);['2026 年招生简章','招生报名条件与日期','报名材料清单'].forEach((t,i)=>{x.fillStyle='#2576e6';x.fillText(t,65,275+i*50);});}
      }else{
        x.fillStyle='#4b5c70';x.fillText('report.pdf · 正在下载',65,160);x.fillStyle='#e6edf6';x.fillRect(65,200,790,12);x.fillStyle='#2576e6';x.fillRect(65,200,395,12);x.fillStyle='#8695a7';x.fillText('5 MB / 10 MB',65,250);
      }
      return c.toDataURL('image/png');
    });
  }
  $('preset-buttons').innerHTML=presets.map(p=>`<button data-preset="${p.id}" title="${esc(p.description)}">${esc(p.title)} <span>↗</span></button>`).join('');
  $('preset-buttons').querySelectorAll('button').forEach(b=>b.onclick=()=>{if(busy||reading)return;const preset=presets.find(p=>p.id===b.dataset.preset);const config={...current().config},key=keys.get(activeId)||'';const s=create('jev');s.config=config;keys.set(s.id,key);s.draft=preset.state;s.images=sampleImages(preset.id);s.questions=JSON.stringify(preset.questions,null,2);render();send(preset);});
  $('new').onclick=()=>{if(!busy&&!reading)create(current()?.mode||'decision');};
  document.querySelectorAll('[data-lab-mode]').forEach(b=>b.onclick=()=>setMode(b.dataset.labMode));
  $('send').onclick=()=>send();$('upload').onclick=pasteClipboard;$('files').onchange=e=>{addFiles(e.target.files);e.target.value='';};
  // Listen on the active page: a screenshot can be pasted even when the composer
  // is not focused. Leave plain text paste (including prompt/JSON editing) alone.
  document.addEventListener('paste',e=>{
    if(!document.getElementById('view-chat').classList.contains('active'))return;
    const files=[...(e.clipboardData?.items||[])].filter(i=>i.type.startsWith('image/')).map(i=>i.getAsFile()).filter(Boolean);
    if(files.length){e.preventDefault();addFiles(files);}
  });
  async function pasteClipboard(){
    if(busy||reading)return;
    if(!navigator.clipboard?.read){error('请复制截图后按 ⌘V（Mac）或 Ctrl+V 粘贴。当前浏览器不支持按钮读取剪贴板。');return;}
    const sessionId=activeId;
    reading=true;controls(false);error('');
    let files=[];
    try{
      for(const item of await navigator.clipboard.read()){
        const type=item.types.find(t=>['image/png','image/jpeg','image/webp'].includes(t));
        if(type)files.push(await item.getType(type));
      }
      if(!files.length)error('剪贴板里没有截图。请先复制图片，再按 ⌘V 或 Ctrl+V。');
    }catch(e){error('无法读取剪贴板，请在此页面按 ⌘V（Mac）或 Ctrl+V 直接粘贴截图。');}
    finally{reading=false;controls(false);}
    if(activeId===sessionId&&files.length)await addFiles(files);
  }
  $('input').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();send();}});
  for(const id of ['input','system','questions','model','url','provider','key','timeout'])$(id).addEventListener('change',()=>{capture();persist();$('model-name').textContent=current()?.config.model||'';$('endpoint').textContent=current()?.config.url||'';});
  function zoomImage(img){
    const dialog=document.createElement('dialog');dialog.className='lab-image-dialog';
    const closeButton=document.createElement('button');closeButton.textContent='关闭 ×';
    const preview=document.createElement('img');preview.src=img.src;preview.alt=img.alt;dialog.append(closeButton,preview);document.body.append(dialog);dialog.showModal();
    const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
    if(!reduced)dialog.animate([{opacity:0,transform:'translateY(14px) scale(.96)'},{opacity:1,transform:'none'}],{duration:280,easing:'ease-out'});
    let closing=false;const close=async()=>{if(closing)return;closing=true;if(!reduced)await dialog.animate([{opacity:1,transform:'scale(1)'},{opacity:0,transform:'scale(.96)'}],{duration:180,fill:'forwards'}).finished;dialog.close();dialog.remove();img.focus({preventScroll:true});};
    closeButton.onclick=close;dialog.addEventListener('cancel',e=>{e.preventDefault();close();});dialog.onclick=e=>{if(e.target===dialog)close();};
  }
  document.getElementById('view-chat').addEventListener('click',e=>{if(e.target.matches('img'))zoomImage(e.target);});
  document.getElementById('view-chat').addEventListener('keydown',e=>{if(e.target.matches('img')&&['Enter',' '].includes(e.key)){e.preventDefault();zoomImage(e.target);}});
  // Retain runtime configuration behavior formerly owned by the separate Jev page.
  document.getElementById('judge-model').addEventListener('change',()=>{const url=document.getElementById('judge-url');if(!url.value.trim()&&field('judge-model'))url.value='http://10.44.101.62:8007/v1/systemone';});
  window.ModelLab={open:()=>{if(!loaded)load();},setMode};
})();
