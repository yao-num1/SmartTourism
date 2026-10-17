
const STATE={provider:'deepseek',model:'deepseek-chat',apiKey:'',skill:'',crowdId:'',conversationId:crypto.randomUUID(),isStreaming:!1};
const PROVIDERS={deepseek:{name:'DeepSeek',models:['deepseek-chat','deepseek-reasoner']},zhipu:{name:'GLM',models:['glm-4-plus','glm-4-flash','glm-4-long']},qwen:{name:'Qwen',models:['qwen-max','qwen-plus','qwen-turbo']},moonshot:{name:'Kimi',models:['moonshot-v1-8k','moonshot-v1-32k','moonshot-v1-128k']}};
const SKILL_NAMES={'tourism-advisor':'🗺️ 咨询规划','tourism-marketing':'📱 营销文创','smart-matching':'🎯 客源匹配'};

function init(){
  const s=localStorage.getItem('smarttourism_config');
  if(s){try{const c=JSON.parse(s);STATE.provider=c.provider||'deepseek';STATE.model=c.model||'deepseek-chat';STATE.apiKey=c.apiKey||''}catch(e){}}
  document.getElementById('providerSelect').value=STATE.provider;
  updateModelSelect();
  document.getElementById('modelSelect').value=STATE.model;
  if(STATE.apiKey)document.getElementById('apiKeyInput').value=STATE.apiKey;
  loadCrowds();checkHealth();updateBadges();
  // 从 localStorage 恢复插件 Key
  const pc=localStorage.getItem('smarttourism_plugin');
  if(pc){try{const c=JSON.parse(pc);if(c.seniverse_key||c.seniverse_public_key){document.getElementById('seniverseKeyInput').value=c.seniverse_key||c.seniverse_public_key;if(c.seniverse_key){document.querySelector('input[name="seniverseType"][value="private"]').checked=true;}else{document.querySelector('input[name="seniverseType"][value="public"]').checked=true;}}if(c.amap_key)document.getElementById('amapKeyInput').value=c.amap_key;}catch(e){}}
}

function updateModelSelect(){
  const sel=document.getElementById('modelSelect');
  const models=(PROVIDERS[STATE.provider]||PROVIDERS.deepseek).models;
  sel.innerHTML=models.map(m=>'<option value="'+m+'"'+(m===STATE.model?' selected':'')+'>'+m+'</option>').join('');
}

async function checkHealth(){
  try{const r=await fetch('/api/health');const d=await r.json();
    document.getElementById('systemStatus').innerHTML='✅ 服务运行中 v'+d.version+'<br>🧠 Skills: '+((d.skills_loaded||[]).length)+' 个<br>👥 客群: '+(d.crowds_loaded||[]).length+' 类<br>🔌 插件: '+d.plugins_configured+'/'+d.plugins_loaded+' 就绪<br>🤖 模型: '+(d.has_api_key?'已配置':'未配置')+'<br>⏰ '+new Date().toLocaleTimeString();
    document.getElementById('statusBadge').innerHTML='<span class="status-dot online"></span> 在线';
    loadPluginStatus();
  }catch(e){document.getElementById('systemStatus').innerHTML='⚠️ 后端未启动';document.getElementById('statusBadge').innerHTML='<span class="status-dot offline"></span> 离线'}
}

async function loadPluginStatus(){
  try{
    const r=await fetch('/api/plugins');const plugins=await r.json();
    let html='';
    plugins.forEach(p=>{
      const icon=p.configured?'✅':'⚪';
      html+=icon+' <b>'+p.display_name+'</b><br><span style="font-size:10px;color:var(--muted);">'+p.description.substring(0,30)+'...</span><br>';
    });
    document.getElementById('pluginStatus').innerHTML=html||'无插件';
  }catch(e){}
}

async function savePluginConfig(){
  const seniverseType=document.querySelector('input[name="seniverseType"]:checked')?.value||'public';
  const seniverseKey=document.getElementById('seniverseKeyInput').value.trim();
  const amapKey=document.getElementById('amapKeyInput').value.trim();
  const config={};
  if(seniverseKey){
    if(seniverseType==='public') config.seniverse_public_key=seniverseKey;
    else config.seniverse_key=seniverseKey;
  }
  if(amapKey)config.amap_key=amapKey;
  try{
    const r=await fetch('/api/plugins/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(config)});
    const d=await r.json();
    if(d.success){showToast('✅ 插件配置已保存','success');localStorage.setItem('smarttourism_plugin',JSON.stringify(config));loadPluginStatus()}
    else showToast('保存失败','error')
  }catch(e){showToast('保存失败','error')}
}

async function loadCrowds(){
  try{const r=await fetch('/api/crowds');const cs=await r.json();const g=document.getElementById('crowdGrid');
    cs.forEach(c=>{const ch=document.createElement('span');ch.className='crowd-chip';ch.dataset.crowd=c.id;ch.textContent=c.label;ch.onclick=()=>selectCrowd(c.id);g.appendChild(ch)})
  }catch(e){console.log('crowds offline')}
}

function updateBadges(){
  const b=document.getElementById('modelBadge');
  const pn=(PROVIDERS[STATE.provider]||{}).name||STATE.provider;
  b.textContent=STATE.apiKey?'🤖 '+pn+' | '+STATE.model:'🤖 未配置模型';
  b.style.background=STATE.apiKey?'rgba(93,252,142,0.2)':'rgba(255,255,255,0.15)';
}

function selectSkill(s){
  STATE.skill=s;
  document.querySelectorAll('.skill-chip').forEach(e=>e.classList.remove('active'));
  const t=document.querySelector('.skill-chip[data-skill="'+s+'"]');
  if(t)t.classList.add('active');
  updateInputTags();
}

function selectCrowd(id){
  STATE.crowdId=STATE.crowdId===id?'':id;
  document.querySelectorAll('.crowd-chip').forEach(e=>e.classList.toggle('active',e.dataset.crowd===STATE.crowdId));
  updateInputTags();
}

function updateInputTags(){
  let h='';
  if(STATE.skill)h+='<span class="input-tag">'+(SKILL_NAMES[STATE.skill]||STATE.skill)+' <span class="remove-tag" onclick="STATE.skill=\'\';selectSkill(\'\');updateInputTags();">×</span></span>';
  if(STATE.crowdId)h+='<span class="input-tag">👥 '+STATE.crowdId+' <span class="remove-tag" onclick="STATE.crowdId=\'\';selectCrowd(\'\');updateInputTags();">×</span></span>';
  document.getElementById('inputTags').innerHTML=h;
}

function quickScenario(t){
  const p={
    plan:'帮我规划一个3天2夜的旅行方案。请选择一个热门目的地，给出完整的行程、住宿、餐饮推荐。',
    marketing:'为一处热门景区写一篇小红书种草文案和抖音短视频脚本，包含完整的分镜表和文案。',
    match:'分析Z世代年轻客群的画像，匹配最适合他们的景酒餐产品和营销转化策略。',
    creative:'为一家有地方文化特色的景区或民宿，设计3-5款文创产品创意方案，包含引流款、利润款和形象款。',
    full:'执行全流程：先识别目标客群（情侣蜜月型），然后规划三亚5天方案，最后生成小红书营销内容。'
  };
  document.getElementById('userInput').value=p[t]||'';document.getElementById('userInput').focus();autoResize(document.getElementById('userInput'));
}

function autoResize(e){e.style.height='auto';e.style.height=Math.min(e.scrollHeight,120)+'px'}

function handleKeyDown(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage()}}

async function sendMessage(){
  if(STATE.isStreaming)return;
  const input=document.getElementById('userInput');
  const msg=input.value.trim();
  if(!msg)return;
  if(!STATE.apiKey){showToast('请先在右侧配置 API Key','error');return}
  // 自动同步 Key 到后端（防止重启丢失）
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:STATE.apiKey,provider:STATE.provider,model:STATE.model})}).catch(()=>{});

  const wel=document.getElementById('welcomeScreen');
  if(wel)wel.remove();
  addMessage('user',msg);
  input.value='';input.style.height='auto';
  STATE.isStreaming=!0;
  document.getElementById('sendBtn').disabled=!0;

  const typingEl=addTypingIndicator();
  try{
    const data={message:msg,skill:STATE.skill,crowd_id:STATE.crowdId,conversation_id:STATE.conversationId,stream:!0};
    const resp=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    if(!resp.ok){const e=await resp.json();throw new Error(e.error||'请求失败')}

    const reader=resp.body.getReader();const decoder=new TextDecoder();
    typingEl.remove();
    let bubble=addMessage('assistant','');
    const bubbleDiv=bubble.querySelector('.msg-bubble');
    let content='';

    while(1){const{value,done}=await reader.read();if(done)break;
      const text=decoder.decode(value,{stream:!0});
      const lines=text.split('\n');
      for(const line of lines){
        if(!line.startsWith('data: '))continue;
        try{const j=JSON.parse(line.slice(6));if(j.done)break;if(j.content){content+=j.content;bubbleDiv.innerHTML=renderMarkdown(content);scrollBottom()}}catch(e){}
      }
    }
  }catch(e){typingEl?.remove();addMessage('assistant','❌ '+e.message);showToast(e.message,'error')}
  finally{STATE.isStreaming=!1;document.getElementById('sendBtn').disabled=!1}
}

function addMessage(role,text){
  const c=document.getElementById('chatContainer');
  const d=document.createElement('div');d.className='msg-row '+role;
  const av=role==='user'?'👤':'🦉';
  d.innerHTML='<div class="msg-avatar">'+av+'</div><div class="msg-bubble">'+renderMarkdown(text)+'</div><div class="msg-actions" style="margin-top:4px;display:flex;gap:4px;"><button class="btn-v3 btn-v3-sm btn-v3-outline" onclick="copyMsg(this)" style="font-size:10px;padding:2px 8px;">📋 复制</button></div>';
  c.appendChild(d);scrollBottom();return d;
}

function addTypingIndicator(){
  const c=document.getElementById('chatContainer');
  const d=document.createElement('div');d.className='msg-row assistant';
  d.innerHTML='<div class="msg-avatar">🦉</div><div class="msg-bubble"><div class="typing-dots"><span></span><span></span><span></span></div></div>';
  c.appendChild(d);scrollBottom();return d;
}

function renderMarkdown(t){
  if(!t)return'';
  let h=t
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/\*(.+?)\*/g,'<em>$1</em>')
    .replace(/^### (.+)/gm,'<h3>$1</h3>')
    .replace(/^## (.+)/gm,'<h2>$1</h2>')
    .replace(/^# (.+)/gm,'<h2>$1</h2>')
    .replace(/`{3}([\s\S]*?)`{3}/g,'<pre><code>$1</code></pre>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/^> (.+)/gm,'<blockquote>$1</blockquote>')
    .replace(/^- (.+)/gm,'<li>$1</li>')
    .replace(/\n/g,'<br>');
  h=h.replace(/(<li>.*?<\/li>(<br>)?)+/g,'<ul>$&</ul>');
  return h;
}

function copyMsg(btn){
  const b=btn.closest('.msg-row').querySelector('.msg-bubble');
  navigator.clipboard.writeText(b.textContent).then(()=>showToast('已复制','success'));
}

function scrollBottom(){const c=document.getElementById('chatContainer');c.scrollTop=c.scrollHeight}

function showToast(msg,type){
  const tc=document.getElementById('toastContainer');
  const t=document.createElement('div');t.className='toast-v3 '+(type||'info');t.textContent=msg;tc.appendChild(t);
  setTimeout(()=>t.remove(),3000);
}

function saveConfig(){
  STATE.provider=document.getElementById('providerSelect').value;
  STATE.model=document.getElementById('modelSelect').value;
  STATE.apiKey=document.getElementById('apiKeyInput').value.trim();
  if(!STATE.apiKey){showToast('请输入 API Key','error');return}
  const config={provider:STATE.provider,model:STATE.model,api_key:STATE.apiKey};
  localStorage.setItem('smarttourism_config',JSON.stringify(config));
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(config)})
    .then(r=>r.json()).then(d=>{
      if(d.success){showToast('✅ 配置已保存，模型已连接','success');updateBadges();checkHealth()}
      else showToast(d.error||'保存失败','error')
    }).catch(()=>{showToast('配置已本地保存（后端未连接）','info');updateBadges()});
}

function onProviderChange(){
  STATE.provider=document.getElementById('providerSelect').value;
  STATE.model=(PROVIDERS[STATE.provider]||PROVIDERS.deepseek).models[0];
  updateModelSelect();
}

function toggleKeyVisibility(){
  const i=document.getElementById('apiKeyInput');
  i.type=i.type==='password'?'text':'password';
}

async function clearChat(){
  document.getElementById('chatContainer').innerHTML='<div class="welcome-splash" id="welcomeScreen"><div class="hero-icon">🏞️</div><h2>对话已清空</h2><p>开始新的对话吧</p><div class="quick-chips-v3"><span class="chip-v3" onclick="quickAsk(\'帮我规划一个3天2夜的旅行\')">📅 行程规划</span><span class="chip-v3" onclick="quickAsk(\'附近有什么好玩的\')">🗺️ 附近景点</span><span class="chip-v3" onclick="quickAsk(\'帮我写一篇小红书营销文案\')">✍️ 营销文案</span></div></div>';
  STATE.conversationId=crypto.randomUUID();
  try{await fetch('/api/conversations/'+STATE.conversationId,{method:'DELETE'})}catch(e){}
}

function exportChat(type){
  const msgs=document.querySelectorAll('.msg-bubble');
  if(msgs.length===0){showToast('没有可导出的内容','error');return}
  let content='';let i=0;
  msgs.forEach(m=>{const role=i%2===0?'User':'AI';i++;
    if(type==='markdown')content+='### '+role+'\n\n'+m.textContent+'\n\n---\n\n';
    else content+=JSON.stringify({role,content:m.textContent})+'\n';
  });
  const blob=new Blob([content],{type:type==='markdown'?'text/markdown':'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='smarttourism_'+new Date().toISOString().slice(0,10)+(type==='markdown'?'.md':'.json');
  a.click();showToast('导出成功','success');
}

function togglePanel(){
  const p=document.getElementById('rightPanel');
  p.style.display=p.style.display==='none'?'flex':'none';
}

function quickAsk(text) {
  document.getElementById('userInput').value=text;
  sendMessage();
}

function toggleDark(){
  document.documentElement.classList.toggle('dark');
  const isDark=document.documentElement.classList.contains('dark');
  localStorage.setItem('smarttourism_dark',isDark?'1':'0');
  showToast(isDark?'🌙 暗色模式已开启':'☀️ 亮色模式已开启','info');
}

// 恢复暗色模式偏好
(function(){
  if(localStorage.getItem('smarttourism_dark')==='1'){
    document.documentElement.classList.add('dark');
  } else if(window.matchMedia('(prefers-color-scheme:dark)').matches){
    document.documentElement.classList.add('dark');
  }
})();

// 更新 locBtn 引用
function gpsLocateAndAsk(){
  if(!navigator.geolocation){showToast('浏览器不支持GPS定位','error');return}
  const input=document.getElementById('userInput');
  const orig=input.placeholder;
  input.placeholder='定位中...';
  navigator.geolocation.getCurrentPosition(async pos=>{
    const lng=pos.coords.longitude;
    const lat=pos.coords.latitude;
    const acc=pos.coords.accuracy;
    try{
      const r=await fetch('https://restapi.amap.com/v3/geocode/regeo?key=&location='+lng+','+lat);
      const d=await r.json();
      const addr=(d.regeocode&&d.regeocode.formatted_address)||('('+lng.toFixed(4)+','+lat.toFixed(4)+')');
      input.value='我当前在'+addr+'，附近有什么好玩的地方和好吃的';
      sendMessage();
    }catch(e){
      input.value='我当前在坐标('+lng.toFixed(4)+','+lat.toFixed(4)+')附近，有什么好玩的地方和好吃的';
      sendMessage();
    }
    input.placeholder=orig;
    showToast('GPS定位精度: ±'+Math.round(acc)+'m','info');
  },err=>{
    showToast('GPS定位失败，请允许位置权限','error');
    input.placeholder=orig;
  },{enableHighAccuracy:!0,timeout:15000,maximumAge:0});
}

init();
