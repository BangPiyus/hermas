/* No dynamic innerHTML: all assistant/tool content is inserted with textContent. */
const $ = (id) => document.getElementById(id);
const state = {csrf:'', sid:null, busy:false, lastTaskFingerprint:'', taskActive:false, taskBlocking:false, actionRequired:false, role:'admin'};
const icon = () => { try { window.feather?.replace(); } catch(_) {} };
function node(tag, cls='', text=null){ const el=document.createElement(tag); if(cls)el.className=cls; if(text!==null)el.textContent=String(text); return el; }
function toast(text){ const e=$('toast'); e.textContent=text; e.classList.remove('opacity-0','translate-y-2'); setTimeout(()=>e.classList.add('opacity-0','translate-y-2'),3500); }
async function api(url, opts={}){
  const res=await fetch(url,{credentials:'same-origin',headers:{'Content-Type':'application/json',...(state.csrf?{'X-Hermas-CSRF':state.csrf}:{})},...opts});
  if(res.status===401 && url!=='/api/login'){showLogin();throw Error('Sesi berakhir. Silakan login ulang.');}
  const data=await res.json().catch(()=>({detail:'Gagal membaca respons'}));
  if(!res.ok)throw Error(typeof data.detail==='string'?data.detail:`HTTP ${res.status}`);
  return data;
}
function showLogin(){ $('loginView').classList.remove('hidden'); $('appView').classList.add('hidden');$('overlay').classList.add('hidden');$('sessionList').replaceChildren();$('chatMessages').replaceChildren();$('approvalList').replaceChildren();state.sid=null;state.csrf='';state.role='admin';state.taskActive=false;state.taskBlocking=false;state.actionRequired=false;state.retryKey=null;state.retryMessage=null; }
function showApp(){ $('loginView').classList.add('hidden');$('appView').classList.remove('hidden');$('appView').classList.add('flex');icon(); }
function setBusy(on){state.busy=on;$('sendButton').disabled=on||state.taskBlocking||state.actionRequired;$('chatInput').disabled=on||state.taskBlocking||state.actionRequired;}
function closeNav(){ $('sidebar').classList.add('-translate-x-full'); }
async function refreshStatus(){
 const me=await api('/api/me');state.csrf=me.csrf;state.role=me.role||'admin';
 $('modeLabel').textContent=me.mode==='demo'?'DEMO':'AI / '+me.model;
 $('modePill').textContent=me.mode==='demo'?'Mode demo':'AI aktif';
 $('modePill').className='hidden sm:inline-flex ml-2 rounded-full text-xs font-semibold px-3 py-1.5 '+(me.mode==='demo'?'bg-amber-50 text-amber-700':'bg-emerald-50 text-emerald-700');
 const isAdmin=state.role==='admin';
 for(const id of ['newSession','gitButton','vpsButton','workflowButton','schedulerButton','integrationsButton','browserButton','memoryButton','skillButton','auditButton','tasksButton','metricsButton','snapshotsButton','accountsButton']){
  $(id).classList.toggle('hidden',!isAdmin);
 }
 $('chatForm').classList.toggle('hidden',!isAdmin);
 $('chatMessages').classList.toggle('hidden',!isAdmin);
 $('modeLabel').textContent=me.mode==='demo'?'DEMO':'AI / '+me.model;
 $('tokenLabel').textContent=me.usage.total_tokens.toLocaleString('id-ID')+' / '+me.daily_token_budget.toLocaleString('id-ID');
 $('tokenBar').style.width=Math.min(100,me.usage.total_tokens/me.daily_token_budget*100)+'%';
}
async function listSessions(){
 const sessions=await api('/api/sessions');const parent=$('sessionList');parent.replaceChildren();
 sessions.forEach(s=>{
  const b=node('button','w-full text-left px-3 py-3 flex gap-2 items-start rounded-xl text-xs transition '+(state.sid===s.id?'bg-indigo-600/25 text-white':'text-slate-400 hover:bg-white/10 hover:text-white'));
  const svg=node('i','w-4 h-4 shrink-0 mt-0.5');svg.setAttribute('data-feather','message-square');b.append(svg,node('span','line-clamp-2 break-words',s.title));
  b.onclick=()=>loadSession(s.id);parent.append(b);
 });icon();return sessions;
}
async function createSession(){
 if(state.busy)return;const session=await api('/api/sessions',{method:'POST',body:'{}'});state.sid=session.id;await listSessions();await loadSession(session.id);
}
function renderMessage(message){
 const row=node('div','max-w-4xl mx-auto flex '+(message.role==='user'?'justify-end':'justify-start'));
 const container=node('div',message.role==='user'?'max-w-[90%] bg-indigo-600 text-white rounded-2xl rounded-tr-sm px-4 py-3 text-sm shadow-sm':'max-w-[98%] w-full');
 if(message.role==='assistant'){
   const h=node('div','text-xs text-indigo-700 font-semibold mb-2 flex items-center gap-2','HERMAS');container.append(h);
 } else if(message.role==='tool'){
   const d=node('details','bg-slate-100 rounded-xl border border-slate-200 px-3 py-2 text-xs');
   const sum=node('summary','cursor-pointer text-slate-600 font-medium','Hasil tool · klik untuk melihat');
   const body=node('pre','message-pre whitespace-pre-wrap mt-3 text-slate-700 text-xs overflow-x-auto',message.content||'');
   d.append(sum,body);container.append(d);row.append(container);return row;
 }
 if(message.content){container.append(node('div','message-pre leading-7 whitespace-pre-wrap break-words',message.content));}
 if(message.tool_calls?.length){container.append(node('div','text-[11px] text-amber-700 mt-2 bg-amber-50 px-3 py-2 rounded-lg',`Tool diminta: ${message.tool_calls.map(t=>t.function?.name||'?').join(', ')}`));}
 row.append(container);return row;
}
function renderApprovals(pending){
 const parent=$('approvalList');parent.replaceChildren();$('approvalCount').textContent=pending.length;
 if(!pending.length){parent.append(node('p','text-slate-400 text-sm text-center py-8','Tidak ada tindakan menunggu.'));return;}
 pending.forEach(a=>{
  const wrap=node('article','border border-amber-200 bg-amber-50/70 rounded-2xl p-4 space-y-3');
  wrap.append(node('div','text-xs font-bold text-amber-800 uppercase tracking-wide',a.tool_name.replaceAll('_',' ')));
  if(a.risk)wrap.append(node('p','text-xs font-semibold text-amber-800',`Risiko ${a.risk.level.toUpperCase()} · ${a.risk.reason}`));
  if(a.state==='unknown')wrap.append(node('p','text-xs text-red-700 font-semibold','Hasil eksekusi tidak diketahui setelah restart. Periksa workspace secara manual sebelum lanjut.'));
  wrap.append(node('p','text-xs text-slate-500',`Permintaan ${new Date(a.created_at).toLocaleString('id-ID')}`));
  const pre=node('pre','bg-slate-950 text-slate-100 rounded-xl p-3 text-[11px] leading-5 whitespace-pre-wrap break-words max-h-64 overflow-auto scroll-slim',a.preview);
  wrap.append(pre);
  const actions=node('div','grid grid-cols-2 gap-2');
  const deny=node('button','border border-slate-300 bg-white rounded-lg py-2.5 text-xs font-semibold hover:bg-slate-50','Tolak');
  const accept=node('button','bg-indigo-600 text-white rounded-lg py-2.5 text-xs font-semibold hover:bg-indigo-700','Setujui');
  if(a.state==='unknown'){
    const acknowledge=node('button','border border-red-300 bg-white rounded-lg py-2.5 text-xs font-semibold','Sudah saya periksa manual');
    acknowledge.onclick=()=>acknowledgeUnknown(a.id);wrap.append(acknowledge);
  }else{
    deny.onclick=()=>decide(a.id,false);accept.onclick=()=>decide(a.id,true);
    actions.append(deny,accept);wrap.append(actions);
  }
  parent.append(wrap);
 });
}
async function loadSession(sid){
 state.sid=sid;closeNav();try{
  const data=await api('/api/sessions/'+encodeURIComponent(sid));const parent=$('chatMessages');parent.replaceChildren();
  if(!data.messages.length){
   const box=node('div','max-w-2xl mx-auto mt-10 text-center');
   box.append(node('h3','text-lg font-bold','Percakapan baru'),node('p','text-sm text-slate-500 mt-2','Coba: “Periksa struktur file proyek ini dan jelaskan arsitekturnya.”'));
   parent.append(box);
  }else data.messages.forEach(m=>parent.append(renderMessage(m)));
  renderApprovals(data.pending);parent.scrollTop=parent.scrollHeight;
  const latest=(data.tasks||[])[0];
  const status=latest?.status||'tidak ada';$('currentTaskStatus').textContent=status.replaceAll('_',' ');
  state.taskActive=['queued','running','approval_required'].includes(status);
  state.taskBlocking=['queued','running','approval_required','interrupted'].includes(status);
  state.actionRequired=!!data.pending.length;
  state.lastTaskFingerprint=JSON.stringify([(data.messages||[]).length,(data.pending||[]).map(a=>a.id+':'+a.state),status]);
  $('chatInput').disabled=state.actionRequired||state.taskBlocking;
  $('sendButton').disabled=$('chatInput').disabled;
  $('chatInput').placeholder=$('chatInput').disabled?'Selesaikan tugas aktif terlebih dahulu':'Tanyakan kode, audit proyek, atau ajukan perubahan...';
  await listSessions(); icon();
 }catch(err){toast(err.message);}
}
async function decide(id,approved){
 if(state.busy)return;setBusy(true);
 try{const data=await api('/api/approvals/'+encodeURIComponent(id)+'/decision',{method:'POST',body:JSON.stringify({approved})});await loadSession(state.sid);await refreshStatus();toast(data.status==='approval_required'?'Masih ada tindakan menunggu':data.status==='queued'?'Tugas kembali ke antrean':'Review selesai');}
 catch(err){toast(err.message);}finally{setBusy(false);}
}
async function sendMessage(e){
 e.preventDefault();if(state.busy||!state.sid)return;
 const message=$('chatInput').value.trim();if(!message)return;
 setBusy(true);$('chatInput').value='';
 try{
  // Keep the same idempotency key on a network retry of the same message.
  if(state.retryMessage!==message){state.retryMessage=message;state.retryKey='web.'+Date.now()+'.'+Math.random().toString(36).slice(2);}
  await api('/api/sessions/'+encodeURIComponent(state.sid)+'/tasks',{method:'POST',body:JSON.stringify({message,idempotency_key:state.retryKey,kind:$('multiAgentMode').checked?'orchestrate':'chat'})});
  state.retryMessage=null;state.retryKey=null;
  await loadSession(state.sid);await refreshStatus();toast('Tugas telah masuk antrean.');
 }
 catch(err){$('chatInput').value=message;toast(err.message);}finally{setBusy(false);}
}
async function acknowledgeUnknown(id){
 if(state.busy)return;setBusy(true);
 try{await api('/api/approvals/'+encodeURIComponent(id)+'/acknowledge',{method:'POST',body:'{}'});await loadSession(state.sid);toast('Status ketidakpastian sudah diakui; pastikan hasil telah diverifikasi.');}
 catch(err){toast(err.message);}finally{setBusy(false);}
}
async function taskAction(id,action){
 try{await api('/api/tasks/'+encodeURIComponent(id)+'/'+action,{method:'POST',body:'{}'});await showTasks();if(state.sid)await loadSession(state.sid);toast(action==='cancel'?'Pembatalan diproses':'Tugas dijadwalkan ulang');}
 catch(err){toast(err.message);}
}
async function showTasks(){
 $('modalTitle').textContent='Antrean & riwayat tugas';$('modalContent').replaceChildren();$('overlay').classList.remove('hidden');$('overlay').classList.add('flex');
 try{
  const jobs=await api('/api/tasks');
  if(!jobs.length)$('modalContent').append(node('p','text-slate-500','Belum ada tugas.'));
  for(const j of jobs){
   const card=node('div','border border-slate-200 bg-slate-50 rounded-xl p-4 space-y-2');
   card.append(node('p','font-semibold text-slate-900',`${j.kind==='orchestrate'?'Multi-Agent':'Chat'} · ${j.id.slice(0,8)} · ${j.status.replaceAll('_',' ')}`));
   if(j.kind==='orchestrate'){
     const detailButton=node('button','border border-indigo-200 bg-white text-indigo-700 rounded-lg px-3 py-2 text-xs font-semibold','Lihat tahapan');
     detailButton.onclick=()=>showStages(j.id);card.append(detailButton);
   }
   card.append(node('p','text-xs text-slate-500',`Percobaan ${j.attempts}/3 · ${j.created_at}`));
   if(j.last_error)card.append(node('p','text-xs text-red-700',j.last_error));
   if(j.elapsed_ms!=null)card.append(node('p','text-xs text-slate-500',`Durasi ${j.elapsed_ms} ms`));
   if(['queued','running','approval_required','interrupted','failed'].includes(j.status)){
    const action=j.status==='failed'||j.status==='interrupted'?'resume':'cancel';
    const button=node('button','rounded-lg bg-indigo-600 text-white px-3 py-2 text-xs font-semibold',action==='resume'?'Lanjutkan manual':'Batalkan');
    button.onclick=()=>taskAction(j.id,action);card.append(button);
   }
   $('modalContent').append(card);
  }
 }catch(err){toast(err.message);}
}
async function showGit(){
 $('modalTitle').textContent='Git Coding Studio';$('modalContent').replaceChildren();$('overlay').classList.remove('hidden');$('overlay').classList.add('flex');
 try{
  const status=await api('/api/git/status');
  $('modalContent').append(node('p','font-semibold text-indigo-700','Branch: '+status.branch));
  if(!status.changes.length)$('modalContent').append(node('p','text-slate-500','Working tree bersih.'));
  status.changes.forEach(change=>$('modalContent').append(node('pre','message-pre whitespace-pre-wrap break-words text-xs rounded-lg bg-slate-50 border border-slate-200 p-3',change)));
  $('modalContent').append(node('p','text-xs text-slate-500','Untuk membuat branch/commit, ketik /git-branch hermas/nama atau /git-commit file.php :: pesan pada mode chat biasa. Tetap memerlukan persetujuan.'));
 }catch(err){$('modalContent').append(node('p','text-amber-700 text-sm',err.message));}
}
async function showStages(taskId){
 $('modalTitle').textContent='Tahapan Multi-Agent';$('modalContent').replaceChildren();$('overlay').classList.remove('hidden');$('overlay').classList.add('flex');
 try{
  const stages=await api('/api/tasks/'+encodeURIComponent(taskId)+'/stages');
  if(!stages.length)$('modalContent').append(node('p','text-slate-500','Belum ada tahapan yang selesai.'));
  stages.forEach(stage=>{
   const panel=node('div','rounded-xl border border-slate-200 bg-slate-50 p-4');
   panel.append(node('h4','font-bold text-indigo-800',stage.role.toUpperCase()));
   panel.append(node('p','text-[11px] text-slate-400',`${stage.duration_ms} ms · ${stage.created_at}`));
   panel.append(node('pre','message-pre whitespace-pre-wrap break-words mt-3 text-xs text-slate-700',stage.result));
   $('modalContent').append(panel);
  });
 }catch(err){toast(err.message);}
}
async function showMetrics(){
 $('modalTitle').textContent='Reliability & observability';$('modalContent').replaceChildren();$('overlay').classList.remove('hidden');$('overlay').classList.add('flex');
 try{
  const d=await api('/api/metrics');
  const rows=[`Integritas audit: ${d.audit_verified?'VALID':'TIDAK VALID'}`,`Rata-rata tugas berhasil: ${d.average_completed_ms} ms`,`Task gagal/interrupted: ${d.failures}`,`Token hari ini: ${d.usage_today.total_tokens}`,`Jumlah task per status: ${JSON.stringify(d.counts)}`];
  rows.forEach(r=>$('modalContent').append(node('div','border border-slate-200 bg-slate-50 rounded-xl p-4 text-sm',r)));
  $('modalContent').append(node('h4','font-bold','Peristiwa terbaru'));
  d.recent_events.forEach(e=>$('modalContent').append(node('p','text-xs text-slate-600 break-words',`${e.created_at} · ${e.task_id.slice(0,8)} · ${e.event} ${e.details}`)));
 }catch(err){toast(err.message);}
}
async function pollActiveTask(){
 if(!state.csrf||!state.sid||state.busy||!state.taskActive)return;
 try{
  const d=await api('/api/sessions/'+encodeURIComponent(state.sid));
  const fp=JSON.stringify([(d.messages||[]).length,(d.pending||[]).map(a=>a.id+':'+a.state),(d.tasks||[])[0]?.status||'tidak ada']);
  if(fp!==state.lastTaskFingerprint)await loadSession(state.sid);
 }catch(_){}
}
setInterval(pollActiveTask,2000);
async function showRecords(type){
 const map={memory:['Memory tersimpan','/api/memory'],skills:['Skills disetujui','/api/skills'],audit:['Audit log','/api/audit']};
 const [title,url]=map[type];$('modalTitle').textContent=title;$('modalContent').replaceChildren();$('overlay').classList.remove('hidden');$('overlay').classList.add('flex');
 try{
  const data=await api(url);const records=Array.isArray(data)?data:data.events;
  if(type==='audit')$('modalContent').append(node('p',data.verified?'text-emerald-700 font-semibold':'text-red-700 font-semibold',data.verified?'Rantai hash audit valid':'PERINGATAN: integritas audit gagal'));
  if(!records.length)$('modalContent').append(node('p','text-slate-500','Belum ada data.'));
  records.forEach(r=>{
   const card=node('div','border border-slate-200 bg-slate-50 rounded-xl p-4');
   const headline=r.key||r.slug||r.event||'Info';card.append(node('div','font-semibold text-slate-900 break-words',headline));
   const body=r.value||r.content||r.details||'';card.append(node('pre','message-pre whitespace-pre-wrap break-all text-xs text-slate-600 mt-2',body));
   if(type==='memory')card.append(node('p','text-xs text-indigo-700 mt-2',`Project: ${r.project} · ${r.status}${r.expires_at?' · Expiry: '+r.expires_at:''} · Confidence: ${r.confidence}`),node('p','text-xs text-slate-500 mt-1','Evidence: '+r.evidence));
   card.append(node('div','text-[11px] text-slate-400 mt-2',r.updated_at||r.created_at||''));$('modalContent').append(card);
  });
 }catch(err){toast(err.message);}
}
function openPanel(title){
 $('modalTitle').textContent=title;
 $('modalContent').replaceChildren();
 $('overlay').classList.remove('hidden');$('overlay').classList.add('flex');
 return $('modalContent');
}
function formInput(label,type='text',value='',placeholder=''){
 const holder=node('label','block text-xs font-semibold text-slate-600 mb-2',label);
 const input=node(type==='textarea'?'textarea':'input',
     'block w-full mt-2 rounded-xl border border-slate-200 p-3 text-sm text-slate-800 outline-none focus:ring-2 focus:ring-indigo-200');
 if(type!=='textarea')input.type=type;
 if(type==='textarea')input.rows=3;
 input.value=value;
 input.placeholder=placeholder;
 holder.append(input);
 return {holder,input};
}
function formSelect(label,options){
 const holder=node('label','block text-xs font-semibold text-slate-600 mb-2',label);
 const input=node('select','block w-full mt-2 rounded-xl border border-slate-200 p-3 text-sm');
 options.forEach(([value,title])=>{const opt=node('option','',title);opt.value=value;input.append(opt);});
 holder.append(input);return {holder,input};
}
function actionButton(text){
 const b=node('button','rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white px-4 py-2.5 text-xs font-semibold',text);
 b.type='button';return b;
}
function infoPanel(text){return node('p','text-xs text-slate-500 leading-5',text);}

async function showWorkflows(){
 const parent=openPanel('Visual Workflow Builder');
 parent.append(infoPanel('Rangkai 1–8 langkah Chat atau Multi-Agent. Langkah berikutnya baru dijalankan setelah tahap sebelumnya berhasil. File/code tetap perlu persetujuan.'));
 if(!state.sid){parent.append(infoPanel('Buat sesi terlebih dahulu.'));return;}
 const name=formInput('Nama workflow','text','','Contoh: Review lalu audit PHP');parent.append(name.holder);
 const stepsBox=node('div','space-y-3');parent.append(stepsBox);
 const steps=[{kind:'orchestrate',message:'Tinjau struktur proyek ini, cari risiko utama dan buat rencana perbaikan tanpa mengubah file.'}];
 function drawSteps(){
  stepsBox.replaceChildren();
  steps.forEach((step,i)=>{
   const box=node('section','rounded-xl border border-slate-200 bg-slate-50 p-4 space-y-3');
   const head=node('div','flex justify-between items-center');head.append(node('span','text-sm font-bold text-indigo-700',`Langkah ${i+1}`));
   const remove=node('button','text-xs text-red-700 font-semibold','Hapus');remove.type='button';remove.disabled=steps.length===1;
   remove.onclick=()=>{steps.splice(i,1);drawSteps();};head.append(remove);box.append(head);
   const kind=formSelect('Jenis agent',[['chat','Chat Agent'],['orchestrate','Multi-Agent Review']]);kind.input.value=step.kind;
   kind.input.onchange=()=>{step.kind=kind.input.value;};box.append(kind.holder);
   const prompt=formInput('Instruksi','textarea',step.message,'Instruksi yang akan dijalankan');
   prompt.input.oninput=()=>{step.message=prompt.input.value;};box.append(prompt.holder);stepsBox.append(box);
  });
 }
 drawSteps();
 const actions=node('div','flex flex-wrap gap-2');
 const add=actionButton('+ Tambah langkah');add.onclick=()=>{if(steps.length>=8)return toast('Maksimum 8 langkah');steps.push({kind:'chat',message:''});drawSteps();};
 const save=actionButton('Simpan workflow');save.onclick=async()=>{
  try{await api('/api/workflows',{method:'POST',body:JSON.stringify({session_id:state.sid,name:name.input.value,steps})});toast('Workflow disimpan');await showWorkflows();}
  catch(err){toast(err.message);}
 };
 actions.append(add,save);parent.append(actions);
 parent.append(node('h4','font-bold mt-5 text-slate-800','Workflow tersimpan'));
 try{
  const [flows,runs]=await Promise.all([api('/api/workflows'),api('/api/workflow-runs')]);
  if(!flows.length)parent.append(infoPanel('Belum ada workflow.'));
  for(const f of flows){
   const item=node('section','rounded-xl border border-slate-200 p-4 space-y-2');
   item.append(node('p','font-semibold text-slate-800',f.name),
               infoPanel(`${f.steps.length} langkah · ${f.enabled?'Aktif':'Nonaktif'} · ${f.id}`));
   const recent=runs.find(r=>r.workflow_id===f.id);
   if(recent)item.append(infoPanel(`Run terakhir: ${recent.status} · langkah ${recent.step_index+1}`));
   const row=node('div','flex gap-2 flex-wrap');
   const launch=actionButton('Jalankan');launch.disabled=!f.enabled;
   launch.onclick=async()=>{try{await api('/api/workflows/'+encodeURIComponent(f.id)+'/run',{method:'POST',body:'{}'});toast('Workflow masuk antrean');await showWorkflows();}catch(err){toast(err.message);}};
   const toggle=actionButton(f.enabled?'Nonaktifkan':'Aktifkan');toggle.onclick=async()=>{
    try{await api('/api/workflows/'+encodeURIComponent(f.id)+'/enabled',{method:'POST',body:JSON.stringify({enabled:!f.enabled})});await showWorkflows();}
    catch(err){toast(err.message);}
   };
   row.append(launch,toggle);
   if(recent && ['running','waiting','paused'].includes(recent.status)){
    const cancel=actionButton('Batalkan run');cancel.onclick=async()=>{
     if(!confirm('Batalkan workflow ini?'))return;
     try{await api('/api/workflow-runs/'+encodeURIComponent(recent.id)+'/cancel',{method:'POST',body:'{}'});await showWorkflows();}catch(err){toast(err.message);}
    };row.append(cancel);
   }
   item.append(row);parent.append(item);
  }
 }catch(err){toast(err.message);}
}

async function showScheduler(){
 const parent=openPanel('Cron Scheduler · UTC');
 parent.append(infoPanel('Format lima bagian: menit jam tanggal bulan hari-pekan. Contoh: 0 1 * * * = setiap hari pukul 01.00 UTC (08.00 WIB). Jadwal baru tidak aktif sebelum kamu mengaktifkannya.'));
 if(!state.sid)return;
 const name=formInput('Nama jadwal','text','Audit harian');
 const cron=formInput('Ekspresi cron (UTC)','text','0 1 * * *');
 const kind=formSelect('Target',[['chat','Chat Agent'],['orchestrate','Multi-Agent Review'],['workflow','Workflow tersimpan']]);
 const prompt=formInput('Instruksi','textarea','Periksa status proyek tanpa melakukan perubahan file.');
 const selection=formSelect('Workflow', [['','Pilih workflow']]);
 try{(await api('/api/workflows')).forEach(f=>{const opt=node('option','',f.name);opt.value=f.id;selection.input.append(opt);});}
 catch(err){toast(err.message);}
 kind.input.onchange=()=>{prompt.holder.classList.toggle('hidden',kind.input.value==='workflow');selection.holder.classList.toggle('hidden',kind.input.value!=='workflow');};
 selection.holder.classList.add('hidden');
 parent.append(name.holder,cron.holder,kind.holder,prompt.holder,selection.holder);
 const save=actionButton('Simpan jadwal (nonaktif)');save.onclick=async()=>{
  try{await api('/api/schedules',{method:'POST',body:JSON.stringify({session_id:state.sid,name:name.input.value,cron:cron.input.value,
  target:kind.input.value,message:prompt.input.value,workflow_id:kind.input.value==='workflow'?selection.input.value:null,enabled:false})});
  toast('Jadwal tersimpan dan masih nonaktif');await showScheduler();}
  catch(err){toast(err.message);}
 };parent.append(save,node('h4','font-bold mt-5','Daftar jadwal'));
 try{
  const schedules=await api('/api/schedules');
  if(!schedules.length)parent.append(infoPanel('Belum ada jadwal.'));
  for(const item of schedules){
   const card=node('div','border border-slate-200 rounded-xl p-4 space-y-2');
   card.append(node('p','font-semibold',item.name),infoPanel(`${item.cron} UTC · ${item.target} · ${item.enabled?'AKTIF':'NONAKTIF'}`),
               infoPanel('Jadwal berikut: '+item.next_run_at));
   if(item.last_error)card.append(node('p','text-xs text-amber-700','Sesi sebelumnya dilewati: '+item.last_error));
   const b=actionButton(item.enabled?'Nonaktifkan':'Aktifkan');
   b.onclick=async()=>{try{await api('/api/schedules/'+encodeURIComponent(item.id)+'/enabled',{method:'POST',body:JSON.stringify({enabled:!item.enabled})});await showScheduler();}catch(err){toast(err.message);}};
   card.append(b);parent.append(card);
  }
 }catch(err){toast(err.message);}
}

async function showIntegrations(){
 const parent=openPanel('Plugin & MCP Registry');
 parent.append(infoPanel('v0.4 adalah katalog manifest saja, bukan eksekusi tool MCP. Tidak ada kode eksternal yang dijalankan melalui registry ini.'));
 const manifest=formInput('Manifest JSON','textarea',JSON.stringify({name:'example_plugin',version:'1.0.0',description:'Contoh katalog MCP',transport:'mcp-http',tools:[{name:'list_items',description:'Daftar item',read_only:true}]},null,2));
 manifest.input.rows=8;parent.append(manifest.holder);
 const save=actionButton('Daftarkan manifest');save.onclick=async()=>{
  try{await api('/api/integrations',{method:'POST',body:JSON.stringify({manifest:JSON.parse(manifest.input.value)})});toast('Manifest disimpan');await showIntegrations();}catch(err){toast(err.message);}
 };parent.append(save,node('h4','font-bold mt-5','Registry integrasi'));
 try{
  const integrations=await api('/api/integrations');
  if(!integrations.length)parent.append(infoPanel('Belum ada manifest yang didaftarkan.'));
  for(const i of integrations){
   const card=node('div','border border-slate-200 rounded-xl p-4 space-y-2');
   card.append(node('p','font-semibold',i.name+' · '+i.manifest.version),
               infoPanel(`${i.manifest.transport} · ${i.manifest.tools.length} tool · ${i.enabled?'Terdaftar aktif':'Nonaktif'}`),
               infoPanel(i.manifest.description));
   const b=actionButton(i.enabled?'Nonaktifkan':'Aktifkan katalog');
   b.onclick=async()=>{try{await api('/api/integrations/'+encodeURIComponent(i.id)+'/enabled',{method:'POST',body:JSON.stringify({enabled:!i.enabled})});await showIntegrations();}catch(err){toast(err.message);}};
   card.append(b);parent.append(card);
  }
 }catch(err){toast(err.message);}
}

async function showBrowser(){
 const parent=openPanel('Browser Automation · Read-only Preview');
 parent.append(infoPanel('Dinonaktifkan secara default. Admin harus memasang Playwright, mengaktifkan HERMAS_BROWSER_ENABLED dan menentukan hostname publik di HERMAS_BROWSER_HOSTS. Browser tidak memiliki session/cookies proyek.'));
 const url=formInput('URL publik allowlisted','url','','https://example.org/');parent.append(url.holder);
 const b=actionButton('Ambil judul & teks halaman');
 const output=node('pre','message-pre whitespace-pre-wrap break-words rounded-xl bg-slate-950 text-slate-100 p-4 text-xs');
 b.onclick=async()=>{b.disabled=true;try{const d=await api('/api/browser/preview',{method:'POST',body:JSON.stringify({url:url.input.value})});output.textContent=JSON.stringify(d,null,2);}catch(err){output.textContent=err.message;}finally{b.disabled=false;}};
 parent.append(b,output);
}

async function showVPS(){
 const parent=openPanel('VPS Commander · WordPress Intelligence');
 parent.append(infoPanel('v0.5+: inventaris multi-VPS, SSH read-only opt-in, audit metadata WordPress, histori, dan rancangan backup/deployment. Tidak ada shell bebas, backup DB otomatis, ataupun deploy langsung.'));
 let sshEnabled=false;
 try{const me=await api('/api/me');sshEnabled=!!me.vps_ssh_enabled;}catch(err){toast(err.message);}
 parent.append(node('div',sshEnabled?'rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800':'rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800',
   sshEnabled?'Mode SSH read-only diaktifkan operator. Host harus memiliki known_hosts tepercaya dan key non-root.':'Mode SSH mati secara default. Tambah inventaris dan susun rencana terlebih dahulu; untuk audit remote, aktifkan SSH sesuai panduan .env.'));
 const name=formInput('Nama server','text','','Contoh: VPS WordPress 1');
 const hostname=formInput('IP / hostname SSH','text','','Contoh: vps1.example.com');
 const user=formInput('User Linux non-root','text','deploy','deploy');
 const port=formInput('Port SSH','number','22','22');
 parent.append(node('h4','font-bold mt-4','Tambah VPS'),name.holder,hostname.holder,user.holder,port.holder);
 const add=actionButton('Tambahkan ke inventaris (nonaktif)');
 add.onclick=async()=>{add.disabled=true;try{
   await api('/api/vps/hosts',{method:'POST',body:JSON.stringify({name:name.input.value,hostname:hostname.input.value,ssh_user:user.input.value,port:Number(port.input.value)})});
   toast('VPS disimpan; pemeriksaan masih nonaktif');await showVPS();
 }catch(err){toast(err.message);}finally{add.disabled=false;}};
 parent.append(add,node('h4','font-bold mt-6','Server tersimpan'));
 let hosts=[];
 try{hosts=await api('/api/vps/hosts');}catch(err){toast(err.message);return;}
 if(!hosts.length)parent.append(infoPanel('Inventaris masih kosong. Kamu belum perlu menyambungkan VPS sekarang.'));
 for(const host of hosts){
  const card=node('section','border border-slate-200 bg-white rounded-xl p-4 space-y-3');
  card.append(node('div','font-semibold text-slate-900',`${host.name} · ${host.retired?'DIARSIPKAN':host.enabled?'AKTIF':'NONAKTIF'}`),
   infoPanel(`${host.ssh_user}@${host.hostname}:${host.port} · ID: ${host.id}`));
  if(host.retired){parent.append(card);continue;}
  const row=node('div','flex flex-wrap gap-2');
  const toggle=actionButton(host.enabled?'Nonaktifkan host':'Aktifkan host');
  toggle.onclick=async()=>{try{await api('/api/vps/hosts/'+encodeURIComponent(host.id)+'/enabled',{method:'POST',body:JSON.stringify({enabled:!host.enabled})});await showVPS();}catch(err){toast(err.message);}};
  row.append(toggle);
  const retire=actionButton('Arsipkan');retire.className='rounded-xl border border-slate-300 text-slate-700 px-4 py-2.5 text-xs font-semibold';
  retire.onclick=async()=>{if(!confirm('Arsipkan server ini? Histori pemeriksaan tetap tersimpan.'))return;
   try{await api('/api/vps/hosts/'+encodeURIComponent(host.id)+'/retire',{method:'POST',body:'{}'});await showVPS();}catch(err){toast(err.message);}};
  row.append(retire);card.append(row);
  const root=formInput('Root WordPress untuk audit / rencana','text','','/var/www/domain.com');
  root.input.value='/var/www/domain.com';card.append(root.holder);
  const scanRow=node('div','flex flex-wrap gap-2');
  const health=actionButton('Periksa VPS');const wp=actionButton('Audit WordPress');
  async function check(kind,button){button.disabled=true;
   try{const result=await api('/api/vps/hosts/'+encodeURIComponent(host.id)+'/checks',{method:'POST',body:JSON.stringify({kind,root:kind==='wordpress'?root.input.value:''})});
    const out=node('pre','message-pre whitespace-pre-wrap break-all rounded-xl p-3 bg-slate-950 text-slate-100 text-xs',JSON.stringify(result.result,null,2));
    card.append(out);toast('Hasil pemeriksaan disimpan');
   }catch(err){toast(err.message);}finally{button.disabled=false;}}
  health.onclick=()=>check('health',health);wp.onclick=()=>check('wordpress',wp);
  health.disabled=!sshEnabled||!host.enabled;wp.disabled=!sshEnabled||!host.enabled;
  scanRow.append(health,wp);card.append(scanRow);
  const planRow=node('div','flex flex-wrap gap-2');
  for(const kind of ['backup','deployment']){
   const b=actionButton(kind==='backup'?'Susun rencana backup':'Susun rencana deployment');
   b.onclick=async()=>{try{
    await api('/api/vps/hosts/'+encodeURIComponent(host.id)+'/plans',{method:'POST',body:JSON.stringify({kind,root:root.input.value,notes:'Draf manual, tidak mengeksekusi perintah server.'})});
    toast('Rencana disimpan');await showVPS();
   }catch(err){toast(err.message);}};planRow.append(b);
  }
  card.append(planRow);
  try{
   const [checks,plans]=await Promise.all([api('/api/vps/hosts/'+encodeURIComponent(host.id)+'/checks'),api('/api/vps/hosts/'+encodeURIComponent(host.id)+'/plans')]);
   if(checks.length){card.append(node('h5','font-semibold text-sm','Pemeriksaan terakhir'));
    checks.slice(0,3).forEach(c=>{const detail=node('details','rounded-lg bg-slate-50 border border-slate-200 p-3');
     detail.append(node('summary','text-xs font-semibold cursor-pointer',`${c.kind} · ${c.status} · ${c.observed_at}`),node('pre','message-pre whitespace-pre-wrap break-all text-xs mt-2',JSON.stringify(c.result,null,2)));
     card.append(detail);});}
   if(plans.length){card.append(node('h5','font-semibold text-sm','Rencana operasional (manual)'));
    plans.slice(0,3).forEach(plan=>{const detail=node('details','rounded-lg bg-slate-50 border border-slate-200 p-3');
     detail.append(node('summary','text-xs font-semibold cursor-pointer',`${plan.kind} · ${plan.created_at} · DRAFT`));
     plan.steps.forEach((s,i)=>detail.append(node('p','text-xs text-slate-600 mt-2',`${i+1}. ${s.title}`)));
     card.append(detail);});}
  }catch(err){toast(err.message);}
  parent.append(card);
 }
}


// v0.6: operations observability, alerts, schedule, snapshots, and accounts.
async function showOperations(){
 const parent=openPanel('Intelligent Operations · Monitoring & Alerts');
 parent.append(infoPanel('v0.6: pemeriksaan VPS terjadwal hanya jika HERMAS_VPS_MONITOR_ENABLED dan SSH sudah diaktifkan. Alert Telegram opt-in; tidak ada auto-remediation atau remote write.'));
 let summary,alerts,monitors;
 try{[summary,alerts,monitors]=await Promise.all([api('/api/operations/summary'),api('/api/operations/alerts'),api('/api/operations/monitors')]);}
 catch(err){parent.append(infoPanel(err.message));return;}
 parent.append(node('h4','font-bold','Ringkasan layanan'));
 parent.append(infoPanel(`${summary.active_monitors} monitor aktif · ${summary.alerts.open||0} alert terbuka · ${summary.alerts.acknowledged||0} diakui · ${summary.snapshots} snapshot lokal · Audit: ${summary.audit_verified?'VALID':'PERLU DIPERIKSA'}`));
 parent.append(node('h4','font-bold mt-5','Alert terbaru'));
 if(!alerts.length)parent.append(infoPanel('Belum ada alert yang tercatat.'));
 alerts.slice(0,25).forEach(a=>{
  const card=node('div','border border-slate-200 rounded-xl p-3 space-y-2');
  card.append(node('p','font-semibold text-sm',`${a.severity.toUpperCase()} · ${a.kind} · ${a.status}`),
    infoPanel(`Host ID ${a.host_id} · ${a.count} kejadian · terakhir ${a.last_seen_at}`));
  if(a.status==='open' && state.role!=='viewer'){
   const b=actionButton('Akui alert');b.onclick=async()=>{try{await api('/api/operations/alerts/'+encodeURIComponent(a.id)+'/acknowledge',{method:'POST',body:'{}'});toast('Alert diakui');await showOperations();}catch(err){toast(err.message);}};card.append(b);
  }
  parent.append(card);
 });
 parent.append(node('h4','font-bold mt-5','Monitoring terjadwal'));
 if(state.role==='admin'){
  try{
   const hosts=await api('/api/vps/hosts');
   const choices=hosts.filter(h=>!h.retired).map(h=>[h.id,h.name+' · '+h.hostname]);
   if(choices.length){
    const host=formSelect('Server',choices),kind=formSelect('Jenis pemeriksaan',[['health','Kesehatan VPS'],['wordpress','Audit WordPress']]);
    const interval=formInput('Interval (menit)','number','30','30');interval.input.min=5;interval.input.max=1440;
    const root=formInput('Folder WordPress (hanya bila jenis WordPress)','text','','/var/www/example.org');
    parent.append(host.holder,kind.holder,interval.holder,root.holder);
    const add=actionButton('Tambah monitor (nonaktif)');add.onclick=async()=>{
     try{await api('/api/operations/monitors',{method:'POST',body:JSON.stringify({host_id:host.input.value,kind:kind.input.value,interval_minutes:Number(interval.input.value),root:kind.input.value==='wordpress'?root.input.value:''})});toast('Monitor ditambahkan, masih nonaktif');await showOperations();}catch(err){toast(err.message);}
    };parent.append(add);
   }else parent.append(infoPanel('Daftarkan VPS dahulu di VPS Commander.'));
  }catch(err){toast(err.message);}
 }
 if(!monitors.length)parent.append(infoPanel('Belum ada monitor.'));
 for(const m of monitors){
  const card=node('div','border border-slate-200 rounded-xl p-3 space-y-2');
  card.append(node('p','font-semibold text-sm',`${m.kind} · ${m.interval_minutes} menit · ${m.enabled?'AKTIF':'NONAKTIF'}`),infoPanel(`Host ${m.host_id} · Terakhir ${m.last_status||'belum'} · Berikutnya ${m.next_run_at}`));
  if(state.role==='admin'){
   const toggle=actionButton(m.enabled?'Nonaktifkan':'Aktifkan');toggle.onclick=async()=>{
    try{await api('/api/operations/monitors/'+encodeURIComponent(m.id)+'/enabled',{method:'POST',body:JSON.stringify({enabled:!m.enabled})});toast('Status monitor diperbarui');await showOperations();}catch(err){toast(err.message);}
   };card.append(toggle);
  }parent.append(card);
 }
}

async function showSnapshots(){
 const parent=openPanel('Workspace Snapshots · Preview Restore');
 parent.append(infoPanel('Hanya source workspace lokal, tidak termasuk VPS, database, .env atau file rahasia yang terdeteksi berdasarkan nama. Restore memerlukan preview bertanda tangan, token 10 menit, dan konfirmasi RESTORE. Multi-file restore tidak atomik.'));
 const create=actionButton('Buat snapshot source lokal');create.onclick=async()=>{
  create.disabled=true;try{await api('/api/snapshots',{method:'POST',body:'{}'});toast('Snapshot dibuat');await showSnapshots();}catch(err){toast(err.message);}finally{create.disabled=false;}
 };parent.append(create,node('h4','font-bold mt-5','Riwayat snapshot'));
 let items=[];try{items=await api('/api/snapshots');}catch(err){toast(err.message);return;}
 if(!items.length)parent.append(infoPanel('Belum ada snapshot.'));
 items.forEach(item=>{
  const card=node('div','border border-slate-200 rounded-xl p-3 space-y-2');
  card.append(node('p','font-semibold text-sm',`${item.created_at} · ${item.files_count} file · ${item.total_bytes} byte`));
  const preview=actionButton('Preview restore');preview.onclick=async()=>{
   try{const plan=await api('/api/snapshots/'+encodeURIComponent(item.id)+'/preview');
    const detail=node('pre','message-pre whitespace-pre-wrap bg-slate-950 text-slate-100 p-3 rounded-xl text-xs',plan.changes.map(c=>c.action+'  '+c.path).join('\n'));
    const confirmField=formInput('Ketik RESTORE untuk melanjutkan','text','','RESTORE');
    const restore=actionButton('Konfirmasi restore workspace');
    restore.onclick=async()=>{if(confirmField.input.value!=='RESTORE'){toast('Ketik RESTORE secara persis');return;}
     restore.disabled=true;try{const result=await api('/api/snapshots/'+encodeURIComponent(item.id)+'/restore',{method:'POST',body:JSON.stringify({token:plan.token,confirmation:'RESTORE'})});toast('Restore selesai: '+result.restored_files+' file');await showSnapshots();}catch(err){toast(err.message);}finally{restore.disabled=false;}
    };card.append(detail,infoPanel(plan.warning),confirmField.holder,restore);
   }catch(err){toast(err.message);}
  };card.append(preview);parent.append(card);
 });
}

async function showAccounts(){
 const parent=openPanel('Akun & Role-Based Access Control');
 parent.append(infoPanel('Admin utama tetap disimpan di .env. Akun tambahan hanya memiliki peran Viewer (monitoring baca-saja) atau Operator (monitoring dan pengakuan alert / health check manual). Tidak bisa mengakses chat, kode, memori, approvals, snapshot, maupun manajemen akun.'));
 const username=formInput('Username','text','','operator01');
 const password=formInput('Password minimal 14 karakter','password','','Gunakan password unik');
 const role=formSelect('Peran',[['viewer','Viewer'],['operator','Operator']]);
 parent.append(username.holder,password.holder,role.holder);
 const add=actionButton('Buat akun');add.onclick=async()=>{
  try{await api('/api/accounts',{method:'POST',body:JSON.stringify({username:username.input.value,password:password.input.value,role:role.input.value})});password.input.value='';toast('Akun dibuat');await showAccounts();}catch(err){toast(err.message);}
 };parent.append(add,node('h4','font-bold mt-5','Akun tambahan'));
 let users=[];try{users=await api('/api/accounts');}catch(err){toast(err.message);return;}
 if(!users.length)parent.append(infoPanel('Belum ada akun tambahan.'));
 users.forEach(u=>{
  const card=node('div','border border-slate-200 rounded-xl p-3 space-y-2');
  card.append(node('p','font-semibold text-sm',`${u.username} · ${u.role.toUpperCase()} · ${u.enabled?'AKTIF':'NONAKTIF'}`));
  const toggle=actionButton(u.enabled?'Nonaktifkan akun':'Aktifkan akun');
  toggle.onclick=async()=>{try{await api('/api/accounts/'+encodeURIComponent(u.id),{method:'POST',body:JSON.stringify({enabled:!u.enabled})});await showAccounts();}catch(err){toast(err.message);}};
  const newRole=formSelect('Ubah peran',[['viewer','Viewer'],['operator','Operator']]);newRole.input.value=u.role;
  const change=actionButton('Simpan peran');change.onclick=async()=>{try{await api('/api/accounts/'+encodeURIComponent(u.id),{method:'POST',body:JSON.stringify({role:newRole.input.value})});await showAccounts();}catch(err){toast(err.message);}};
  const newPassword=formInput('Reset password','password','','Password baru min. 14 karakter');
  const reset=actionButton('Reset password');reset.onclick=async()=>{try{await api('/api/accounts/'+encodeURIComponent(u.id),{method:'POST',body:JSON.stringify({password:newPassword.input.value})});newPassword.input.value='';toast('Password diubah; sesi lama dicabut');await showAccounts();}catch(err){toast(err.message);}};
  card.append(toggle,newRole.holder,change,newPassword.holder,reset);parent.append(card);
 });
}

$('loginForm').addEventListener('submit',async e=>{
 e.preventDefault();$('loginError').textContent='';
 try{const data=await api('/api/login',{method:'POST',body:JSON.stringify({username:$('loginUsername').value,password:$('password').value})});state.csrf=data.csrf;$('password').value='';showApp();await refreshStatus();if(state.role==='admin'){const sessions=await listSessions();if(sessions.length)await loadSession(sessions[0].id);else await createSession();}else await showOperations();}
 catch(err){$('loginError').textContent=err.message;}
});
$('chatForm').addEventListener('submit',sendMessage);
$('chatInput').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('chatForm').requestSubmit();}});
$('newSession').onclick=createSession;
$('openSidebar').onclick=()=>$('sidebar').classList.remove('-translate-x-full');
$('closeSidebar').onclick=closeNav;
$('gitButton').onclick=showGit;
$('vpsButton').onclick=showVPS;
$('operationsButton').onclick=showOperations;
$('snapshotsButton').onclick=showSnapshots;
$('accountsButton').onclick=showAccounts;
$('workflowButton').onclick=showWorkflows;
$('schedulerButton').onclick=showScheduler;
$('integrationsButton').onclick=showIntegrations;
$('browserButton').onclick=showBrowser;
$('memoryButton').onclick=()=>showRecords('memory');
$('skillButton').onclick=()=>showRecords('skills');
$('auditButton').onclick=()=>showRecords('audit');
$('tasksButton').onclick=showTasks;
$('metricsButton').onclick=showMetrics;
$('closeModal').onclick=()=>{$('overlay').classList.add('hidden');$('overlay').classList.remove('flex');};
$('overlay').onclick=e=>{if(e.target===$('overlay'))$('closeModal').click();};
$('logoutButton').onclick=async()=>{try{await api('/api/logout',{method:'POST',body:'{}'});}finally{showLogin();}};
(async()=>{try{showApp();await refreshStatus();if(state.role==='admin'){const sessions=await listSessions();if(sessions.length)await loadSession(sessions[0].id);else await createSession();}else await showOperations();}catch(_){showLogin();}finally{icon();}})();
