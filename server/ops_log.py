"""
SmartTourism · 轻量运维统计（Ops Log）
========================================
零依赖、纯内存的运行时指标收集，便于后台「发现问题 + 维护」：
  - 每接口请求数 / 平均耗时 / 最大耗时 / 5xx 错误数
  - 全局请求数、错误率、状态码分布、静态资源跳过数
  - 最近 N 条错误明细（含 request_id，可与 app.log/error.log 关联）

设计要点：
  - 线程安全（Flask 多线程）；用锁保护所有可变状态。
  - 端点基数保护：把 UUID/纯数字路径段归一成 :id，并限制最多跟踪 MAX_ENDPOINTS 个端点，
    防止恶意/异常路径撑爆内存。
  - 内存指标重启即清零；但后台线程会周期性把精简快照落盘到 data/logs/ops-YYYYMMDD.jsonl，
    并在近期错误率超阈值时记录 WARNING 告警，便于事后排查。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

# UUID 或纯数字路径段 → 归一为 :id，避免 /api/x/<uuid> 造成端点无限膨胀
_ID_SEG = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|\d+)$"
)

# 静态资源/噪音：不计入运维指标，也不写访问日志（否则 app.log 会被图片请求淹没）
_STATIC_PREFIX = ("/static/", "/assets/")
_STATIC_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".css", ".js", ".map", ".woff", ".woff2", ".ttf", ".mp4", ".webm",
)


def normalize_path(path: str) -> str:
    """把动态路径段（UUID/数字）归一为 :id，稳定端点聚合键。"""
    segs = str(path or "").split("/")
    return "/".join(":id" if _ID_SEG.match(s) else s for s in segs)


def is_static_noise(path: str) -> bool:
    """判断是否为静态资源/噪音请求（不计入运维指标与访问日志）。"""
    p = str(path or "")
    if p.startswith(_STATIC_PREFIX):
        return True
    if p == "/favicon.ico":
        return True
    low = p.lower().split("?", 1)[0]
    return low.endswith(_STATIC_EXT)


class OpsStats:
    """内存态运行指标收集器。"""

    MAX_ENDPOINTS = 300          # 端点基数上限
    MAX_ERROR_MSG = 300          # 单条错误消息截断长度

    def __init__(self, max_errors: int = 200) -> None:
        self._lock = threading.Lock()
        self._start = time.time()
        self.total_requests = 0
        self.total_errors = 0        # 仅统计 5xx（由 record 累加，record_error 不重复计）
        self.static_skipped = 0
        self._by_status: Dict[int, int] = defaultdict(int)
        self._ep: Dict[str, Dict[str, Any]] = {}
        self._errors: deque = deque(maxlen=max_errors)
        # 告警窗口：自上次评估以来的请求/错误数（评估后清零，反映「近期」流量）
        self._win_requests = 0
        self._win_errors = 0
        self._last_alert_ts = 0.0
        self._bg_started = False

    # ---- 采集 ----

    def record(self, method: str, path: str, status: int, duration_ms: float) -> None:
        """记录一次请求（非静态）。status>=500 计入错误。"""
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = 0
        duration_ms = float(duration_ms or 0.0)
        key = f"{method} {normalize_path(path)}"
        with self._lock:
            self.total_requests += 1
            self._by_status[status] += 1
            self._win_requests += 1
            if status >= 500:
                self._win_errors += 1
            # 基数保护：超过上限且是新端点则不再新增（仍计入总量）
            if key not in self._ep and len(self._ep) >= self.MAX_ENDPOINTS:
                return
            e = self._ep.setdefault(
                key, {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "errors": 0}
            )
            e["count"] += 1
            e["total_ms"] += duration_ms
            if duration_ms > e["max_ms"]:
                e["max_ms"] = duration_ms
            if status >= 500:
                e["errors"] += 1
                self.total_errors += 1

    def record_error(
        self, path: str, message: str, request_id: str = "",
        status: int = 500, exc_type: str = "",
    ) -> None:
        """记录一条错误明细（供「最近错误」列表；不重复累加 total_errors）。"""
        with self._lock:
            self._errors.append({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "path": normalize_path(path),
                "message": str(message)[: self.MAX_ERROR_MSG],
                "request_id": request_id or "",
                "status": status,
                "exc_type": exc_type or "",
            })

    def skip_static(self) -> None:
        with self._lock:
            self.static_skipped += 1

    # ---- 汇总 ----

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            eps: List[Dict[str, Any]] = []
            for k, v in self._ep.items():
                avg = (v["total_ms"] / v["count"]) if v["count"] else 0.0
                eps.append({
                    "endpoint": k,
                    "count": v["count"],
                    "avg_ms": round(avg, 1),
                    "max_ms": round(v["max_ms"], 1),
                    "errors": v["errors"],
                })
            tr = self.total_requests
            return {
                "uptime_s": round(time.time() - self._start, 1),
                "total_requests": tr,
                "total_errors": self.total_errors,
                "error_rate": round(self.total_errors / tr, 4) if tr else 0.0,
                "static_skipped": self.static_skipped,
                "by_status": {str(k): v for k, v in sorted(self._by_status.items())},
                "busiest": sorted(eps, key=lambda x: x["count"], reverse=True)[:20],
                "slowest": sorted(eps, key=lambda x: x["avg_ms"], reverse=True)[:15],
                "most_errors": sorted(
                    [e for e in eps if e["errors"]], key=lambda x: x["errors"], reverse=True
                )[:15],
                "recent_errors": list(self._errors)[-50:][::-1],
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

    # ---- 告警与持久化 ----

    def evaluate_alerts(
        self,
        logger=None,
        error_rate_threshold: float = 0.05,
        min_requests: int = 20,
        cooldown_s: float = 300.0,
    ) -> Optional[Dict[str, Any]]:
        """评估「近期窗口」错误率，超阈值则记录一条 WARNING 告警。

        - 窗口请求数不足 min_requests 时不评估（样本太小，避免误报）。
        - 每次评估后清零窗口，使告警反映最近流量而非全量历史。
        - cooldown_s 内不重复告警，避免刷屏。
        返回告警字典（未触发时返回 None）。
        """
        now = time.time()
        with self._lock:
            wr, we = self._win_requests, self._win_errors
            if wr < min_requests:
                return None
            rate = (we / wr) if wr else 0.0
            # 无论是否告警，评估后都清零窗口，聚焦「近期」
            self._win_requests = 0
            self._win_errors = 0
            if rate <= error_rate_threshold:
                return None
            if now - self._last_alert_ts < cooldown_s:
                return None
            self._last_alert_ts = now
            alert = {
                "type": "high_error_rate",
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "window_requests": wr,
                "window_errors": we,
                "error_rate": round(rate, 4),
                "threshold": error_rate_threshold,
            }
        if logger is not None:
            logger.warning(
                "[ops-alert] 近期错误率 %.2f%% (%d/%d 为 5xx) 超过阈值 %.0f%%",
                rate * 100, we, wr, error_rate_threshold * 100,
            )
        return alert

    def persist_snapshot(self, log_dir: str) -> Optional[str]:
        """把当前指标的精简快照追加写入 data/logs/ops-YYYYMMDD.jsonl（按天分文件）。

        失败时静默返回 None（持久化不能影响主请求链路）。
        """
        try:
            snap = self.snapshot()
            compact = {
                "generated_at": snap["generated_at"],
                "uptime_s": snap["uptime_s"],
                "total_requests": snap["total_requests"],
                "total_errors": snap["total_errors"],
                "error_rate": snap["error_rate"],
                "static_skipped": snap["static_skipped"],
                "by_status": snap["by_status"],
                "top_slowest": snap["slowest"][:5],
                "top_errors": snap["most_errors"][:5],
                "recent_error_count": len(snap["recent_errors"]),
            }
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, "ops-%s.jsonl" % time.strftime("%Y%m%d"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(compact, ensure_ascii=False) + "\n")
            return path
        except Exception:
            return None

    def start_background(
        self,
        log_dir: str,
        logger=None,
        interval_s: float = 300.0,
        alert_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """启动一个守护线程，周期性评估告警并把快照落盘。幂等（只启动一次）。"""
        with self._lock:
            if self._bg_started:
                return
            self._bg_started = True

        def _loop() -> None:
            while True:
                time.sleep(interval_s)
                try:
                    self.evaluate_alerts(logger, **(alert_kwargs or {}))
                    self.persist_snapshot(log_dir)
                except Exception as exc:  # 后台任务绝不能拖垮服务
                    if logger is not None:
                        logger.debug("ops background task error: %s", exc)

        threading.Thread(target=_loop, name="ops-background", daemon=True).start()



# ==================== /admin 运维面板（零依赖，纯内联 HTML+JS）====================

_ADMIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SmartTourism · 运维概览</title>
<style>
:root{color-scheme:dark}body{margin:0;background:#0f1420;color:#e6e9ef;
font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:20px}
h1{font-size:18px;margin:0 0 4px}h2{font-size:14px;margin:20px 0 8px;color:#9aa4b2;font-weight:600}
.sub{color:#7c8698;font-size:12px;margin-bottom:16px}
.cards{display:flex;flex-wrap:wrap;gap:12px}
.card{background:#171e2e;border:1px solid #232c40;border-radius:10px;padding:12px 16px;min-width:120px}
.card .k{color:#8b96a8;font-size:12px}.card .v{font-size:22px;font-weight:700;margin-top:2px}
.bad{color:#ff6b6b}.warn{color:#ffc078}.ok{color:#69db7c}
table{width:100%;border-collapse:collapse;background:#141b29;border:1px solid #232c40;border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #1e2739;font-size:13px}
th{color:#8b96a8;font-weight:600;background:#171e2e}tr:last-child td{border-bottom:none}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.err-msg{color:#ff9c9c;word-break:break-all}
.pill{display:inline-block;padding:1px 7px;border-radius:6px;background:#232c40;font-size:12px}
.refresh{float:right;color:#7c8698;font-size:12px}
button{background:#232c40;color:#e6e9ef;border:1px solid #2f3a52;border-radius:8px;padding:5px 12px;cursor:pointer}
button:hover{background:#2c3752}
</style></head><body>
<h1>SmartTourism · 运维概览 <span class="refresh" id="ts"></span></h1>
<div class="sub">内存态指标，重启清零 · 每 5 秒自动刷新 ·
<button onclick="load()">立即刷新</button></div>
<div class="cards" id="cards"></div>
<h2>最近错误（含 request_id，可对照 data/logs/error.log）</h2>
<div id="errors"></div>
<h2>最慢接口（按平均耗时）</h2>
<div id="slowest"></div>
<h2>错误最多接口</h2>
<div id="mosterr"></div>
<h2>最忙接口</h2>
<div id="busiest"></div>
<h2>状态码分布</h2>
<div id="status"></div>
<h2>降级与质检（保险丝状态：坏了不影响答复，但必须看得见）</h2>
<div id="degrade"></div>
<h2>答复反馈与质量（用户投票 + 规则指标跑在库里真实答复上）</h2>
<div id="feedback"></div>
<h2>模型用量与成本（上游报的 token 算实测，取不到才按字符折算，两种来源分开计数）</h2>
<div id="usage"></div>
<h2>生图用量与复用（Seedream 按张计费，命中指纹的复用一次网都不出）</h2>
<div id="imgusage"></div>
<h2>Agent 能力清单（已接线 / 建好未接线，如实分开）</h2>
<div id="agents"></div>
<script>
function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
function fmtUp(s){s=Math.round(s);var h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;
return (h?h+'h ':'')+(m?m+'m ':'')+x+'s';}
function tbl(rows,cols){if(!rows||!rows.length)return '<div class="sub">（暂无数据）</div>';
var h='<table><tr>'+cols.map(function(c){return '<th'+(c.n?' class="n"':'')+'>'+esc(c.t)+'</th>';}).join('')+'</tr>';
rows.forEach(function(r){h+='<tr>'+cols.map(function(c){var v=c.f?c.f(r):r[c.k];
return '<td'+(c.n?' class="n"':'')+(c.cls?' class="'+c.cls+'"':'')+'>'+esc(v)+'</td>';}).join('')+'</tr>';});
return h+'</table>';}
function load(){
  fetch('/api/admin/ops').then(function(r){return r.json();}).then(function(d){
    document.getElementById('ts').textContent='更新于 '+esc(d.generated_at)+' · uptime '+fmtUp(d.uptime_s);
    var er=d.error_rate||0, erCls=er>0.05?'bad':(er>0.01?'warn':'ok');
    document.getElementById('cards').innerHTML=[
      ['总请求',d.total_requests,''],['运行时长',fmtUp(d.uptime_s),''],
      ['错误数(5xx)',d.total_errors,d.total_errors?'bad':'ok'],
      ['错误率',(er*100).toFixed(2)+'%',erCls],
      ['静态跳过',d.static_skipped,''],['跟踪端点',(d.busiest||[]).length,'']
    ].map(function(c){return '<div class="card"><div class="k">'+esc(c[0])+
      '</div><div class="v '+(c[2]||'')+'">'+esc(c[1])+'</div></div>';}).join('');
    document.getElementById('errors').innerHTML=tbl(d.recent_errors,[
      {t:'时间',k:'ts'},{t:'状态',k:'status',n:1},{t:'路径',k:'path'},
      {t:'类型',k:'exc_type'},{t:'消息',k:'message',cls:'err-msg'},{t:'request_id',k:'request_id'}]);
    var ec=[{t:'接口',k:'endpoint'},{t:'次数',k:'count',n:1},
      {t:'平均ms',k:'avg_ms',n:1},{t:'最大ms',k:'max_ms',n:1},{t:'错误',k:'errors',n:1}];
    document.getElementById('slowest').innerHTML=tbl(d.slowest,ec);
    document.getElementById('mosterr').innerHTML=tbl(d.most_errors,ec);
    document.getElementById('busiest').innerHTML=tbl(d.busiest,ec);
    var bs=d.by_status||{},keys=Object.keys(bs);
    document.getElementById('status').innerHTML=keys.length?
      '<div class="cards">'+keys.map(function(k){var cls=k[0]==='5'?'bad':(k[0]==='4'?'warn':'ok');
      return '<div class="card"><div class="k"><span class="pill">'+esc(k)+'</span></div><div class="v '+cls+'">'+esc(bs[k])+'</div></div>';}).join('')+'</div>'
      :'<div class="sub">（暂无数据）</div>';
    // 降级与质检：会话库 / 文件日志 / 反幻觉校验 / 知识库检索
    var db=d.database||{}, vd=d.validation||{}, rag=vd.rag||{}, dg=[];
    function cnt(o){var k=Object.keys(o||{});return k.length?k.map(function(x){return x+'×'+o[x];}).join(' '):'';}
    if(Object.keys(db).length){
      var f=db.total_failures||0;
      dg.push(['会话库',db.ready?(f?'后台写库有降级':'可用'):'启动即失败',
        db.ready?('失败 '+f+' 次 '+(cnt(db.by_function)||'—')):(db.init_error||'')]);
    }
    if(d.degradation){dg.push(['文件日志',d.degradation.log_file_ok?'写入正常':'已降级为仅控制台',
      d.degradation.log_file_ok?'data/logs 可写':'排查请看 stdout']);}
    var cs=d.chat_streams||{};
    if(Object.keys(cs).length){dg.push(['流式生成',cs.error?'统计异常':('在跑 '+(cs.active||0)+' 路'),
      '主动停止 '+(cs.cancelled||0)+' 次 · 客户端断连 '+(cs.client_disconnected||0)+' 次']);}
    if(Object.keys(vd).length){
      dg.push(['反幻觉校验',vd.degraded_runs?('有环节被跳过 ×'+vd.degraded_runs):'环节完整',
        '已校验 '+(vd.runs||0)+' 轮 · 检出问题 '+(vd.issues||0)+' 轮 '+cnt(vd.by_stage)]);
      dg.push(['知识库检索',rag.build_failed?'构建失败·已永久跳过':(rag.retriever||'未初始化'),
        '检索异常 '+(rag.retrieval_errors||0)+' 次']);
    }
    document.getElementById('degrade').innerHTML=dg.length?
      '<table><tr><th>项</th><th>状态</th><th>说明</th></tr>'+dg.map(function(r){
        return '<tr><td>'+esc(r[0])+'</td><td><span class="pill">'+esc(r[1])+'</span></td><td>'+esc(r[2])+'</td></tr>';}).join('')+'</table>'
      :'<div class="sub">（暂无数据）</div>';
    // 答复反馈与质量：票数=用户怎么说，规则指标=代码怎么判，放在一起才看得出两者偏差
    var aq=d.answer_quality||{}, fb=aq.feedback||{}, MN={no_banned_terms:'违规词（广告法极限词）',
      source_tag:'价格来源标注', format:'格式/长度合理'}, fh='';
    if(aq.sample_size!=null||aq.error){
      var rate=fb.up_rate;
      fh+='<div class="cards">'+[
        ['反馈票数',fb.total||0,''],['点赞',fb.up||0,'ok'],
        ['点踩',fb.down||0,(fb.down||0)?'bad':''],
        ['点赞率',rate==null?'还没人投票':((rate*100).toFixed(1)+'%'),''],
        ['评估样本',aq.sample_size||0,''],
        ['规则均分',aq.mean_overall==null?'暂无样本':aq.mean_overall,'']
      ].map(function(c){return '<div class="card"><div class="k">'+esc(c[0])+
        '</div><div class="v '+(c[2]||'')+'">'+esc(c[1])+'</div></div>';}).join('')+'</div>';
      fh+='<div class="sub">'+esc(aq.sample_source||'')+' · 半截答复 '+(aq.truncated||0)+
        ' 条 · 带知识库出处 '+(aq.with_sources||0)+' 条'+
        (aq.error?(' · 评估模块异常：'+esc(aq.error)):'')+
        '（要点覆盖率需要参考答案要点，真实提问没有，不参与均分）</div>';
      var pr=aq.metric_pass_rate||{}, pk=Object.keys(pr);
      if(pk.length) fh+=tbl(pk.map(function(k){return {m:MN[k]||k,r:(pr[k]*100).toFixed(1)+'%',
        c:pr[k]>=0.995?'ok':(pr[k]>=0.8?'warn':'bad')};}),
        [{t:'规则指标',k:'m'},{t:'通过率',k:'r',n:1,cls:'c'}]);
      var br=aq.by_rating||{};
      if(br.voted!=null) fh+='<div class="sub">有票样本 '+br.voted+' 条（点赞均分 '+
        esc((br.up||{}).mean_overall==null?'—':br.up.mean_overall)+'，点踩均分 '+
        esc((br.down||{}).mean_overall==null?'—':br.down.mean_overall)+
        '，未投票 '+br.unvoted+' 条——没投票不等于满意）</div>';
      if((fb.top_down_reasons||[]).length) fh+=tbl(fb.top_down_reasons,
        [{t:'点踩理由',k:'reason'},{t:'次数',k:'count',n:1}]);
      if((fb.by_day||[]).length) fh+=tbl(fb.by_day,
        [{t:'日期(UTC)',k:'date'},{t:'点赞',k:'up',n:1},{t:'点踩',k:'down',n:1}]);
      if((aq.failed_samples||[]).length) fh+=tbl(aq.failed_samples.map(function(x){
        return {q:x.question||'（无相邻提问）',r:x.rating||'—',f:(x.failed||[]).join('; ')};}),
        [{t:'未通过的答复',k:'q'},{t:'用户票',k:'r'},{t:'明细',k:'f',cls:'err-msg'}]);
    }
    document.getElementById('feedback').innerHTML=fh||'<div class="sub">（暂无数据）</div>';
    // 模型用量与成本：实测/折算分开报，没配单价就只报 token，不编出金额
    var lu=d.llm_usage||{}, uh='';
    if(lu.error){uh='<div class="sub">用量统计异常：'+esc(lu.error)+'</div>';}
    else if(lu.calls!=null){
      uh+='<div class="cards">'+[
        ['模型调用',lu.calls,''],['token 实测调用',lu.upstream_calls||0,'ok'],
        ['token 折算调用',lu.estimated_calls||0,(lu.estimated_calls||0)?'warn':'ok'],
        ['输入 token',lu.prompt_tokens||0,''],['输出 token',lu.completion_tokens||0,''],
        ['平均耗时',(lu.avg_duration_ms||0)+'ms',''],
        ['最大耗时',(lu.max_duration_ms||0)+'ms',''],
        ['平均首字',lu.avg_first_token_ms==null?'仅流式可测':(lu.avg_first_token_ms+'ms'),''],
        ['缓存命中率',lu.cache_hit_rate==null?'上游未报':((lu.cache_hit_rate*100).toFixed(1)+'%'),
          lu.cache_hit_rate>=0.5?'ok':(lu.cache_hit_rate>0?'warn':'')]
      ].map(function(c){return '<div class="card"><div class="k">'+esc(c[0])+
        '</div><div class="v '+(c[2]||'')+'">'+esc(c[1])+'</div></div>';}).join('')+'</div>';
      uh+='<div class="sub">模型 '+esc(lu.model||'')+' · '+esc(lu.token_source||'')+
        (lu.cache_hit_tokens!=null?(' · 输入按缓存命中 '+esc(lu.cache_hit_tokens)+
            ' / 未命中 '+esc(lu.cache_miss_tokens)+' token（'+esc(lu.cache_note||'')+'）'):'')+
        (lu.cost?(' · 单价 '+esc(lu.cost.currency)+' '+lu.cost.price_in_per_million+
                 '（输入）/ '+lu.cost.price_out_per_million+'（输出）元每百万 token → 累计约 '+
                 lu.cost.amount+' 元；'+esc(lu.cost.note)):(' · '+esc(lu.cost_note||'')))+
        '（本进程启动以来累计，重启清零）</div>';
      var bk=Object.keys(lu.by_skill||{});
      if(bk.length) uh+=tbl(bk.map(function(k){var b=lu.by_skill[k]||{};
        return {k:k,c:b.calls||0,p:b.prompt_tokens||0,o:b.completion_tokens||0,
          a:b.calls?Math.round((b.duration_ms_total||0)/b.calls):0,
          u:b.upstream_calls||0,e:b.estimated_calls||0};}),
        [{t:'Skill',k:'k'},{t:'调用',k:'c',n:1},{t:'输入token',k:'p',n:1},
         {t:'输出token',k:'o',n:1},{t:'平均ms',k:'a',n:1},
         {t:'实测',k:'u',n:1},{t:'折算',k:'e',n:1}]);
    }
    document.getElementById('usage').innerHTML=uh||'<div class="sub">（本进程还没有模型调用记录）</div>';
    // 生图与复用：Seedream 按张计费，复用掉的每一次都是净省的一张钱
    var iu=d.image_usage||{}, ih='';
    if(iu.error){ih='<div class="sub">生图统计异常：'+esc(iu.error)+'</div>';}
    else if(iu.requests!=null){
      ih+='<div class="cards">'+[
        ['出图请求（计费）',iu.requests,(iu.failed?'warn':'')],
        ['成功出图',iu.success||0,'ok'],
        ['失败',iu.failed||0,(iu.failed||0)?'warn':'ok'],
        ['命中复用（0 成本）',iu.reused||0,(iu.reused||0)?'ok':''],
        ['复用率',iu.reuse_rate==null?'尚无请求':((iu.reuse_rate*100).toFixed(1)+'%'),
          (iu.reuse_rate>=0.3?'ok':'')],
        ['平均出图耗时',(iu.avg_duration_ms||0)+'ms',''],
        ['最慢一次',(iu.max_duration_ms||0)+'ms',''],
        ['库里可复用指纹',iu.cache_entries||0,''],
        ['累计复用（跨重启）',iu.cache_reuses_total||0,'ok']
      ].map(function(c){return '<div class="card"><div class="k">'+esc(c[0])+
        '</div><div class="v '+(c[2]||'')+'">'+esc(c[1])+'</div></div>';}).join('')+'</div>';
      ih+='<div class="sub">'+esc(iu.note||'')+
        (iu.cost?(' · 单价 '+esc(iu.cost.currency)+' '+iu.cost.price_per_image+
                 ' 元/张 → 出网 '+iu.requests+' 张约 '+iu.cost.amount+' 元；'+esc(iu.cost.note))
                :(' · '+esc(iu.cost_note||'')))+
        ' · 缓存'+(iu.enabled?'开启':'关闭（SMARTTOURISM_ARK_IMAGE_CACHE=0）')+
        '。'+esc(iu.cache_persist_note||'')+'</div>';
      var sk=Object.keys(iu.by_scene||{});
      if(sk.length) ih+=tbl(sk.map(function(k){var b=iu.by_scene[k]||{};
        return {k:k||'(未标注)',r:b.requests||0,s:b.success||0,f:b.failed||0,
          u:b.reused||0,a:b.requests?Math.round((b.duration_ms_total||0)/b.requests):0};}),
        [{t:'场景',k:'k'},{t:'出图请求',k:'r',n:1},{t:'成功',k:'s',n:1},
         {t:'失败',k:'f',n:1},{t:'命中复用',k:'u',n:1},{t:'平均ms',k:'a',n:1}]);
      var fr=Object.keys(iu.fail_reasons||{});
      if(fr.length) ih+='<div class="sub">失败原因分布：'+
        esc(fr.map(function(k){return k+' '+iu.fail_reasons[k]+' 次';}).join('、'))+'</div>';
    }
    document.getElementById('imgusage').innerHTML=
      ih||'<div class="sub">（本进程还没有生图记录）</div>';
    // Agent 能力清单：状态词翻译成人话，未接线的照实写「未接线」而不是藏起来
    var SM={active:'运行中',wired_idle:'已接线·待触发',disabled:'环境变量关闭',
      unavailable:'不可用·已兜底',degraded:'降级·已跳过',not_wired:'未接线',
      imported:'已被导入·清单待更新'};
    function st(s){return esc(SM[s]||s);}
    var am=d.agent_modules||{}, ar=[];
    (am.wired||[]).forEach(function(m){ar.push(['已接线',m.module,m.capability,st(m.state),m.detail]);});
    (am.unwired||[]).forEach(function(m){ar.push(['未接线',m.module,m.capability,st(m.state),m.detail]);});
    document.getElementById('agents').innerHTML=ar.length?
      '<table><tr><th>接线</th><th>模块</th><th>能力</th><th>状态</th><th>说明</th></tr>'+ar.map(function(r){
        return '<tr><td><span class="pill">'+esc(r[0])+'</span></td><td>'+esc(r[1])+'</td><td>'+esc(r[2])
          +'</td><td>'+r[3]+'</td><td>'+esc(r[4])+'</td></tr>';}).join('')+'</table>'
      :'<div class="sub">（暂无数据）</div>';
  }).catch(function(e){document.getElementById('ts').textContent='加载失败：'+e;});
}
load();setInterval(load,5000);
</script></body></html>"""


def render_admin_html() -> str:
    return _ADMIN_HTML
