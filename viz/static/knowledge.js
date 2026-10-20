(() => {
  'use strict';
  let drafts=[], revision='', selected=-1, loaded=false, dirty=false, generation=0, mode='edit';
  const el=id=>document.getElementById('kb-'+id);
  const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const help={all:'全部启用知识直接交给规划器，无额外模型调用。',decision:'将完整知识和任务交给决策模型，只返回选中的知识 ID。',jev:'为每条知识判断相关性；小知识库一次请求，大知识库分批。按最高概率标签筛选，不确定项保留。'};
  const status=t=>el('status').textContent=t;
  function animate(node){if(!matchMedia('(prefers-reduced-motion: reduce)').matches)node.animate([{opacity:.25,transform:'translateY(5px)'},{opacity:1,transform:'translateY(0)'}],{duration:180,easing:'ease-out'});}
  const formatButton=el('format-toggle'), formatNote=el('format-note');
  let formatOpen=false, formatAnimation=null;
  function showFormat(open,restoreFocus=false){
    formatOpen=open;
    formatButton.setAttribute('aria-expanded',String(open));
    const current=formatNote.hidden?{opacity:0,transform:'translateY(-6px) scale(.96)'}:{opacity:getComputedStyle(formatNote).opacity,transform:getComputedStyle(formatNote).transform};
    if(formatAnimation){formatAnimation.cancel();formatAnimation=null;}
    if(restoreFocus)formatButton.focus({preventScroll:true});
    formatNote.inert=!open;
    if(matchMedia('(prefers-reduced-motion: reduce)').matches){formatNote.hidden=!open;return;}
    formatNote.hidden=false;
    const animation=formatNote.animate([current,open?{opacity:1,transform:'translateY(0) scale(1)'}:{opacity:0,transform:'translateY(-6px) scale(.96)'}],{duration:open?200:160,easing:'cubic-bezier(.2,.8,.2,1)',fill:'forwards'});
    formatAnimation=animation;
    animation.onfinish=()=>{if(formatAnimation!==animation)return;formatNote.hidden=!formatOpen;animation.cancel();formatAnimation=null;};
  }
  formatButton.onclick=()=>showFormat(!formatOpen);
  el('format-close').onclick=()=>showFormat(false,true);
  document.addEventListener('pointerdown',event=>{if(formatOpen&&!formatButton.parentElement.contains(event.target))showFormat(false);});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&formatOpen){showFormat(false,true);event.preventDefault();}});
  document.addEventListener('focusin',event=>{if(formatOpen&&!formatButton.parentElement.contains(event.target))showFormat(false);});
  function title(text){return text.match(/^title:\s*([^\n]+)/m)?.[1]?.replace(/^['"]|['"]$/g,'')||text.match(/^id:\s*([^\n]+)/m)?.[1]||'未命名知识';}
  function id(text){return text.match(/^id:\s*([^\n]+)/m)?.[1]?.replace(/^['"]|['"]$/g,'')||'';}
  function collect(){if(selected>=0)drafts[selected]=el('markdown').value;}
  function changed(){dirty=true;generation++;status('有未保存的修改');el('save').textContent='保存修改';}
  function list(){
    el('list').replaceChildren();
    drafts.forEach((text,i)=>{const option=document.createElement('option');option.value=String(i);option.textContent=title(text)+( /^enabled:\s*false\s*$/m.test(text)?' · 停用':'');option.selected=i===selected;el('list').append(option);});
    el('list').disabled=!drafts.length;
  }
  el('list').onchange=()=>{collect();select(Number(el('list').value));};
  function markdown(text){
    const inline=s=>escape(s).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1 ↗</a>');
    const lines=text.split('\n'),out=[];let code=null,listType=null;
    const endList=()=>{if(listType){out.push('</'+listType+'>');listType=null;}};
    for(let i=0;i<lines.length;i++){
      const line=lines[i];if(/^```/.test(line)){endList();if(code!==null){out.push('<pre><code>'+escape(code.join('\n'))+'</code></pre>');code=null;}else code=[];continue;}if(code!==null){code.push(line);continue;}
      const li=line.match(/^\s*(?:([-*])|\d+\.)\s+(.*)/);if(li){const type=li[1]?'ul':'ol';if(type!==listType){endList();listType=type;out.push('<'+type+'>');}out.push('<li>'+inline(li[2])+'</li>');continue;}endList();
      if(/^#{1,6}\s/.test(line)){const n=line.match(/^#+/)[0].length;out.push(`<h${n}>${inline(line.replace(/^#+\s/,''))}</h${n}>`);}
      else if(/^\s*\|/.test(line)&&/^[\s|:\-]+$/.test(lines[i+1]||'')&&(lines[i+1]||'').includes('-')){const cells=l=>l.trim().replace(/^\||\|$/g,'').split('|').map(v=>inline(v.trim()));out.push('<div class="kb-table"><table><thead><tr>'+cells(line).map(c=>'<th>'+c+'</th>').join('')+'</tr></thead><tbody>');i+=2;for(;i<lines.length&&/^\s*\|/.test(lines[i]);i++)out.push('<tr>'+cells(lines[i]).map(c=>'<td>'+c+'</td>').join('')+'</tr>');i--;out.push('</tbody></table></div>');}
      else if(/^>\s?/.test(line))out.push('<blockquote>'+inline(line.replace(/^>\s?/,''))+'</blockquote>');
      else if(/^---\s*$/.test(line))out.push('<hr>');else if(line.trim())out.push('<p>'+inline(line)+'</p>');
    }endList();if(code!==null)out.push('<pre><code>'+escape(code.join('\n'))+'</code></pre>');return out.join('');
  }
  function render(){const text=drafts[selected]||'',match=text.match(/^---\s*\n([\s\S]*?)\n---[^\S\n]*\n([\s\S]*)$/);el('rendered').innerHTML=match?'<section class="kb-metadata" aria-label="知识元数据"><pre>'+escape(match[1])+'</pre></section>'+markdown(match[2]):'<p class="kb-error">请按规范填写 front matter，保存和测试时会校验。</p>'+markdown(text);}
  function select(i){selected=i;el('markdown').value=drafts[i]||'';el('empty').hidden=i>=0;el('markdown').hidden=i<0||mode!=='edit';el('rendered').hidden=i<0||mode!=='preview';el('delete').disabled=i<0;el('file-label').textContent=(id(drafts[i]||'')||'knowledge')+'.md';el('size').textContent=(drafts[i]?.length||0)+' 字符';list();if(mode==='preview')render();animate(mode==='edit'?el('markdown'):el('rendered'));}
  async function request(url,options){const r=await fetch(url,options),d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'请求失败');return d;}
  const jsonPost=(url,value,method='POST')=>request(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
  async function load(){if(dirty&&!confirm('重新加载会丢弃未保存修改，是否继续？'))return;const start=generation;try{const d=await request('/knowledge');if(start!==generation)return;drafts=d.entry_markdown;revision=d.revision;el('path').textContent=d.path;loaded=true;dirty=false;el('save').textContent='保存';select(drafts.length?0:-1);status('已加载 · 保存后用于下次规划');}catch(e){status(e.message);}}
  el('reload').onclick=load;
  el('add').onclick=()=>{collect();drafts.push(`---\nid: knowledge-${Date.now().toString(36)}\ntitle: 新知识\naliases: []\nkeywords: []\nurl: ''\nenabled: true\n---\n\n# 用途\n描述这条知识适用于哪些任务。\n\n## 操作提示\n- 在这里填写入口、业务背景和访问前提。\n`);mode='edit';tabState();select(drafts.length-1);changed();};
  el('delete').onclick=()=>{if(selected<0)return;drafts.splice(selected,1);select(drafts.length?Math.min(selected,drafts.length-1):-1);changed();};
  el('markdown').oninput=()=>{collect();changed();list();el('size').textContent=el('markdown').value.length+' 字符';};
  function tabState(){el('edit-tab').classList.toggle('active',mode==='edit');el('render-tab').classList.toggle('active',mode==='preview');}
  el('edit-tab').onclick=()=>{collect();mode='edit';tabState();select(selected);};el('render-tab').onclick=()=>{collect();mode='preview';tabState();select(selected);};
  el('save').onclick=async()=>{collect();const start=generation;el('save').disabled=true;try{const d=await jsonPost('/knowledge',{markdown_entries:drafts,revision},'PUT');revision=d.revision;if(start===generation){drafts=d.entry_markdown;dirty=false;el('save').textContent='保存';select(selected);status('已保存 · 下次规划生效');}else status('已保存提交版本；仍有新的未保存修改');el('path').textContent=d.path;}catch(e){status(e.message);}finally{el('save').disabled=false;}};
  function settings(){const value=id=>document.getElementById(id)?.value||'';return {decision:{model:value('run-model'),url:value('run-model-url'),api_key:value('run-model-apikey')},jev:{model:'vllm-sr/d3-flash',url:value('run-knowledge-jev-url')}};}
  function methodInfo(){el('method').title=help[el('method').value];}
  el('method').onchange=methodInfo;
  el('preview').onclick=async()=>{
    collect();if(!el('task').value.trim()){el('task').focus();return;}const start=performance.now(),version=generation,method=el('method').value;
    el('preview').disabled=true;el('results').innerHTML='<div class="kb-testing"><span class="spinner"></span>正在筛选 <span id="kb-clock">0.0 秒</span></div>';
    const timer=setInterval(()=>{const clock=el('clock');if(clock)clock.textContent=((performance.now()-start)/1000).toFixed(1)+' 秒';},100);
    try{const d=await jsonPost('/knowledge/preview',{task:el('task').value,markdown_entries:[...drafts],method,settings:settings()});
      const wall=performance.now()-start;const result=el('results');result.replaceChildren();const line=document.createElement('div');line.className='kb-result-summary';line.textContent=`${d.ok?'完成':'失败'} · ${wall<1000?wall.toFixed(1)+' ms':(wall/1000).toFixed(3)+' 秒'} · ${d.entries?.length||0} / ${d.candidate_count??'—'} 条`;result.append(line);
      const timing=document.createElement('p');timing.className='kb-subtle';timing.textContent='页面总等待（含网络） · 服务处理 '+Number(d.latency_ms||0).toFixed(2)+' ms';result.append(timing);
      if(d.calls?.length){const info=document.createElement('p');info.className='kb-subtle';const usages=d.calls.map(c=>c.usage||c.response?.usage||{});const total=(a,b)=>usages.every(u=>Number.isFinite(u[a]??u[b]))?usages.reduce((s,u)=>s+(u[a]??u[b]),0):'未完整上报';info.textContent=d.calls.length+' 次模型请求 · 输入 '+total('prompt_tokens','input_tokens')+' / 输出 '+total('completion_tokens','output_tokens')+' token';result.append(info);}
      const note=document.createElement('p');note.className='kb-subtle';note.textContent=(d.reason||d.message||d.error||'')+(version!==generation?'（草稿已修改，本结果对应点击测试时的版本。）':'');if(method!=='all'||!d.ok||version!==generation)result.append(note);
      if(d.ok&&!d.entries.length){const empty=document.createElement('p');empty.textContent='没有选入知识。请检查条目是否启用，或修改任务后再测试。';result.append(empty);}
      for(const entry of d.entries||[]){const row=document.createElement('details');row.className='kb-result-entry';const summary=document.createElement('summary');summary.textContent=entry.title+' · '+entry.id;const content=document.createElement('div');content.className='kb-markdown';content.innerHTML=markdown(entry.content);if(entry.url){const a=document.createElement('a');a.textContent=entry.url;a.href=entry.url;a.target='_blank';a.rel='noopener noreferrer';content.prepend(a);}row.append(summary,content);result.append(row);}
      const detail=document.createElement('details');detail.className='kb-raw';const summary=document.createElement('summary');summary.textContent='实际输入 / 输出与用量';const pre=document.createElement('pre');pre.textContent=JSON.stringify(d,null,2);detail.append(summary,pre);result.append(detail);animate(result);
    }catch(e){el('results').textContent=e.message;}finally{clearInterval(timer);el('preview').disabled=false;}
  };
  const runMethod=document.getElementById('run-knowledge-method'),runURL=document.getElementById('run-knowledge-jev-url');
  const updateRun=()=>{document.getElementById('run-knowledge-note').textContent=help[runMethod.value];document.getElementById('run-knowledge-jev-row').hidden=runMethod.value!=='jev';try{localStorage.setItem('knowledge-selection-v2',JSON.stringify({method:runMethod.value,url:runURL.value}));}catch{}methodInfo();};
  try{const saved=JSON.parse(localStorage.getItem('knowledge-selection-v2')||'null');if(saved&&help[saved.method])runMethod.value=saved.method;if(saved?.url)runURL.value=saved.url;}catch{}
  runMethod.onchange=updateRun;runURL.onchange=updateRun;updateRun();
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  window.KnowledgeStudio={open:()=>{if(!loaded)load();methodInfo();}};
})();
