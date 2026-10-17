var S={provider:'deepseek',model:'deepseek-chat',apiKey:'',convId:crypto.randomUUID(),streaming:false};
var PM={deepseek:['deepseek-chat','deepseek-reasoner'],zhipu:['glm-4-plus','glm-4-flash'],qwen:['qwen-max','qwen-plus'],moonshot:['moonshot-v1-8k','moonshot-v1-32k']};
var map2=null,mapPos=null,mapMarker=null;
var JS_KEY='',WEB_KEY=''; // 高德 Key 不内置，启动时从后端 /api/map/key 读取
var DEFAULT_CENTER={lng:120.658,lat:28.012};
// 校验经纬度是否为有效数字（防止定位返回 null 导致地图中心变 NaN 而空白）
function validLL(lng,lat){return typeof lng==='number'&&isFinite(lng)&&lng>=-180&&lng<=180&&typeof lat==='number'&&isFinite(lat)&&lat>=-90&&lat<=90}

function init(){
  var s=localStorage.getItem('smarttourism_mobile');
  if(s){try{var c=JSON.parse(s);S.provider=c.provider||'deepseek';S.model=c.model||'deepseek-chat';S.apiKey=c.apiKey||''}catch(e){}}
  document.getElementById('sProvider').value=S.provider;updateModelM();
  document.getElementById('sModel').value=S.model;
  if(S.apiKey)document.getElementById('sApiKey').value=S.apiKey;
  checkConn();
}

function updateModelM(){
  var m=PM[S.provider]||['default'];
  document.getElementById('sModel').innerHTML=m.map(function(x){return'<option value="'+x+'"'+(x===S.model?' selected':'')+'>'+x+'</option>'}).join('');
}
function onProviderChangeM(){S.provider=document.getElementById('sProvider').value;S.model=PM[S.provider][0];updateModelM();}

function switchTab(t){
  document.getElementById('chatPage').classList.toggle('hidden',t!=='chat');
  document.getElementById('mapPage').classList.toggle('hidden',t!=='map');
  document.getElementById('settingsPage').classList.toggle('hidden',t!=='settings');
  ['chat','map','settings'].forEach(function(x){document.getElementById('tab'+x.charAt(0).toUpperCase()+x.slice(1)).classList.toggle('active',t===x)});
  if(t==='map'){if(!map2)initMap();else setTimeout(function(){map2.resize()},200)}
}

function checkConn(){
  fetch('/api/health').then(function(r){return r.json()}).then(function(d){
    document.getElementById('connInfo').innerHTML='✅ 服务运行中 v'+d.version+'<br>🧠 '+d.skills_loaded.length+' Skills | 🔌 '+d.plugins_configured+'/'+d.plugins_loaded+' 插件<br>🌐 '+window.location.host;
  }).catch(function(){document.getElementById('connInfo').innerHTML='⚠️ 未连接后端'});
}

function saveSettings(){
  S.provider=document.getElementById('sProvider').value;
  S.model=document.getElementById('sModel').value;
  S.apiKey=document.getElementById('sApiKey').value.trim();
  if(!S.apiKey){toast('请输入 API Key');return}
  var c={provider:S.provider,model:S.model,api_key:S.apiKey};
  localStorage.setItem('smarttourism_mobile',JSON.stringify(c));
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)})
    .then(function(r){return r.json()}).then(function(d){
      if(d.success)toast('✅ 配置已保存');else toast(d.error||'失败')
    }).catch(function(){toast('已本地保存')});
}

function sendMsg(){
  if(S.streaming)return;
  var input=document.getElementById('inputM'),msg=input.value.trim();
  if(!msg)return;
  if(!S.apiKey){toast('请先在设置中配置 API Key');switchTab('settings');return}
  // 自动同步 Key 到后端（防止重启丢失）
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:S.apiKey,provider:S.provider,model:S.model})}).catch(function(){})
  var wel=document.getElementById('welcomeM');if(wel)wel.style.display='none';
  addMsg('user',msg);input.value='';S.streaming=true;
  var ty=addTyping();
  fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,conversation_id:S.convId,stream:true})})
    .then(function(r){if(!r.ok)return r.json().then(function(e){throw new Error(e.error)});
      var reader=r.body.getReader(),decoder=new TextDecoder();ty.remove();
      var bubble=addMsg('assistant',''),bubbleDiv=bubble.querySelector('.bubble'),content='';
      function read(){reader.read().then(function(result){
        if(result.done){S.streaming=false;return}
        var text=decoder.decode(result.value,{stream:true}),lines=text.split('\n');
        for(var i=0;i<lines.length;i++){
          if(!lines[i].startsWith('data: '))continue;
          try{var j=JSON.parse(lines[i].slice(6));if(j.done){S.streaming=false;return}if(j.content){content+=j.content;bubbleDiv.innerHTML=md(content);scrollBtm()}}catch(e){}
        }
        read();
      })}
      read();
    }).catch(function(e){ty&&ty.remove();addMsg('assistant','❌ '+e.message);S.streaming=false});
}

function addMsg(role,text){
  var b=document.getElementById('chatBody'),d=document.createElement('div');
  d.className='msg '+role;
  d.innerHTML='<div class="avatar">'+(role==='user'?'👤':'🦉')+'</div><div class="bubble">'+md(text)+'</div>';
  b.appendChild(d);scrollBtm();return d;
}
function addTyping(){
  var b=document.getElementById('chatBody'),d=document.createElement('div');
  d.className='msg assistant';
  d.innerHTML='<div class="avatar">🦉</div><div class="bubble"><div class="typing"><span></span><span></span><span></span></div></div>';
  b.appendChild(d);scrollBtm();return d;
}
function md(t){
  if(!t)return'';
  var html=t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // 清理分隔线符号
  html=html.replace(/^[\s]*[-=*_~|]{3,}[\s]*$/gm,'');
  // 清理表格残留
  html=html.replace(/^\|.*\|$/gm,function(l){return l.replace(/^\|/,'').replace(/\|$/,'').replace(/\|/g,'  ·  ')});
  // 标题
  html=html.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  html=html.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  // 粗体和斜体
  html=html.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  html=html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g,'<em>$1</em>');
  // 代码
  html=html.replace(/```([^`]+)```/g,'<pre><code>$1</code></pre>');
  html=html.replace(/`([^`]+)`/g,'<code>$1</code>');
  // 按行处理列表
  var lines=html.split('\n'),result=[],inUL=false,inOL=false;
  for(var i=0;i<lines.length;i++){
    var line=lines[i];
    var ulM=line.match(/^(?:\s*)(?:-|\*|•)\s+(.+)/);
    var olM=line.match(/^(?:\s*)(\d+)\.\s+(.+)/);
    if(ulM){
      if(inOL){result.push('</ol>');inOL=false;}
      if(!inUL){result.push('<ul>');inUL=true;}
      result.push('<li>'+ulM[1]+'</li>');
    }else if(olM){
      if(inUL){result.push('</ul>');inUL=false;}
      if(!inOL){result.push('<ol>');inOL=true;}
      result.push('<li>'+olM[2]+'</li>');
    }else{
      if(inUL){result.push('</ul>');inUL=false;}
      if(inOL){result.push('</ol>');inOL=false;}
      result.push(line);
    }
  }
  if(inUL)result.push('</ul>');
  if(inOL)result.push('</ol>');
  html=result.join('\n');
  html=html.replace(/\n\n+/g,'</p><p>');
  html=html.replace(/\n/g,'<br>');
  html=html.replace(/<p>\s*<\/p>/g,'');
  return '<p>'+html+'</p>';
}
function scrollBtm(){var b=document.getElementById('chatBody');setTimeout(function(){b.scrollTop=b.scrollHeight},50)}
function quickAsk(t){document.getElementById('inputM').value=t;sendMsg()}
function clearChatM(){document.getElementById('chatBody').innerHTML='<div class="welcome" id="welcomeM"><div class="welcome-logo">🏞️</div><h2>对话已清空</h2><p>开始新对话</p></div>';S.convId=crypto.randomUUID()}

// ====== 语音输入（优先 Web Speech API；不支持时自动降级为录音+后端识别）======
var SR=window.SpeechRecognition||window.webkitSpeechRecognition;
var recogM=null,listeningM=false,voiceModeM='speech';
var mediaRecM=null,audioChunksM=[],recStreamM=null;

function toggleVoiceM(){
  if(listeningM){stopVoiceM();return}
  if(!SR){startRecorderModeM();return}
  var btn=document.getElementById('micBtnM');
  if(btn)btn.classList.add('listening');
  if(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia){
    showVoiceStatusM('正在请求麦克风权限，请在弹窗中点击允许...',false);
    navigator.mediaDevices.getUserMedia({audio:true}).then(function(stream){
      recStreamM=stream;
      startRecognizeM();
    }).catch(function(err){
      if(btn)btn.classList.remove('listening');
      hideVoiceStatusM();
      if(err&&(err.name==='NotFoundError'||err.name==='DevicesNotFoundError'||err.name==='OverconstrainedError')){toast('未检测到可用的麦克风设备，请检查麦克风连接')}
      else{showVoiceGuideM('mic-system')}
    });
  }else{startRecognizeM()}
}

function startRecognizeM(){
  if(recogM){try{recogM.abort()}catch(e){}}
  recogM=new SR();
  recogM.lang='zh-CN';
  recogM.interimResults=true;
  recogM.continuous=false;
  recogM.maxAlternatives=1;
  recogM.onstart=function(){
    listeningM=true;
    voiceModeM='speech';
    showVoiceStatusM('正在录音，请说话...',true);
  };
  recogM.onresult=function(e){
    var finalText='',interimText='';
    for(var i=0;i<e.results.length;i++){
      if(e.results[i].isFinal){finalText+=e.results[i][0].transcript}
      else{interimText+=e.results[i][0].transcript}
    }
    if(interimText){showVoiceStatusM('正在识别：'+interimText,true)}
    if(finalText){document.getElementById('inputM').value=finalText}
  };
  recogM.onerror=function(e){
    if(e.error==='not-allowed'||e.error==='service-not-allowed'||e.error==='network'||e.error==='language-not-supported'){
      switchToRecorderM();
    }else if(e.error==='no-speech'){
      toast('没有听到声音，请靠近麦克风再试');
      setMicM(false);
    }else if(e.error!=='aborted'){
      toast('语音识别失败：'+e.error);
      setMicM(false);
    }
  };
  recogM.onend=function(){
    if(voiceModeM==='recorder')return;
    if(recStreamM){recStreamM.getTracks().forEach(function(t){t.stop()});recStreamM=null}
    setMicM(false);
  };
  try{recogM.start()}catch(err){switchToRecorderM()}
}

function switchToRecorderM(){
  voiceModeM='recorder';
  if(recogM){try{recogM.abort()}catch(e){}recogM=null}
  if(!recStreamM){
    if(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia){
      showVoiceStatusM('当前浏览器不支持网页语音识别，切换到录音模式...',false);
      navigator.mediaDevices.getUserMedia({audio:true}).then(function(stream){
        recStreamM=stream;
        startRecorderModeM();
      }).catch(function(){setMicM(false);showVoiceGuideM('mic-system')});
    }else{setMicM(false);showVoiceGuideM('mic-system')}
    return;
  }
  startRecorderModeM();
}

function startRecorderModeM(){
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){setMicM(false);showVoiceGuideM('mic-system');return}
  voiceModeM='recorder';
  listeningM=true;
  audioChunksM=[];
  if(!recStreamM){
    navigator.mediaDevices.getUserMedia({audio:true}).then(function(stream){recStreamM=stream;beginRecordM()}).catch(function(){setMicM(false);showVoiceGuideM('mic-system')});
    return;
  }
  beginRecordM();
}

function beginRecordM(){
  try{
    mediaRecM=new MediaRecorder(recStreamM);
    mediaRecM.ondataavailable=function(e){if(e.data&&e.data.size>0)audioChunksM.push(e.data)};
    mediaRecM.onstop=function(){uploadAudioM()};
    mediaRecM.start();
    showVoiceStatusM('正在录音，请说话... 说完点击🎤或「停止」结束',true);
  }catch(e){toast('录音启动失败：'+e.message);setMicM(false)}
}

function stopVoiceM(){
  if(voiceModeM==='recorder'&&mediaRecM&&mediaRecM.state==='recording'){
    showVoiceStatusM('正在识别，请稍候...',true);
    mediaRecM.stop();
    return;
  }
  if(recogM){try{recogM.stop()}catch(e){}}
}

function uploadAudioM(){
  if(recStreamM){recStreamM.getTracks().forEach(function(t){t.stop()});recStreamM=null}
  if(!audioChunksM.length){setMicM(false);toast('没有录到声音');return}
  var blob=new Blob(audioChunksM,{type:'audio/webm'});
  var fd=new FormData();
  fd.append('audio',blob,'voice.webm');
  showVoiceStatusM('正在识别，请稍候...',true);
  fetch('/api/voice/recognize',{method:'POST',body:fd})
    .then(function(r){return r.json()})
    .then(function(d){
      if(d.text){document.getElementById('inputM').value=d.text;toast('识别完成，可点击发送')}
      else{toast(d.error||'识别失败')}
      setMicM(false);
    })
    .catch(function(e){toast('识别服务请求失败：'+e.message);setMicM(false)});
}

function setMicM(on){
  listeningM=on;
  var btn=document.getElementById('micBtnM');
  if(btn)btn.classList.toggle('listening',on);
  if(!on)hideVoiceStatusM();
}

function showVoiceStatusM(text,isRecording){
  var el=document.getElementById('voiceStatusM');
  if(!el)return;
  var txt=document.getElementById('voiceStatusTextM');
  if(txt)txt.textContent=text;
  if(isRecording){el.classList.add('recording')}else{el.classList.remove('recording')}
  el.style.display='flex';
}

function hideVoiceStatusM(){
  var el=document.getElementById('voiceStatusM');
  if(el)el.style.display='none';
}

function showVoiceGuideM(type){
  var reason=type==='mic-system'
    ?'系统或浏览器未允许本页面使用麦克风。<br>请在 <b>Windows 设置 → 隐私和安全性 → 麦克风</b> 中允许应用访问麦克风。'
    :'当前浏览器环境禁用了语音识别服务。<br>请使用电脑上的 <b>Edge</b> 或 <b>Chrome</b> 浏览器打开本页面，语音功能即可正常使用。';
  var ov=document.createElement('div');
  ov.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:99999;display:flex;align-items:center;justify-content:center';
  var box=document.createElement('div');
  box.style.cssText='background:#fff;color:#333;max-width:320px;width:88%;border-radius:14px;padding:22px 18px;text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.35)';
  box.innerHTML='<div style="font-size:36px">🎤</div><h3 style="margin:10px 0 8px;font-size:16px;color:#222">语音功能不可用</h3><p style="font-size:13px;line-height:1.8;color:#555">'+reason+'</p><button style="margin-top:16px;padding:9px 28px;border:none;border-radius:20px;background:#2d5a27;color:#fff;font-size:14px;cursor:pointer">知道了</button>';
  var btn=box.querySelector('button');
  btn.onclick=function(){ov.remove()};
  ov.onclick=function(e){if(e.target===ov)ov.remove()};
  ov.appendChild(box);
  document.body.appendChild(ov);
}

function gpsLocateM(){
  showLoading(true);
  // 优先使用高德官方定位
  if(typeof AMap!=='undefined'&&AMap.Geolocation){
    var geo=new AMap.Geolocation({enableHighAccuracy:true,timeout:10000,maximumAge:0,noIpLocate:0,useNative:true,GeoLocationFirst:true});
    geo.getCurrentPosition(function(status,res){
      if(status==='complete'&&res.position&&validLL(res.position.lng,res.position.lat)){
        var lng=res.position.lng,lat=res.position.lat;
        var addr=res.formattedAddress||'';
        if(addr){
          showLoading(false);
          document.getElementById('inputM').value='我当前在'+addr+'，附近有什么好玩的地方和好吃的';
          sendMsg();
        }else{
          // 高德定位未返回地址，逆地理编码补充
          fetch('https://restapi.amap.com/v3/geocode/regeo?key='+WEB_KEY+'&location='+lng+','+lat)
            .then(function(r){return r.json()})
            .then(function(d){
              showLoading(false);
              addr=(d.regeocode&&d.regeocode.formatted_address)||(lng.toFixed(4)+','+lat.toFixed(4));
              document.getElementById('inputM').value='我当前在'+addr+'，附近有什么好玩的地方和好吃的';
              sendMsg();
            }).catch(function(){
              showLoading(false);
              document.getElementById('inputM').value='我当前在'+lng.toFixed(4)+','+lat.toFixed(4)+'，附近有什么好玩的地方和好吃的';
              sendMsg();
            });
        }
      }else{showLoading(false);toast('高德定位失败，尝试GPS');gpsLocateFallback()}
    });
  }else{gpsLocateFallback()}
}
function gpsLocateFallback(){
  if(!navigator.geolocation){toast('浏览器不支持GPS');return}
  showLoading(true);
  navigator.geolocation.getCurrentPosition(function(pos){
    if(!validLL(pos.coords.longitude,pos.coords.latitude)){showLoading(false);toast('定位失败');return}
    var lng=pos.coords.longitude,lat=pos.coords.latitude;
    fetch('https://restapi.amap.com/v3/geocode/regeo?key='+WEB_KEY+'&location='+lng+','+lat)
      .then(function(r){return r.json()})
      .then(function(d){
        showLoading(false);
        var addr=(d.regeocode&&d.regeocode.formatted_address)||(lng.toFixed(4)+','+lat.toFixed(4));
        document.getElementById('inputM').value='我当前在'+addr+'，附近有什么好玩的地方和好吃的';
        sendMsg();
      }).catch(function(){showLoading(false)});
  },function(){showLoading(false);toast('定位失败')},{enableHighAccuracy:true,timeout:10000});
}

// ====== 地图 ======
function initMap(){
  var s=document.createElement('script');
  s.src='https://webapi.amap.com/maps?v=2.0&key='+JS_KEY+'&plugin=AMap.Geolocation,AMap.Geocoder';
  s.onload=function(){map2=new AMap.Map('mapContainer2',{zoom:15,center:[120.658,28.012],resizeEnable:true});relocateMap()};
  s.onerror=function(){document.getElementById('mapContainer2').innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#999;">地图加载失败</div>'};
  document.head.appendChild(s);
}
function relocateMap(){
  if(!map2){initMap();return}showLoading(true);
  // 优先使用高德官方定位
  if(typeof AMap!=='undefined'&&AMap.Geolocation){
    var geo=new AMap.Geolocation({enableHighAccuracy:true,timeout:10000,maximumAge:0,noIpLocate:0,useNative:true,GeoLocationFirst:true});
    geo.getCurrentPosition(function(status,res){
      if(status==='complete'&&res.position&&validLL(res.position.lng,res.position.lat)){
        mapPos={lng:res.position.lng,lat:res.position.lat};
        setMapCenter(mapPos.lng,mapPos.lat);reverseGeo(mapPos.lng,mapPos.lat);
        showLoading(false);setTimeout(function(){searchMap()},500);
      }else{showLoading(false);relocateFallback()}
    });
  }else{relocateFallback()}
}
function relocateFallback(){
  if(!navigator.geolocation){showLoading(false);ipLocateMap();return}
  showLoading(true);
  navigator.geolocation.getCurrentPosition(function(pos){
    if(!validLL(pos.coords.longitude,pos.coords.latitude)){showLoading(false);ipLocateMap();return}
    mapPos={lng:pos.coords.longitude,lat:pos.coords.latitude};
    setMapCenter(mapPos.lng,mapPos.lat);reverseGeo(mapPos.lng,mapPos.lat);
    showLoading(false);setTimeout(function(){searchMap()},500);
  },function(){showLoading(false);ipLocateMap()},{enableHighAccuracy:true,timeout:10000});
}
function ipLocateMap(){
  fetch('https://restapi.amap.com/v3/ip?key='+WEB_KEY).then(function(r){return r.json()}).then(function(d){
    var ok=d.status==='1'&&d.rectangle;
    var lng=DEFAULT_CENTER.lng,lat=DEFAULT_CENTER.lat;
    if(ok){
      var rect=d.rectangle.split(';')[0].split(',');
      lng=(parseFloat(rect[0])+parseFloat(rect[2]))/2;
      lat=(parseFloat(rect[1])+parseFloat(rect[3]))/2;
    }
    if(!validLL(lng,lat)){lng=DEFAULT_CENTER.lng;lat=DEFAULT_CENTER.lat}
    mapPos={lng:lng,lat:lat};
    setMapCenter(lng,lat);
    document.getElementById('mapAddrText').textContent=ok?(d.city||d.province):'温州市';
    document.getElementById('mapAddrBar').style.display='flex';
    if(!ok)toast('定位失败，已显示默认城市');
    searchMap();
  }).catch(function(){
    mapPos={lng:DEFAULT_CENTER.lng,lat:DEFAULT_CENTER.lat};
    setMapCenter(mapPos.lng,mapPos.lat);searchMap();
  });
}
function setMapCenter(lng,lat){
  if(!map2||!validLL(lng,lat))return;map2.setCenter([lng,lat]);map2.setZoom(15);
  if(mapMarker)mapMarker.setMap(null);
  mapMarker=new AMap.Marker({position:[lng,lat],map:map2,title:'我的位置',draggable:true,icon:new AMap.Icon({size:new AMap.Size(30,42),image:'https://webapi.amap.com/theme/v1.3/markers/n/mark_r.png',imageSize:new AMap.Size(30,42)}),anchor:'bottom-center',zIndex:100});
  mapMarker.on('dragend',function(e){var p=e.target.getPosition();mapPos.lng=p.getLng();mapPos.lat=p.getLat();reverseGeo(p.getLng(),p.getLat());setTimeout(function(){searchMap()},300)});
}
function reverseGeo(lng,lat){
  fetch('https://restapi.amap.com/v3/geocode/regeo?key='+WEB_KEY+'&location='+lng+','+lat).then(function(r){return r.json()}).then(function(d){
    if(d.status==='1'&&d.regeocode){document.getElementById('mapAddrText').textContent=d.regeocode.formatted_address;document.getElementById('mapAddrBar').style.display='flex'}
  });
}
function searchMap(){
  if(!mapPos){toast('请先定位');return}
  var kw=document.getElementById('mapSearchInput').value.trim()||document.getElementById('mapSearchType').value;
  showLoading(true);
  fetch('https://restapi.amap.com/v3/place/around?key='+WEB_KEY+'&location='+mapPos.lng+','+mapPos.lat+'&keywords='+encodeURIComponent(kw)+'&radius=5000&offset=15&extensions=all')
    .then(function(r){return r.json()}).then(function(d){
      showLoading(false);
      if(d.status==='1'&&d.pois){renderMapResults(d.pois,kw)}
    }).catch(function(){showLoading(false)});
}
function renderMapResults(pois,kw){
  document.getElementById('mapPanelTitle').textContent=kw+' 周边 ('+pois.length+')';
  var list=document.getElementById('mapPanelList');list.innerHTML='';
  (map2._markers||[]).forEach(function(m){m.setMap(null)});map2._markers=[];
  pois.forEach(function(p,i){
    var div=document.createElement('div');div.className='map-item';
    div.innerHTML='<span class="idx">'+(i+1)+'</span><div class="info"><div class="name">'+p.name+(p.biz_ext&&p.biz_ext.rating?' ⭐'+p.biz_ext.rating:'')+'</div><div class="addr">'+(p.distance?p.distance+'m | ':'')+p.address+'</div></div>';
    div.onclick=function(){var loc=p.location.split(',');map2.setCenter([parseFloat(loc[0]),parseFloat(loc[1])]);map2.setZoom(17)};
    list.appendChild(div);
    var loc=p.location.split(','),mk=new AMap.Marker({position:[parseFloat(loc[0]),parseFloat(loc[1])],map:map2,title:p.name,label:{content:''+(i+1),offset:new AMap.Pixel(-8,-10)}});
    map2._markers.push(mk);
  });
  document.getElementById('mapPanel').classList.add('show');
}
function closeMapPanel(){document.getElementById('mapPanel').classList.remove('show')}

function toast(msg){var t=document.createElement('div');t.className='toast';t.textContent=msg;document.body.appendChild(t);setTimeout(function(){t.remove()},2500)}
function showLoading(s){document.getElementById('loadingOverlay').classList.toggle('show',s)}
window.onload=function(){fetch('/api/map/key').then(function(r){return r.json()}).then(function(d){JS_KEY=d.key||'';WEB_KEY=d.key||'';init()}).catch(function(){JS_KEY='';WEB_KEY='';init()})};
