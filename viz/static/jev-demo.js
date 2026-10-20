/* Presentation-only scenario runner. No agent actions or training writes. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const labels = {passed:'通过',progress:'有进展 · 尚未完成',failed:'未通过',pending:'处理中 · 等待',unknown:'证据不足'};
  let scenes = [], results = [], busy = false, mode = '', controller = null, stopped = false, timer = null;
  function text(id, value) { const node = $(id); if (node) node.textContent = value; }
  function elem(tag, value, cls) { const e = document.createElement(tag); if (value != null) e.textContent = value; if (cls) e.className = cls; return e; }
  function flow(stage) { document.querySelectorAll('.jd-flow [data-stage]').forEach(e => e.classList.toggle('active', e.dataset.stage === stage)); }
  function error(message) { text('jd-error', message); $('jd-error').hidden = !message; }
  const pause = ms => new Promise((resolve,reject) => {
    const signal=controller?.signal;
    const abort=()=>{clearTimeout(id);reject(new DOMException('Stopped','AbortError'));};
    const id=setTimeout(()=>{signal?.removeEventListener('abort',abort);resolve();},ms);
    if(signal?.aborted)abort();else signal?.addEventListener('abort',abort,{once:true});
  });
  function rect(ctx,x,y,w,h,color,r=12) { ctx.fillStyle=color; ctx.beginPath(); ctx.roundRect(x,y,w,h,r); ctx.fill(); }
  function line(ctx, value, x, y, size=24, color='#263a55', weight=400) { ctx.fillStyle=color; ctx.font=`${weight} ${size}px Arial, sans-serif`; ctx.fillText(value,x,y); }
  function wrap(ctx,value,x,y,max,size=28,color='#213755',weight=500) {
    let row=''; for (const word of value.split(' ')) { ctx.font=`${weight} ${size}px Arial, sans-serif`; if(ctx.measureText(row+word).width>max&&row){line(ctx,row,x,y,size,color,weight);row='';y+=size*1.5;} row+=word+' '; } line(ctx,row,x,y,size,color,weight);return y;
  }
  function draw(id, scene) {
    const canvas=$(id), c=canvas.getContext('2d'); c.clearRect(0,0,1280,720);
    rect(c,0,0,1280,720,'#f4f7fb',0);rect(c,0,0,1280,70,'#e2e8f1',0);
    [0,1,2].forEach(i=>{c.beginPath();c.arc(29+i*24,33,6,0,Math.PI*2);c.fillStyle=['#df9296','#e2c687','#8dc6aa'][i];c.fill();});
    rect(c,135,17,1000,37,'#fff',9);
    let url='demo.invalid/scholar';if(scene==='wrong_pdf')url='demo.invalid/pdf/survey';if(scene.startsWith('paper_'))url='arxiv.org/abs/1706.03762';if(scene.startsWith('download_'))url='demo-workspace / Downloads';
    line(c,url,159,42,17,'#60748e');line(c,'DEMO',1160,42,16,'#4c719d',700);
    if(scene==='load_pending') {
      rect(c,0,70,1280,617,'#111827',0);line(c,'Loading search results…',480,345,27,'#d9e5ea');
      c.beginPath();c.arc(640,420,29,-Math.PI*.7,Math.PI*.6);c.strokeStyle='#72c9ad';c.lineWidth=7;c.stroke();
    } else if(scene==='load_ready') {
      line(c,'Search results for Attention Is All You Need',80,154,34,'#294660',600);rect(c,61,211,1158,195,'#fff',12);
      wrap(c,'Attention Is All You Need',88,270,870,30,'#326a9b',600);
      line(c,'arxiv.org  ·  View paper',88,358,22,'#667e93');
    } else if(scene.startsWith('search_')) {
      line(c,'Scholar Search',423,242,48,'#386999',500);line(c,'Find scholarly literature',482,287,21,'#8494a9');
      rect(c,240,333,800,70,'#dce4ed',10);rect(c,242,335,796,66,'#fff',9);
      if(scene==='search_typed')line(c,'Attention Is All You Need',271,379,28,'#2b3d56');else line(c,'Search papers...',271,379,26,'#a3afbe');
      rect(c,924,344,101,49,'#4677ab',8);line(c,'Search',939,376,21,'#fff');line(c,'Advanced search  /  About  /  Privacy',433,461,19,'#8b98aa');
    } else if(scene==='results') {
      line(c,'Search results for Attention Is All You Need',80,133,29,'#264b72',600);
      rect(c,62,174,1153,194,'#fff',12);wrap(c,'A Survey of Attention Mechanisms',86,216,840,28,'#325f9c');
      line(c,'A survey of transformer architectures.',86,302,21,'#73839a');line(c,'[PDF] related survey',946,219,20,'#326fa8');
      rect(c,62,393,1153,140,'#fff',12);line(c,'Attention Is All You Need',86,440,28,'#325f9c');line(c,'Vaswani et al. · arXiv:1706.03762',86,490,21,'#73839a');
    } else if(scene==='wrong_pdf') {
      rect(c,0,70,1280,50,'#35465e',0);line(c,'related-survey.pdf',28,103,19,'#fff');line(c,'1 / 28',621,103,18,'#d9e2ef');
      rect(c,164,139,952,545,'#fff',0);wrap(c,'A Survey of Attention Mechanisms',224,221,833,37,'#1b2c43',600);
      line(c,'Abstract',228,377,25,'#31445e',600);wrap(c,'This survey discusses earlier work including Attention Is All You Need.',228,423,823,24,'#67778a');
      line(c,'1. Introduction',228,566,25,'#31445e',600);line(c,'We review existing approaches to attention models.',228,612,22,'#67778a');
    } else if(scene.startsWith('paper_')) {
      rect(c,0,70,1280,73,'#9a3a40',0);line(c,'arXiv  /  Robotics',52,119,29,'#fff',600);
      line(c,'arXiv:1706.03762',60,198,22,'#8b4e52');wrap(c,'Attention Is All You Need',60,255,850,36,'#213650',600);
      line(c,'Abstract',60,421,24,'#3b526b',600);wrap(c,'The Transformer is based solely on attention mechanisms.',60,468,795,24,'#728399');
      rect(c,970,195,260,177,'#e7edf7',10);line(c,'Access Paper',996,234,23,'#3c5677',600);rect(c,994,263,210,62,'#4379b2',8);line(c,'View PDF',1033,304,25,'#fff');
      if(scene==='paper_modal'){rect(c,0,143,1280,544,'#15273ecc',0);rect(c,288,218,705,344,'#fff',15);line(c,'Subscribe to the newsletter',329,294,35,'#273d59',600);line(c,'Get updates on new research.',330,350,24,'#8191a7');rect(c,330,406,530,61,'#e7edf5',8);line(c,'Email address',350,446,23,'#95a3b5');line(c,'×',941,262,35,'#73839a');}
    } else {
      line(c,'Downloads',70,143,38,'#263e5c',600);line(c,'demo-workspace',72,186,20,'#8394ab');rect(c,61,223,1158,224,'#fff',14);
      rect(c,92,259,88,105,'#e8eff9',8);line(c,'PDF',109,320,27,'#4774a4',600);
      line(c,scene==='download_done'?'data.pdf':'1706.03762v3.pdf',208,288,31,'#263d58',600);
      line(c,scene==='download_done'?'Completed  ·  4,156,037 bytes':'Downloading  ·  transport still active',210,336,23,'#8391a5');
      rect(c,211,367,937,12,'#e7edf5',5);rect(c,211,367,scene==='download_done'?937:405,12,scene==='download_done'?'#62b598':'#739fd0',5);
      line(c,scene==='download_done'?'Artifact event: completed = true':'Artifact event: status = in_progress',84,520,25,'#526a87');
      line(c,'Synthetic event record for demonstration. No file was downloaded.',84,571,21,'#9b8667');
    }
    rect(c,0,687,1280,33,'#e9eef5',0);line(c,'SYNTHETIC DEMO / NO REAL BROWSER OR FILE OPERATIONS',23,709,13,'#8091a8');
  }
  function sceneButtons(active) {
    const box=$('jd-scenes');box.replaceChildren();scenes.forEach((s,i)=>{const button=elem('button');button.type='button';button.classList.toggle('active',s.id===active);button.classList.toggle('done',results.some(r=>r.scenario_id===s.id));button.append(elem('span',String(i+1).padStart(2,'0')));const title=elem('div',s.title);title.append(elem('small',s.role==='jev'?'JEV · 语义核查':'FRAMEWORK · 系统证据'));button.append(title);button.addEventListener('click',()=>{if(busy)return;showScene(s);const result=results.find(r=>r.scenario_id===s.id);if(result)render(s,result);});box.append(button);});
  }
  function showScene(s) {
    sceneButtons(s.id);text('jd-kicker',s.kicker);text('jd-title',s.title);text('jd-narration',s.narration);text('jd-candidate',s.candidate);text('jd-takeaway',s.takeaway);
    draw('jd-before',s.before);draw('jd-after',s.after);$('jd-probs').replaceChildren();text('jd-outcome','正在收集证据');text('jd-reason',s.expected);text('jd-next-title','等待核查结果');text('jd-next-detail','系统核查在前，语义判断按需调用。');text('jd-raw','尚无结果');text('jd-result-source',s.role==='code'?'SYSTEM EVIDENCE':'JEV VERDICT');flow('observe');
  }
  function render(s,r) {
    text('jd-outcome',labels[r.outcome]||'证据不足');text('jd-reason',r.report?.reason||'');text('jd-raw',JSON.stringify(r,null,2));
    text('jd-result-source',r.source==='scripted_replay'?'SCRIPTED REPLAY · 非模型实测':r.source==='code_on_fixture'?'CODE · 合成系统证据':'LIVE JEV · '+(r.model||'实际模型未返回'));
    const raw=r.report?.judgment?.raw_response?.answers?.step||r.report?.judgment?.raw_response?.answers?.readiness, probs=$('jd-probs');probs.replaceChildren();
    Object.entries(raw?.probabilities||{}).sort((a,b)=>b[1]-a[1]).forEach(([key,value],i)=>{const row=elem('div',null,'jd-prob-row'+(i===0?' top':''));row.append(elem('span',key));const track=elem('div',null,'jd-prob-track'),fill=elem('div',null,'jd-prob-fill');fill.style.width=Math.max(0,Math.min(100,value*100))+'%';track.append(fill);row.append(track,elem('span',(value*100).toFixed(1)+'%'));probs.append(row);});
    text('jd-score-note',r.source==='scripted_replay'?'此结论由剧本预置，没有模型概率，也没有模型实测延迟。':r.source==='code_on_fixture'?'检查预置事件，不调用模型；真实运行须使用实际浏览器与文件证据。':'选项分数未经校准。演示采信阈值 0.80 / 分差 0.15；低于阈值保留 unknown。');
    const next={passed:['推进子任务','本幕要求已通过核查；演示控制器展示下一阶段。'],progress:['继续当前子任务','输入已有效，下一步提交搜索；不重复输入，不提前宣告搜索完成。'],failed:['纠正当前路径','当前结果未满足要求。演示控制器停止沿用该结果，提示回到浏览器继续检索。'],pending:['交给框架等待','传输事件仍为 in_progress；不调用 Jev，不重复触发下载。'],unknown:['保留不确定','不把 unknown 当成成功；补充观察或恢复检索。']};
    const action=next[r.outcome]||next.unknown;text('jd-next-title',action[0]);text('jd-next-detail',action[1]);
    if(r.source==='live_jev'&&r.outcome!==s.replay)text('jd-takeaway','实际返回与剧本预期不同，已原样保留。该结果需要进一步分析，不能用演示预期替换模型结果。');
    if(r.source==='code_on_fixture'&&s.id==='artifact')text('jd-next-detail','合成记录的完成标记、字节数与文件名满足样例条件。这里只演示验收分工，不宣称完成真实下载。');
    flow('route');sceneButtons(s.id);metrics();
  }
  function metrics() {
    const calls=results.filter(r=>r.model_called), successful=calls.filter(r=>r.report?.judgment?.status==='answered');
    text('jd-calls',calls.length);text('jd-mean',successful.length?(successful.reduce((n,r)=>n+r.model_latency_ms,0)/successful.length).toFixed(0)+' ms':'—');text('jd-code-count',results.filter(r=>r.source==='code_on_fixture').length+' / 2');
  }
  function summary() {
    $('jd-summary').hidden=false;const calls=results.filter(r=>r.model_called&&r.report?.judgment?.status==='answered');
    text('jd-summary-text',mode==='replay'?`已演示 ${results.length} / ${scenes.length} 幕。离线剧本未调用 Jev，不能用于证明速度或准确率。`:`已展示 ${results.length} / ${scenes.length} 幕，收到 ${calls.length} 次有效 Jev 返回。下面仅展示本场实测请求时间，包含网络与服务排队。`);
    const chart=$('jd-chart');chart.replaceChildren();const max=Math.max(1,...calls.map(r=>r.model_latency_ms));calls.forEach(r=>{const row=elem('div',null,'jd-chart-row');row.append(elem('span',scenes.find(s=>s.id===r.scenario_id).title));const track=elem('div',null,'jd-chart-track'),bar=elem('span');bar.style.width=(r.model_latency_ms/max*100)+'%';track.append(bar);row.append(track,elem('span',r.model_latency_ms.toFixed(0)+' ms'));chart.append(row);});
  }
  const loaded=fetch('/jev/demo/scenarios').then(r=>{if(!r.ok)throw new Error('演示场景加载失败');return r.json();}).then(data=>{scenes=data.scenarios;sceneButtons();draw('jd-before','results');draw('jd-after','wrong_pdf');}).catch(e=>{error(e.message);});
  async function start(selectedMode) {
    if(busy)return;busy=true;stopped=false;mode=selectedMode;results=[];controller=new AbortController();error('');$('jd-summary').hidden=true;
    $('view-jev-demo').classList.add('jd-presenting');
    $('jd-live').disabled=$('jd-replay').disabled=true;$('jd-stop').disabled=false;$('jd-export').disabled=true;
    text('jd-mode',mode==='live'?'LIVE · 真实 Jev 请求':'REPLAY · 离线剧本 / 非实测');$('jd-mode').classList.toggle('replay',mode==='replay');metrics();
    text('jd-clock','—');text('jd-clock-note',mode==='replay'?'剧本不提供模型实测耗时':'请求时间包含网络与服务排队');
    try {
      await loaded;
      if(!scenes.length)throw new Error('场景尚未加载，请刷新页面重试');
      const timeout=Number($('jd-timeout').value);if(mode==='live'&&(!Number.isFinite(timeout)||timeout<1||timeout>120))throw new Error('请求超时应在 1–120 秒之间');
    const config={url:$('jd-url').value.trim(),model:$('jd-model').value,api_key:$('jd-key').value,timeout,
                  review_mode:'exclusive',threshold:0.8,margin:0.15};
      for(const s of scenes) {
        if(stopped)break;showScene(s);await pause(200);if(stopped)break;flow('code');
        const actual=mode==='live'&&s.role==='jev';if(actual){flow('judge');$('jd-scan').hidden=false;text('jd-outcome','Jev 正在核查…');}
        else text('jd-outcome',s.role==='code'?'代码正在核查…':'离线剧本回放…');
        const started=performance.now();if(actual){timer=setInterval(()=>text('jd-clock',((performance.now()-started)/1000).toFixed(1)+' s'),80);}
        const payload={mode,scenario_id:s.id};if(actual){payload.config=config;payload.images=[$('jd-before').toDataURL('image/png'),$('jd-after').toDataURL('image/png')];}
        const response=await fetch('/jev/demo/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:controller.signal});
        const data=await response.json();clearInterval(timer);timer=null;$('jd-scan').hidden=true;
        if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'演示请求失败');
        if(stopped)break;
        data.observed_at=new Date().toISOString();data.page_request_ms=actual?Math.round(performance.now()-started):null;results.push(data);render(s,data);$('jd-export').disabled=false;
        if(actual)text('jd-clock',(data.page_request_ms/1000).toFixed(2)+' s');
        if(actual&&data.report?.judgment?.status!=='answered')throw new Error('Jev 请求未成功：'+(data.report?.judgment?.error_type||data.report?.judgment?.reason||'服务不可用')+'。已保留实际错误，未切换成模拟结果。可点击“离线剧本演示”继续汇报。');
        await pause(Number($('jd-pace').value));
      }
      if(!stopped)summary();
    } catch(e) { if(e.name!=='AbortError')error(e.message); }
    finally {clearInterval(timer);timer=null;$('jd-scan').hidden=true;busy=false;$('jd-live').disabled=$('jd-replay').disabled=false;$('jd-stop').disabled=true;if(stopped)text('jd-outcome','已停止演示');}
  }
  $('jd-live').addEventListener('click',()=>start('live'));$('jd-replay').addEventListener('click',()=>start('replay'));
  $('jd-stop').addEventListener('click',()=>{stopped=true;controller?.abort();text('jd-clock-note','停止后不发起下一幕；已发出的服务请求可能仍在结束中。');});
  $('jd-model').addEventListener('change',()=>{$('jd-url').value='http://10.44.101.62:8007/v1/systemone';});
  $('jd-fullscreen').addEventListener('click',async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await $('view-jev-demo').requestFullscreen();}catch{error('浏览器未允许全屏，可使用 F11 或浏览器全屏菜单。');}});
  $('jd-export').addEventListener('click',()=>{const record={schema:'jev_presentation_v1',mode,synthetic_observations:true,trainable:false,created_at:new Date().toISOString(),notice:'合成演示材料。replay 不是模型实测；live 只表示核查实际调用，不表示执行过真实浏览器任务。',results};const url=URL.createObjectURL(new Blob([JSON.stringify(record,null,2)],{type:'application/json'}));const link=elem('a');link.href=url;link.download='jev-demo-'+mode+'-'+Date.now()+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  window.JevDemo={start};
})();
