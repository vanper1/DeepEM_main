(() => {
  'use strict';
  const params = new URLSearchParams(window.location.search);
  const taskId = params.get('task') || '';
  const nodeId = params.get('node') || '';
  const state = { task: null, node: null, socket: null, socketUrl: '', waterfallRows: [], lastFft: null, frames: 0, timer: null };
  const el = id => document.getElementById(id);
  const dom = Object.fromEntries([
    'taskHeading','backToCapture','detailConnectionStatus','detailEmpty','detailContent','detailTitle','detailSubtitle','detailStatus',
    'nodeProgress','nodeAttempts','nodeStarted','nodeEnded','nodeSummaryTime','nodeSummary','liveSpectrumCard','liveSpectrumStatus','liveSpectrumCanvas',
    'liveWaterfallCanvas','liveSpectrumMeta','nodeDescription','nodeCriteria','nodeDependencies','nodeTool','logCount','nodeLogs','nodeInputs','nodeOutputs',
    'codeCard','generatedCode','copyCodeBtn','nodeNote','saveNodeNoteBtn','noteSaveStatus','modelModeBadge','reasoningSummaryTitle','reasoningSummary','reasoningSummaryMeta',
    'reasoningDetails','reasoningDetailsTitle','reasoningStream','structuredStream','toast'
  ].map(id => [id, el(id)]));

  const STATUS = { planning:'规划中', awaiting_approval:'待批准', running:'执行中', paused:'已暂停', completed:'已完成', failed:'失败', cancelled:'已取消', pending:'等待', ready:'就绪', in_progress:'执行中', blocked:'待批准', skipped:'跳过', idle:'空闲' };
  const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const safeJson = value => { try { return JSON.stringify(value ?? {}, null, 2); } catch (_) { return String(value); } };
  const fmtTime = value => { if (!value) return '-'; const d = new Date(value); return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString('zh-CN', {hour12:false}); };
  const fmtFreq = value => { const n=Number(value); if (!Number.isFinite(n)) return '-'; const mhz=Math.abs(n)>100000?n/1e6:n; return `${mhz.toLocaleString('zh-CN',{maximumFractionDigits:6})} MHz`; };

  async function api(path, options={}) {
    const response = await fetch(path, { ...options, headers: { 'Content-Type':'application/json', ...(options.headers||{}) } });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.message || `请求失败 (${response.status})`);
    return data;
  }
  function showToast(message, isError=false) {
    dom.toast.textContent = message; dom.toast.className = `toast${isError?' error':''}`;
    clearTimeout(showToast.timer); showToast.timer=setTimeout(()=>dom.toast.classList.add('hidden'),2400);
  }
  function setStatus(node, value) { node.textContent=STATUS[value]||value||'-'; node.className=`status-pill ${value||'idle'}`; }
  function isCaptureNode(node) { const tool=String(node?.tool_name||node?.allowed_tool||''); return node?.executor==='collection_execution' || ['execute_spectrum_collection','execute_usrp_task_code','run_autonomous_usrp_task'].includes(tool); }
  function reasoningMode(task) { return String(task?.reasoning_mode||'fast').toLowerCase()==='deep'?'deep':'fast'; }

  function renderTask() {
    const task=state.task, node=state.node;
    if (!task || !node) return;
    dom.detailEmpty.classList.add('hidden'); dom.detailContent.classList.remove('hidden');
    dom.taskHeading.textContent = task.display_title || task.title || task.id;
    dom.backToCapture.href = `/capture-agent?task=${encodeURIComponent(task.id)}`;
    dom.detailTitle.textContent=node.title||node.id; dom.detailSubtitle.textContent=node.description||''; setStatus(dom.detailStatus,node.status);
    dom.nodeProgress.textContent=`${Number(node.progress||0)}%`; dom.nodeAttempts.textContent=String(node.attempts||0); dom.nodeStarted.textContent=fmtTime(node.started_at); dom.nodeEnded.textContent=fmtTime(node.ended_at);
    dom.nodeSummary.textContent=node.summary||'等待执行'; dom.nodeSummaryTime.textContent=fmtTime(node.updated_at); dom.nodeDescription.textContent=node.description||'-';
    dom.nodeCriteria.innerHTML=(node.success_criteria||[]).map(item=>`<div class="criteria-item">${escapeHtml(item)}</div>`).join('')||'<span class="muted">未设置</span>';
    const deps=node.dependencies||[]; dom.nodeDependencies.innerHTML=deps.length?deps.map(dep=>`<span class="chip">${escapeHtml(task.nodes?.find(n=>n.id===dep)?.title||dep)}</span>`).join(''):'<span class="chip">无前置依赖</span>';
    dom.nodeTool.textContent=node.tool_name||node.allowed_tool||(node.kind?`分析节点 · ${node.kind}`:'系统节点'); dom.nodeInputs.textContent=safeJson(node.inputs||{}); dom.nodeOutputs.textContent=safeJson(node.outputs||{});
    const logs=node.logs||[]; dom.logCount.textContent=`${logs.length} 条`; dom.nodeLogs.innerHTML=logs.length?logs.slice(-60).reverse().map(entry=>`<div class="log-entry ${escapeHtml(entry.level||'info')}"><time>${escapeHtml(fmtTime(entry.timestamp))}</time><span class="log-level">${escapeHtml(entry.level||'info')}</span><span>${escapeHtml(entry.message||'')}</span></div>`).join(''):'<span class="muted">暂无日志</span>';
    if (document.activeElement !== dom.nodeNote) dom.nodeNote.value=node.notes||'';
    if ((node.tool_name==='generate_usrp_task_code'||node.tool_name==='run_autonomous_usrp_task') && task.generated_code) { dom.codeCard.classList.remove('hidden'); dom.generatedCode.textContent=task.generated_code; } else { dom.codeCard.classList.add('hidden'); dom.generatedCode.textContent=''; }
    renderReasoning(); renderSpectrumMode();
  }

  function renderReasoning() {
    const node=state.node, mode=reasoningMode(state.task); const o=node?.outputs||{};
    dom.modelModeBadge.textContent=mode==='fast'?'快速模式':'深度思考模式'; dom.modelModeBadge.className=`capture-mode-badge ${mode}`;
    dom.reasoningSummaryTitle.textContent=mode==='fast'?'模型输出摘要':'动态思考摘要'; dom.reasoningSummary.textContent=String(o.llm_status_summary||node?.summary||'等待当前节点开始执行'); dom.reasoningSummaryMeta.textContent=o.llm_status_summary_updated_at?`更新 ${fmtTime(o.llm_status_summary_updated_at)}`:'';
    dom.reasoningDetails.classList.toggle('hidden',mode==='fast'); dom.reasoningDetailsTitle.textContent=mode==='fast'?'快速模式不展示详细思考':'查看详细思考过程'; dom.reasoningStream.textContent=mode==='fast'?'快速模式不输出详细思考过程。':String(o.llm_reasoning||'尚无详细思考过程'); dom.structuredStream.textContent=String(o.llm_live_output||'尚无输出');
  }

  function powerColor(value) { const t=Math.max(0,Math.min(1,Number(value||0)/255)), stops=[[0,[15,22,36]],[.2,[39,53,92]],[.45,[53,127,168]],[.7,[93,200,156]],[.88,[248,213,93]],[1,[239,96,67]]]; for(let i=1;i<stops.length;i++){const [p2,c2]=stops[i],[p1,c1]=stops[i-1]; if(t<=p2){const r=(t-p1)/(p2-p1||1); return c1.map((c,j)=>Math.round(c+(c2[j]-c)*r));}} return stops.at(-1)[1]; }
  function sizeCanvas(canvas,w,h){const width=Math.max(320,Math.round(canvas.clientWidth||w)),height=Math.max(60,Math.round(canvas.clientHeight||h)); if(canvas.width!==width)canvas.width=width;if(canvas.height!==height)canvas.height=height;return{width,height};}
  function setSpectrumStatus(text,mode=''){dom.liveSpectrumStatus.textContent=text;dom.liveSpectrumStatus.className=`live-spectrum-status${mode?` ${mode}`:''}`;}
  function drawPlaceholder(){[['liveSpectrumCanvas','等待当前频谱',160],['liveWaterfallCanvas','等待 USRP 实时 FFT 数据',240]].forEach(([id,label,h])=>{const canvas=dom[id],{width,height}=sizeCanvas(canvas,780,h),ctx=canvas.getContext('2d');ctx.fillStyle='#07111a';ctx.fillRect(0,0,width,height);ctx.fillStyle='#7893a3';ctx.font='12px sans-serif';ctx.fillText(label,14,24);ctx.strokeStyle='rgba(255,255,255,.08)';ctx.strokeRect(.5,.5,width-1,height-1);});}
  function renderFft(message,append=true){const fft=(Array.isArray(message.fft_data)?message.fft_data:[]).map(Number).filter(Number.isFinite);if(!fft.length)return;state.lastFft=message;if(append)state.frames++;const min=Math.min(...fft),max=Math.max(...fft),span=Math.max(max-min,1e-6),row=fft.map(v=>Math.round((v-min)/span*255));if(append){state.waterfallRows.push(row);state.waterfallRows=state.waterfallRows.slice(-110);}let canvas=dom.liveSpectrumCanvas,{width,height}=sizeCanvas(canvas,780,160),ctx=canvas.getContext('2d');ctx.fillStyle='#07111a';ctx.fillRect(0,0,width,height);ctx.strokeStyle='rgba(255,255,255,.08)';for(let i=1;i<4;i++){const y=height*i/4;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(width,y);ctx.stroke();}ctx.strokeStyle='#67e5be';ctx.lineWidth=1.8;ctx.beginPath();fft.forEach((v,i)=>{const x=i/Math.max(fft.length-1,1)*width,y=height-8-(v-min)/span*(height-18);i?ctx.lineTo(x,y):ctx.moveTo(x,y);});ctx.stroke();canvas=dom.liveWaterfallCanvas;({width,height}=sizeCanvas(canvas,780,240));ctx=canvas.getContext('2d');ctx.fillStyle='#07111a';ctx.fillRect(0,0,width,height);state.waterfallRows.forEach((r,ri)=>{const y=ri/Math.max(state.waterfallRows.length,1)*height,h=Math.ceil(height/Math.max(state.waterfallRows.length,1));r.forEach((v,ci)=>{const [rr,g,b]=powerColor(v),x=ci/Math.max(r.length,1)*width,w=Math.ceil(width/Math.max(r.length,1));ctx.fillStyle=`rgb(${rr},${g},${b})`;ctx.fillRect(x,y,w,h);});});const center=Number(message.freq||0),sr=Number(message.sample_rate||0);dom.liveSpectrumMeta.textContent=`中心 ${fmtFreq(center)} ｜ 范围 ${fmtFreq(sr?center-sr/2:center)} - ${fmtFreq(sr?center+sr/2:center)} ｜ FFT ${message.fft_size||fft.length} ｜ 已接收 ${state.frames} 帧 ｜ 功率 ${min.toFixed(2)} ~ ${max.toFixed(2)} dB`;setSpectrumStatus('实时数据','connected');}
  function latestWs(task){const events=Array.isArray(task?.events)?task.events:[];for(let i=events.length-1;i>=0;i--){if(events[i]?.data?.ws_url)return String(events[i].data.ws_url);}return '';}
  function disconnectWs(){const s=state.socket;state.socket=null;state.socketUrl='';if(s&&[WebSocket.OPEN,WebSocket.CONNECTING].includes(s.readyState)){try{s.close();}catch(_){}}}
  function connectWs(url){url=String(url||'').trim();if(!url||typeof WebSocket==='undefined')return;if(state.socket&&state.socketUrl===url&&[WebSocket.OPEN,WebSocket.CONNECTING].includes(state.socket.readyState))return;disconnectWs();state.socketUrl=url;setSpectrumStatus('连接中','connecting');try{const socket=new WebSocket(url);state.socket=socket;socket.onopen=()=>{if(state.socket!==socket)return;setSpectrumStatus('已连接','connected');};socket.onmessage=event=>{try{const msg=JSON.parse(event.data);if(msg.type==='fft')renderFft(msg);else if(msg.type==='status')setSpectrumStatus(msg.status==='RUNNING'?'采集中':String(msg.status||'已连接'),'connected');}catch(_){}};socket.onerror=()=>{if(state.socket===socket)setSpectrumStatus('连接失败','error');};socket.onclose=()=>{if(state.socket!==socket)return;state.socket=null;state.socketUrl='';setSpectrumStatus(state.lastFft?'保留最后一帧':'连接已断开',state.lastFft?'':'error');};}catch(error){setSpectrumStatus('连接失败','error');dom.liveSpectrumMeta.textContent=error.message;}}
  function renderSpectrumMode(){const visible=isCaptureNode(state.node);dom.liveSpectrumCard.classList.toggle('hidden',!visible);if(!visible){disconnectWs();return;}if(!state.lastFft)drawPlaceholder();const active=state.task?.status==='running'&&state.node?.status==='in_progress';const url=latestWs(state.task);if(active&&url)connectWs(url);else if(active)setSpectrumStatus('等待实时地址','connecting');else{disconnectWs();setSpectrumStatus(state.lastFft?'保留最后一帧':'非采集中');}}

  async function loadTask(silent=false){if(!taskId||!nodeId){dom.detailEmpty.innerHTML='<div class="node-detail-error">缺少 task 或 node 参数，无法显示节点详情。</div>';return;}try{const payload=await api(`/api/capture-agent/tasks/${encodeURIComponent(taskId)}`);state.task=payload.item;state.node=state.task.nodes?.find(item=>item.id===nodeId)||null;if(!state.node)throw new Error('任务中不存在该执行节点');renderTask();dom.detailConnectionStatus.textContent=['completed','failed','cancelled'].includes(state.task.status)?'任务已结束':'自动刷新中';if(!['completed','failed','cancelled'].includes(state.task.status)){clearTimeout(state.timer);state.timer=setTimeout(()=>loadTask(true),1500);} }catch(error){if(!silent){dom.detailEmpty.innerHTML=`<div class="node-detail-error">${escapeHtml(error.message)}</div>`;showToast(error.message,true);}clearTimeout(state.timer);}}
  async function saveNote(){if(!state.task||!state.node)return;dom.saveNodeNoteBtn.disabled=true;try{const payload=await api(`/api/capture-agent/tasks/${encodeURIComponent(state.task.id)}/nodes/${encodeURIComponent(state.node.id)}/note`,{method:'PUT',body:JSON.stringify({note:dom.nodeNote.value})});state.task=payload.item;state.node=state.task.nodes?.find(item=>item.id===nodeId)||state.node;dom.noteSaveStatus.textContent='已保存';showToast('节点备注已保存');}catch(error){showToast(error.message,true);}finally{dom.saveNodeNoteBtn.disabled=false;}}

  dom.saveNodeNoteBtn.addEventListener('click',saveNote);dom.copyCodeBtn.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(dom.generatedCode.textContent||'');showToast('代码已复制');}catch(_){showToast('复制失败',true);}});window.addEventListener('resize',()=>{if(state.lastFft)renderFft(state.lastFft,false);});window.addEventListener('beforeunload',()=>{clearTimeout(state.timer);disconnectWs();});
  loadTask();
})();
