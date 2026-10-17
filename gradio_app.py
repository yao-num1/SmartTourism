"""
SmartTourism · Gradio 前端 v3.1
"智旅灵犀" 文旅智能助手 — Impeccable Design System

设计系统：Impeccable 3.9.1
色彩策略：Restrained · OKLCH · seed-162 sky blue
场景：明亮环境（酒店大堂/咖啡馆/手机）— 纯白底 + 天空蓝 accent
字体：system-ui stack 单族
运动：150-250ms · state-only · no page-load choreography
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from typing import Any, Generator, List, Dict, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gradio as gr

# ==================== 配置 ====================

BACKEND_URL = os.environ.get("SMARTTOURISM_BACKEND", "http://localhost:5050")
AMAP_JS_KEY = ""

QUICK_SCENES = [
    ("行程规划", "帮我规划一个3天的行程"),
    ("酒店推荐", "推荐几家性价比高的酒店"),
    ("美食探索", "附近有什么特色美食推荐"),
    ("营销文案", "帮我写一篇景区推广文案"),
]

CROWD_CHOICES = ["自动识别", "亲子家庭", "情侣蜜月", "银发一族", "公司团建", "大学生", "商务出差"]
CROWD_IDS = ["", "family", "couple", "senior", "team_building", "student", "business"]

PROVIDER_CHOICES = ["deepseek", "zhipu", "qwen", "moonshot"]
PROVIDER_LABELS = ["DeepSeek", "智谱 GLM", "通义千问", "Moonshot Kimi"]

# ==================== Impeccable OKLCH Design Tokens ====================
# seed-162: oklch(0.55 0.091 210) sky blue
# Mood: "Travel planning kiosk in a bright hotel lobby — 
#         polished terrazzo floors, diffused morning light, 
#         crisp white surfaces, one confident blue mark"

DESIGN_TOKENS = """
:root {
  /* Surface architecture — PURE white (DEFAULT A) */
  --bg:         oklch(1.000 0.000 0);
  --surface:    oklch(0.965 0.002 210);
  --surface-alt: oklch(0.935 0.003 210);

  /* Ink — near-black with brand coolness */
  --ink:        oklch(0.15 0.005 250);
  --ink-muted:  oklch(0.48 0.005 240);
  --ink-subtle: oklch(0.65 0.003 235);

  /* Primary — seed-162 sky blue, mid-luminance for presence */
  --primary:       oklch(0.55 0.091 210);
  --primary-hover: oklch(0.50 0.095 210);
  --primary-press: oklch(0.45 0.098 210);
  --primary-bg:    oklch(0.96 0.015 210);

  /* Accent — warm amber, distinct hue+luminance from primary */
  --accent:       oklch(0.62 0.090 70);
  --accent-hover: oklch(0.57 0.095 70);
  --accent-bg:    oklch(0.96 0.025 75);

  /* Semantic states */
  --success: oklch(0.56 0.095 160);
  --warning: oklch(0.62 0.100 85);
  --error:   oklch(0.52 0.115 25);

  /* Borders */
  --border:       oklch(0.88 0.005 240);
  --border-hover: oklch(0.60 0.050 210);
  --border-focus: oklch(0.55 0.091 210);

  /* Miscellaneous */
  --radius:  12px;
  --radius-sm: 8px;
  --shadow-sm: 0 1px 3px oklch(0 0 0 / 0.06);
  --shadow:    0 4px 16px oklch(0 0 0 / 0.08);
  --shadow-lg: 0 8px 32px oklch(0 0 0 / 0.12);
  --transition: 180ms ease;
  --transition-slow: 250ms ease;
}
"""

GLOBAL_CSS = (
    DESIGN_TOKENS
    + """
/* ===== FOUNDATION ===== */
body, html {
  background: var(--bg);
  color: var(--ink);
  font-family: system-ui, -apple-system, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

.gradio-container {
  max-width: 100% !important;
  background: var(--bg) !important;
  font-family: inherit !important;
}

footer { display: none !important; }

/* Hide defaults */
.block.padded, .form > * {
  border: none !important;
  box-shadow: none !important;
  background: transparent !important;
}

/* ===== BUTTONS ===== */
.gr-button, button {
  border-radius: var(--radius-sm) !important;
  font-weight: 500 !important;
  font-size: 14px !important;
  letter-spacing: -0.01em;
  transition: background var(--transition), box-shadow var(--transition), transform var(--transition);
  cursor: pointer;
  border: none !important;
}

button.primary, .gr-button-primary {
  background: var(--primary) !important;
  color: white !important;
  padding: 10px 20px !important;
  font-weight: 600 !important;
}
button.primary:hover {
  background: var(--primary-hover) !important;
  box-shadow: var(--shadow) !important;
}
button.primary:active {
  background: var(--primary-press) !important;
  transform: scale(0.98);
}

button.secondary, .gr-button-secondary {
  background: var(--surface) !important;
  color: var(--ink-muted) !important;
  border: 1px solid var(--border) !important;
  padding: 8px 16px !important;
}
button.secondary:hover {
  background: var(--surface-alt) !important;
  color: var(--ink) !important;
  border-color: var(--border-hover) !important;
}

/* ===== INPUTS ===== */
textarea, input[type="text"], input[type="password"],
.gr-textbox textarea, .gr-textbox input {
  background: var(--bg) !important;
  color: var(--ink) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius-sm) !important;
  font-family: inherit !important;
  font-size: 15px !important;
  transition: border-color var(--transition), box-shadow var(--transition);
  padding: 10px 14px !important;
}
textarea:focus, input:focus {
  border-color: var(--border-focus) !important;
  box-shadow: 0 0 0 3px var(--primary-bg) !important;
  outline: none !important;
}

/* ===== DROPDOWN ===== */
.gr-dropdown select, .gr-radio {
  background: var(--bg) !important;
  color: var(--ink) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius-sm) !important;
  font-family: inherit !important;
}

/* ===== CHATBOT ===== */
.chatbot, [class*="chatbot"], [data-testid="chatbot"] {
  background: var(--bg) !important;
  border-radius: var(--radius) !important;
}

.chatbot .message-wrap {
  background: var(--bg) !important;
}

.message.user .bubble {
  background: var(--primary) !important;
  color: white !important;
  border-radius: 14px 14px 4px 14px !important;
  padding: 10px 16px !important;
  font-size: 15px !important;
  line-height: 1.5 !important;
}
.message.bot .bubble {
  background: var(--surface) !important;
  color: var(--ink) !important;
  border-radius: 14px 14px 14px 4px !important;
  padding: 10px 16px !important;
  font-size: 15px !important;
  line-height: 1.6 !important;
  border: 1px solid var(--border) !important;
}
.message.bot .bubble code {
  font-size: 13px;
  background: var(--surface-alt);
  border-radius: 4px;
  padding: 2px 6px;
}

/* ===== GROUP / PANEL ===== */
.gr-group, .gr-panel, .panel {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius) !important;
}

/* ===== ACCORDION ===== */
.gr-accordion {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius) !important;
}
.gr-accordion .label-text {
  color: var(--ink) !important;
  font-weight: 600 !important;
}

/* ===== SCROLLBAR ===== */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: oklch(0.82 0.005 240); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: oklch(0.70 0.010 240); }

/* ===== GRADIO MARKDOWN ===== */
.gradio-container .prose, .gradio-container p {
  color: var(--ink) !important;
}
.gradio-container h1, h2, h3 {
  color: var(--ink) !important;
  font-weight: 600 !important;
  letter-spacing: -0.02em;
}

/* ===== CUSTOM COMPONENTS ===== */
.brand-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 0;
}
.brand-logo {
  width: 36px; height: 36px;
  border-radius: var(--radius-sm);
  background: var(--primary);
  display: flex; align-items: center; justify-content: center;
  font-size: 18px; color: white; font-weight: 700;
}
.brand-title {
  font-size: 22px; font-weight: 700;
  color: var(--ink);
  letter-spacing: -0.03em;
}
.brand-subtitle {
  font-size: 12px; color: var(--ink-subtle);
  margin-top: 1px;
}

.progress-bar {
  display: flex; gap: 0; padding: 8px 0 16px;
}
.progress-step {
  flex: 1; text-align: center; padding: 8px 0;
  font-size: 13px; font-weight: 500;
  color: var(--ink-subtle);
  border-bottom: 2px solid var(--border);
  transition: border-color var(--transition), color var(--transition);
}
.progress-step.active {
  color: var(--primary);
  border-bottom-color: var(--primary);
  font-weight: 600;
}
.progress-step.done {
  color: var(--success);
  border-bottom-color: var(--success);
}

/* Scene buttons row */
.scene-row button {
  font-size: 13px !important;
  padding: 8px 14px !important;
  flex: 1 1 0 !important;
}

/* Map container */
.map-wrap {
  border-radius: var(--radius);
  overflow: hidden;
  border: 1px solid var(--border);
  height: 100%;
  min-height: 560px;
}
#map-container, #map-container .html-container, #map-container iframe {
  height: 560px !important;
  min-height: 560px !important;
}
#map-container .html-container {
  height: 100% !important;
  border-radius: var(--radius);
  overflow: hidden;
}

/* Chat column */
.chat-column {
  min-width: 420px;
}
"""
)

# ==================== MAP HTML ====================

MAP_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<style>
*{margin:0;padding:0;box-sizing:border-box;}
html,body,#container{width:100%;height:100%;font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;}
#container{position:relative;border-radius:12px;overflow:hidden;}
#map-controls{position:absolute;top:12px;right:12px;z-index:1000;
  background:white;border-radius:8px;padding:6px;
  border:1px solid oklch(0.88 0.005 240);box-shadow:0 2px 8px oklch(0 0 0/0.08);}
.ctrl-btn{display:block;width:100%;padding:6px 12px;margin:2px 0;
  border:1px solid oklch(0.92 0.003 240);background:white;
  border-radius:6px;cursor:pointer;font-size:12px;color:oklch(0.48 0.005 240);
  font-family:inherit;transition:all 180ms;}
.ctrl-btn:hover{background:oklch(0.96 0.015 210);border-color:oklch(0.55 0.091 210);}
#marker-count{display:block;margin-top:6px;color:oklch(0.65 0.003 235);font-size:11px;text-align:center;}
</style></head><body>
<div id="container"></div>
<div id="map-controls">
<button class="ctrl-btn" onclick="fitAllMarkers()">查看全部点位</button>
<button class="ctrl-btn" onclick="clearAllMarkers()">清除标记</button>
<span id="marker-count"></span></div>
<script>
var AMAP_KEY='__AMAP_KEY__';
var s=document.createElement('script');
s.src='https://webapi.amap.com/maps?v=2.0&key='+AMAP_KEY+'&plugin=AMap.Geocoder,AMap.AutoComplete,AMap.PlaceSearch';
s.onload=initMap;document.head.appendChild(s);
var map,markers=[],infoWins=[],geocoder,placeSearch;
function initMap(){
 map=new AMap.Map('container',{zoom:5,center:[116.397428,39.90923],resizeEnable:true});
 geocoder=new AMap.Geocoder({map:map});
 placeSearch=new AMap.PlaceSearch({map:map});
 map.plugin('AMap.CitySearch',function(){var cs=new AMap.CitySearch();cs.getLocalCity(function(s,r){if(s==='complete'&&r.info==='OK'){map.setCity(r.city);map.setZoom(12);}});});
 window.addMarker=function(n,lng,lat,info,type){
  var icons={'景点':'🏛️','酒店':'🛏️','餐饮':'🍽️','交通':'🚕','购物':'🛍️'};
  var m=new AMap.Marker({position:[lng,lat],title:n,content:'<div style="font-size:24px;filter:drop-shadow(0 2px 3px rgba(0,0,0,0.3))">'+(icons[type]||'📍')+'</div>',offset:new AMap.Pixel(-14,-14)});
  var iw=new AMap.InfoWindow({content:'<div style="padding:8px 12px;font-size:13px;font-family:inherit"><strong style="font-size:14px">'+n+'</strong>'+(info?'<br/><span style="color:oklch(0.65 0.003 235)">'+info+'</span>':'')+'</div>',offset:new AMap.Pixel(0,-34)});
  m.on('click',function(){iw.open(map,m.getPosition());});
  m.setMap(map);markers.push(m);infoWins.push(iw);updateMC();return markers.length;};
 window.clearAllMarkers=function(){markers.forEach(function(m){m.setMap(null)});infoWins.forEach(function(i){i.close()});markers=[];infoWins=[];updateMC();};
 window.fitAllMarkers=function(){if(markers.length===0)return;map.setFitView(markers);};
 window.setMapCenter=function(lng,lat,z){map.setZoomAndCenter(z||14,[lng,lat]);};
 window.aroundSearch=function(kw,lng,lat,r){placeSearch.searchNearBy(kw,[lng,lat],r||5000,function(s,r){if(s==='complete'&&r.info==='OK'){var pois=r.poiList.pois;clearAllMarkers();for(var k=0;k<Math.min(pois.length,15);k++){var p=pois[k],tn='景点';if((kw||'').indexOf('酒店')>=0||(kw||'').indexOf('住宿')>=0)tn='酒店';else if((kw||'').indexOf('餐饮')>=0||(kw||'').indexOf('美食')>=0)tn='餐饮';addMarker(p.name,p.location.lng,p.location.lat,p.address||'',tn);}fitAllMarkers();}});};
 function updateMC(){var el=document.getElementById('marker-count');if(el)el.textContent='共 '+markers.length+' 个点位';}
}
</script></body></html>
"""

BRAND_HEADER = """
<div class="brand-header">
  <div class="brand-logo">灵</div>
  <div>
    <div class="brand-title">智旅灵犀</div>
    <div class="brand-subtitle">SmartTourism AI</div>
  </div>
</div>
"""

PROGRESS_HTML = """
<div class="progress-bar">
  <div class="progress-step active" id="st-consult">咨询</div>
  <div class="progress-step" id="st-plan">规划</div>
  <div class="progress-step" id="st-marketing">营销</div>
  <div class="progress-step" id="st-cultural">文创</div>
</div>
"""

# ==================== API ====================

def fetch_backend(path: str, method: str = "GET", data: dict = None) -> dict:
    url = f"{BACKEND_URL}{path}"
    try:
        if method == "GET":
            resp = requests.get(url, timeout=30)
        else:
            resp = requests.post(url, json=data, timeout=180)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.ConnectionError:
        return {"error": f"后端服务未启动 ({BACKEND_URL})"}
    except requests.exceptions.Timeout:
        return {"error": "请求超时"}
    except Exception as e:
        return {"error": str(e)}

def get_amap_key() -> str:
    r = fetch_backend("/api/map/key")
    return r.get("key", "") if isinstance(r, dict) else ""

# ==================== STREAMING ====================

def stream_chat(
    message, history, skill, crowd_id, dest, conv_id, api_key, provider,
) -> Generator[Tuple[List, str], None, None]:
    if not message or not message.strip():
        yield history, conv_id
        return
    if not conv_id:
        conv_id = str(uuid.uuid4())
    if api_key:
        try:
            requests.post(f"{BACKEND_URL}/api/config", json={"api_key": api_key, "provider": provider}, timeout=10)
        except: pass

    payload = {
        "message": message, "skill": skill or "", "crowd_id": crowd_id or "",
        "destination": dest or "", "conversation_id": conv_id, "stream": True,
    }
    history = list(history) if history else []
    history.append({"role": "user", "content": message})

    try:
        resp = requests.post(f"{BACKEND_URL}/api/chat", json=payload, stream=True, timeout=300)
        resp.raise_for_status()
        acc = ""
        tmp = list(history) + [{"role": "assistant", "content": ""}]
        for line in resp.iter_lines():
            if not line: continue
            ls = line.decode("utf-8")
            if ls.startswith("data: "):
                ds = ls[6:]
                if ds == "[DONE]": break
                try:
                    ck = json.loads(ds)
                    if "content" in ck:
                        acc += ck["content"]
                        tmp[-1] = {"role": "assistant", "content": acc}
                        yield tmp, conv_id
                except: continue
        final = list(history) + [{"role": "assistant", "content": acc}]
        yield final, conv_id
    except Exception as e:
        final = list(history) + [{"role": "assistant", "content": f"请求失败：{str(e)[:100]}"}]
        yield final, conv_id

# ==================== POI / MAP ====================

def extract_pois(reply: str) -> List[Dict]:
    if not reply: return []
    pois, seen = [], set()
    jm = re.search(r'```json\s*([\s\S]*?)```', reply)
    if jm:
        try:
            data = json.loads(jm.group(1))
            for key in ["pois", "poi_list", "daily_routes"]:
                raw = data.get(key, [])
                for item in (raw if isinstance(raw, list) else []):
                    if isinstance(item, dict):
                        if "pois" in item:
                            for p in item["pois"]:
                                n = p.get("poi_name") or p.get("name", "")
                                if n and n not in seen:
                                    seen.add(n); pois.append({"name": n, "type": p.get("type", "景点")})
                        else:
                            n = item.get("poi_name") or item.get("name", "")
                            if n and n not in seen:
                                seen.add(n); pois.append({"name": n, "type": item.get("type", "景点")})
        except: pass
    if not pois:
        for m in re.finditer(r'\*\*(.{2,20})\*\*', reply):
            n = m.group(1).strip()
            if n not in seen and len(n) >= 2 and not re.match(r'^[\d,.]+$', n):
                seen.add(n); pois.append({"name": n, "type": "景点"})
    return pois[:10]

def geocode_pois(pois: List[Dict], city: str = "") -> List[Dict]:
    if not pois: return []
    key = get_amap_key()
    if not key: return pois
    for poi in pois:
        try:
            addr = f"{city}{poi['name']}" if city else poi['name']
            r = requests.get(f"https://restapi.amap.com/v3/geocode/geo?key={key}&address={addr}&city={city}", timeout=5)
            d = r.json()
            if d.get("status") == "1" and d.get("geocodes"):
                loc = d["geocodes"][0]["location"].split(",")
                poi["lng"], poi["lat"] = float(loc[0]), float(loc[1])
        except: pass
    return pois

def map_js(markers: List[Dict]) -> str:
    if not markers: return ""
    lines = []
    for m in markers:
        n = str(m.get("name", "")).replace("'", "\\'")
        lines.append(f"addMarker('{n}', {m.get('lng',0)}, {m.get('lat',0)}, '{m.get('type','')}', '{m.get('type','')}');")
    if lines:
        lines.append("fitAllMarkers();")
        return f'<script>(function(){{if(window.addMarker){{{";".join(lines)}}}}})()</script>'
    return ""

# ==================== BUILD UI ====================

def build_ui() -> gr.Blocks:
    global AMAP_JS_KEY
    AMAP_JS_KEY = get_amap_key() or "YOUR_AMAP_KEY"
    mh = MAP_HTML.replace("__AMAP_KEY__", AMAP_JS_KEY)

    with gr.Blocks(
        css=GLOBAL_CSS,
        title="智旅灵犀 · SmartTourism",
        js="""
        function fix() {
            // 隐藏 Chatbot 标签
            document.querySelectorAll('.label-wrap').forEach(el => {
                if (el.textContent.includes('Chatbot') || el.textContent.includes('对话')) el.style.display = 'none';
            });
            // 强制地图容器高度
            var mc = document.getElementById('map-container');
            if (mc) {
                mc.style.height = '560px';
                mc.style.minHeight = '560px';
                var inner = mc.querySelector('.html-container, iframe, div');
                if (inner) inner.style.height = '100%';
            }
        }
        fix(); setInterval(fix, 2500);
        """,
    ) as demo:
        conv_state = gr.State("")

        gr.HTML(BRAND_HEADER)
        gr.HTML(PROGRESS_HTML)

        with gr.Row(equal_height=True):
            with gr.Column(scale=2, min_width=420, elem_classes="chat-column"):
                chatbot = gr.Chatbot(value=[], label=None, height=560, render_markdown=True, layout="bubble")

                with gr.Row(elem_classes="scene-row"):
                    scene_btns = {}
                    for label, prompt in QUICK_SCENES:
                        scene_btns[label] = gr.Button(label, size="sm", scale=1)

                with gr.Row():
                    import_input = gr.Textbox(placeholder="粘贴外部链接，AI 自动解析行程...", label=None, show_label=False, scale=5)
                    import_btn = gr.Button("导入", size="sm", scale=1, variant="secondary")
                    clear_btn = gr.Button("清空", size="sm", scale=1, variant="secondary")

                with gr.Row():
                    msg_input = gr.Textbox(
                        placeholder="输入文旅需求... Enter 发送",
                        label=None, show_label=False, scale=6, lines=2, max_lines=4)
                    send_btn = gr.Button("发送", variant="primary", scale=1)

            with gr.Column(scale=3, min_width=500):
                with gr.Group():
                    map_comp = gr.HTML(value=mh, elem_classes="map-wrap", elem_id="map-container")

        with gr.Accordion("设置", open=False):
            with gr.Row():
                api_key = gr.Textbox(placeholder="DeepSeek API Key...", label="API Key", type="password", scale=3)
                provider = gr.Dropdown(list(zip(PROVIDER_LABELS, PROVIDER_CHOICES)), value="deepseek", label="模型供应商", scale=1)
                crowd = gr.Dropdown(list(zip(CROWD_CHOICES, CROWD_IDS)), value="", label="客群", scale=1)
            with gr.Row():
                dest_input = gr.Textbox(placeholder="杭州、三亚", label="默认目的地", scale=2)
                skill_dd = gr.Dropdown([("自动", ""), ("咨询", "tourism-advisor"), ("营销", "tourism-marketing"), ("匹配", "smart-matching")], value="", label="Skill", scale=1)
                refresh_btn = gr.Button("加载配置", size="sm", scale=1, variant="secondary")

        map_js_runner = gr.HTML(value="", visible=False)

        # events
        def send(msg, hist, sk, cr, dst, cid, ak, pv):
            if not msg or not msg.strip():
                yield hist, cid; return
            for h, c in stream_chat(msg, hist, sk, cr, dst, cid, ak, pv):
                yield h, c

        si = [msg_input, chatbot, skill_dd, crowd, dest_input, conv_state, api_key, provider]
        so = [chatbot, conv_state]
        msg_input.submit(fn=send, inputs=si, outputs=so, queue=True)
        send_btn.click(fn=send, inputs=si, outputs=so, queue=True)

        def clr(cid):
            if cid:
                try: fetch_backend(f"/api/conversations/{cid}", "DELETE")
                except: pass
            return [], str(uuid.uuid4())

        clear_btn.click(fn=clr, inputs=[conv_state], outputs=[chatbot, conv_state])

        for label, prompt in QUICK_SCENES:
            scene_btns[label].click(fn=lambda p=prompt: p, outputs=[msg_input])

        def do_import(url, cid, ak, pv):
            if not url or not url.strip():
                return chatbot.value, cid
            cid = cid or str(uuid.uuid4())
            if ak:
                try: requests.post(f"{BACKEND_URL}/api/config", json={"api_key": ak, "provider": pv}, timeout=10)
                except: pass
            try:
                r = requests.post(f"{BACKEND_URL}/api/chat", json={
                    "message": f"分析以下内容，提取目的地/景点/酒店/美食，做简要摘要：\n{url}",
                    "conversation_id": cid, "stream": False}, timeout=180).json()
                reply = r.get("reply", "解析完成")
                hist = list(chatbot.value) if chatbot.value else []
                hist.append({"role": "user", "content": f"导入外部链接"})
                hist.append({"role": "assistant", "content": reply})
                return hist, cid
            except Exception as e:
                return chatbot.value, cid

        import_btn.click(fn=do_import, inputs=[import_input, conv_state, api_key, provider], outputs=[chatbot, conv_state])

        def upd_map(cv):
            if not cv: return ""
            last = cv[-1] if cv else None
            if not last: return ""
            reply = last[1] if len(last) > 1 else ""
            if not reply: return ""
            pois = extract_pois(reply)
            if not pois: return ""
            pois = geocode_pois(pois, dest_input.value or "")
            return map_js(pois)

        chatbot.change(fn=upd_map, inputs=[chatbot], outputs=[map_js_runner])

    return demo

def main():
    import argparse
    p = argparse.ArgumentParser(description="智旅灵犀 Gradio 前端")
    p.add_argument("--port", type=int, default=7860)
    p.add_argument("--share", action="store_true")
    p.add_argument("--host", type=str, default="127.0.0.1")
    args = p.parse_args()

    print("=" * 60)
    print("🏯 智旅灵犀 SmartTourism · v3.1 Impeccable")
    print(f"🔗 {BACKEND_URL}  |  🌐 http://{args.host}:{args.port}")
    print("=" * 60)

    demo = build_ui()
    demo.queue(default_concurrency_limit=5, max_size=20)
    demo.launch(server_name=args.host, server_port=args.port, share=args.share, show_error=True, css=GLOBAL_CSS)

if __name__ == "__main__":
    main()
