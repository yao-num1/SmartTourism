"""
SmartTourism · ReAct 编排引擎
轻量 ReAct 循环引擎（Nanobot 风格），负责意图分类 → 规划 → 执行 → 格式化 → 循环控制

架构:
  IntentClassifier → Planner → Executor → Formatter
  循环控制: 最多 15 轮，可中断

依赖:
  - app.call_llm()       # LLM 调用
  - app.get_plugin_data() # 插件数据
  - skills/ 目录         # SKILL.md 定义
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

# ---- 日志 ----
logger = logging.getLogger("agent_engine")
logger.setLevel(logging.DEBUG)

# ==================== 类型定义 ====================

class Intent(Enum):
    """意图分类枚举"""
    CONSULT = "consult"        # 咨询问答
    PLAN = "plan"              # 行程规划
    MARKETING = "marketing"    # 营销文案
    CULTURAL = "cultural"      # 文创设计
    OTHER = "other"            # 通用对话


@dataclass
class IntentResult:
    """意图分类结果"""
    intent: Intent
    destination: Optional[str] = None       # 目的地城市
    days: Optional[int] = None             # 天数
    budget: Optional[int] = None           # 预算
    crowd_type: Optional[str] = None       # 人群类型
    keywords: List[str] = field(default_factory=list)  # 关键词
    confidence: float = 0.0                 # 置信度


@dataclass
class PlanStep:
    """规划步骤"""
    skill_name: str           # 要调用的 Skill 名称
    action: str               # 动作: call_skill / call_api / call_llm
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult:
    """执行结果"""
    success: bool
    data: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ==================== Skill 加载 ====================

SKILL_DIR = None  # 延迟初始化


def _ensure_skill_dir():
    """确保 SKILL_DIR 已初始化"""
    global SKILL_DIR
    if SKILL_DIR is None:
        import os
        SKILL_DIR = os.path.join(os.path.dirname(__file__), '..', 'skills')


def load_skill_definitions() -> Dict[str, str]:
    """加载所有 Skill 的 SKILL.md 定义"""
    import os
    _ensure_skill_dir()
    definitions: Dict[str, str] = {}
    for skill_name in ['tourism-advisor', 'tourism-marketing', 'smart-matching']:
        skill_path = os.path.join(SKILL_DIR, skill_name, 'SKILL.md')
        if os.path.exists(skill_path):
            with open(skill_path, 'r', encoding='utf-8') as f:
                definitions[skill_name] = f.read()
    return definitions


# ==================== 意图分类器 ====================

class IntentClassifier:
    """
    意图分类器（规则 + LLM 兜底）

    策略:
      - 先用关键词规则快速匹配（毫秒级）
      - 规则不命中时用 LLM 兜底（约 1 秒）
    """

    # 规则映射: (关键词列表) → (Intent, 技能名)
    RULES: List[Tuple[List[str], Intent, str]] = [
        # 行程规划类（最高优先级 — 多关键词可覆盖咨询类）
        (["行程", "规划", "攻略", "几日游", "几天", "帮我安排", "路线",
          "三日游", "一日游", "两日游", "四日游", "五日游", "七日游", "n日游", "多日"],
         Intent.PLAN, "tourism-advisor"),
        # 咨询类
        (["推荐", "有什么", "怎么去", "在哪里", "好吃", "好玩", "酒店", "住宿",
          "景点", "门票", "天气", "交通", "美食", "附近", "周边"],
         Intent.CONSULT, "tourism-advisor"),
        # 营销文案类
        (["文案", "营销", "推广", "广告", "宣传", "短视频", "脚本", "小红书",
          "抖音", "朋友圈", "海报", "活动策划"],
         Intent.MARKETING, "tourism-marketing"),
        # 文创设计类
        (["文创", "周边产品", "纪念品", "IP", "品牌", "设计", "产品概念"],
         Intent.CULTURAL, "tourism-marketing"),
        # 客群匹配类（交给 smart-matching）
        (["客群", "画像", "匹配", "转化", "策略", "客源", "定位"],
         Intent.MARKETING, "smart-matching"),
    ]

    # 城市列表（用于提取目的地）
    CITIES = {
        '杭州', '三亚', '成都', '北京', '大理', '丽江', '上海', '广州', '深圳',
        '厦门', '桂林', '西安', '南京', '重庆', '青岛', '武汉', '长沙', '昆明',
        '张家界', '黄山', '九寨沟', '拉萨', '哈尔滨', '苏州', '乌镇', '温州',
        '宁波', '郑州', '合肥', '南昌', '福州', '南宁', '贵阳', '海口',
        '济南', '太原', '沈阳', '长春', '天津', '石家庄', '呼和浩特', '珠海',
    }

    def classify(self, user_input: str) -> IntentResult:
        """
        对用户输入进行意图分类

        Args:
            user_input: 用户输入文本

        Returns:
            IntentResult: 意图分类结果
        """
        user_lower = user_input.lower()

        # 1) 规则匹配（计分制 — 匹配关键词最多的获胜，打破"先到先得"）
        best_match = (0, None, [])  # (score, intent, keywords)
        for keywords, intent, skill_name in self.RULES:
            hits = [kw for kw in keywords if kw in user_input]
            if hits:
                score = sum(len(kw) for kw in hits)  # 关键词越长权重越高
                if score > best_match[0]:
                    best_match = (score, intent, keywords)

        if best_match[1] is not None:
            result = IntentResult(
                intent=best_match[1],
                confidence=0.85,
                keywords=best_match[2],
            )
            self._extract_params(user_input, result)
            return result

        # 2) 默认回退: OTHER（一般对话）
        result = IntentResult(intent=Intent.OTHER, confidence=0.3, keywords=[])
        self._extract_params(user_input, result)
        return result

    def classify_with_llm(self, user_input: str, call_llm_fn: Callable) -> IntentResult:
        """
        LLM 兜底分类（用于规则不命中或低置信度场景）

        Args:
            user_input: 用户输入
            call_llm_fn: LLM 调用函数（签名同 app.call_llm）

        Returns:
            IntentResult: 分类结果
        """
        prompt = (
            "你是一个意图分类器。分析以下用户输入，返回 JSON。\n"
            "intent 可选值: consult(咨询问答) / plan(行程规划) / marketing(营销文案) "
            "/ cultural(文创设计) / other(其他)\n"
            "同时提取 destination(目的地城市)、days(天数)、budget(预算) 等参数。\n\n"
            f"用户输入: {user_input}\n\n"
            '请只返回 JSON，格式: {"intent": "...", "destination": "...", '
            '"days": ..., "budget": ..., "keywords": [...]}'
        )

        try:
            resp = call_llm_fn([{"role": "user", "content": prompt}])
            if "error" not in resp:
                content = resp["choices"][0]["message"]["content"]
                # 提取 JSON
                json_match = re.search(r'\{[^{}]*\}', content, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group())
                    intent_str = data.get("intent", "other")
                    try:
                        intent = Intent(intent_str)
                    except ValueError:
                        intent = Intent.OTHER

                    result = IntentResult(
                        intent=intent,
                        destination=data.get("destination"),
                        days=data.get("days"),
                        budget=data.get("budget"),
                        keywords=data.get("keywords", []),
                        confidence=0.7,
                    )
                    return result
        except Exception as e:
            logger.warning(f"LLM 意图分类失败: {e}")

        return IntentResult(intent=Intent.OTHER, confidence=0.0)

    def _extract_params(self, user_input: str, result: IntentResult):
        """从用户输入中提取结构化参数"""
        # 提取目的地
        for city in self.CITIES:
            if city in user_input:
                result.destination = city
                break

        # 提取天数
        day_match = re.search(r'(\d+)\s*天', user_input)
        if day_match:
            result.days = int(day_match.group(1))

        # 提取预算
        budget_match = re.search(r'(\d+)\s*(元|块|万|k|K)?\s*(预算|人均)', user_input)
        if budget_match:
            amount = int(budget_match.group(1))
            unit = budget_match.group(2)
            if unit in ('万',):
                amount *= 10000
            elif unit in ('k', 'K'):
                amount *= 1000
            result.budget = amount

        # 提取人群类型
        crowd_patterns = {
            '亲子': r'亲子|带娃|小孩|孩子|宝宝',
            '情侣': r'情侣|蜜月|约会|二人',
            '老年': r'老人|爸妈|父母|银发|退休',
            '团建': r'团建|公司|团队',
            '学生': r'学生|毕业|穷游',
            '商务': r'商务|出差|会议',
        }
        for crowd_type, pattern in crowd_patterns.items():
            if re.search(pattern, user_input):
                result.crowd_type = crowd_type
                break


# ==================== 规划器 ====================

class Planner:
    """
    规划器: 根据意图选择要调用的 Skill/API/LLM

    映射:
      consult  → tourism-advisor (咨询问答)
      plan     → tourism-advisor (行程规划)
      marketing → tourism-marketing (营销文案)
      cultural → tourism-marketing (文创设计)
      other    → 通用对话 (无 Skill)
    """

    INTENT_TO_SKILL: Dict[Intent, str] = {
        Intent.CONSULT: "tourism-advisor",
        Intent.PLAN: "tourism-advisor",
        Intent.MARKETING: "tourism-marketing",
        Intent.CULTURAL: "tourism-marketing",
    }

    def plan(self, intent_result: IntentResult) -> List[PlanStep]:
        """
        根据意图结果生成执行计划

        Args:
            intent_result: 意图分类结果

        Returns:
            List[PlanStep]: 执行步骤列表
        """
        steps: List[PlanStep] = []

        if intent_result.intent == Intent.OTHER:
            # 通用对话: 直接 LLM 回复
            steps.append(PlanStep(
                skill_name="",
                action="call_llm",
            ))
            return steps

        # 获取对应的 Skill
        skill = self.INTENT_TO_SKILL.get(intent_result.intent, "tourism-advisor")

        # Step 1: 获取插件实时数据（天气、POI 等）
        steps.append(PlanStep(
            skill_name=skill,
            action="call_api",
            params={"type": "plugin_data"},
        ))

        # Step 2: 调用 LLM (带 Skill 提示词)
        steps.append(PlanStep(
            skill_name=skill,
            action="call_llm",
            params={
                "skill": skill,
                "destination": intent_result.destination,
                "days": intent_result.days,
                "budget": intent_result.budget,
                "crowd_type": intent_result.crowd_type,
            },
        ))

        return steps


# ==================== 执行器 ====================

class Executor:
    """
    执行器: 执行规划步骤，调用 LLM 或插件 API
    """

    def __init__(
        self,
        call_llm_fn: Callable,
        get_plugin_data_fn: Callable,
        skill_definitions: Optional[Dict[str, str]] = None,
    ):
        """
        Args:
            call_llm_fn: LLM 调用函数 (app.call_llm)
            get_plugin_data_fn: 插件数据获取函数 (app.get_plugin_data)
            skill_definitions: Skill 定义字典
        """
        self.call_llm = call_llm_fn
        self.get_plugin_data = get_plugin_data_fn
        self.skill_definitions = skill_definitions or load_skill_definitions()

    def execute(self, step: PlanStep, user_input: str) -> ActionResult:
        """
        执行单个规划步骤

        Args:
            step: 规划步骤
            user_input: 原始用户输入

        Returns:
            ActionResult: 执行结果
        """
        try:
            if step.action == "call_api":
                return self._execute_api(step, user_input)
            elif step.action == "call_llm":
                return self._execute_llm(step, user_input)
            elif step.action == "call_skill":
                return self._execute_skill(step, user_input)
            else:
                return ActionResult(success=False, error=f"未知动作类型: {step.action}")
        except Exception as e:
            logger.exception(f"执行步骤失败: {step}")
            return ActionResult(success=False, error=str(e))

    def _execute_api(self, step: PlanStep, user_input: str) -> ActionResult:
        """执行 API 调用（插件数据获取）"""
        destination = step.params.get("destination")
        plugin_context = self.get_plugin_data(user_input, destination)
        return ActionResult(
            success=True,
            data=plugin_context,
            metadata={"plugin_context_length": len(plugin_context) if plugin_context else 0},
        )

    def _execute_llm(self, step: PlanStep, user_input: str) -> ActionResult:
        """执行 LLM 调用"""
        skill_name = step.params.get("skill", "")
        destination = step.params.get("destination")
        days = step.params.get("days")
        budget = step.params.get("budget")
        crowd_type = step.params.get("crowd_type")

        # 构建系统提示词
        system_prompt = self._build_system_prompt(skill_name)

        # 构建用户消息
        user_parts = []
        if destination:
            user_parts.append(f"目的地：{destination}")
        if days:
            user_parts.append(f"游玩天数：{days}天")
        if budget:
            user_parts.append(f"预算：{budget}元")
        if crowd_type:
            crowd_labels = {
                '亲子': '亲子家庭',
                '情侣': '情侣蜜月',
                '老年': '银发一族',
                '团建': '公司团建',
                '学生': '大学生',
                '商务': '商务出差',
            }
            user_parts.append(f"客群类型：{crowd_labels.get(crowd_type, crowd_type)}")
        user_parts.append(f"用户需求：{user_input}")

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": '\n'.join(user_parts)},
        ]

        resp = self.call_llm(messages)
        if "error" in resp:
            return ActionResult(success=False, error=resp["error"])

        content = resp["choices"][0]["message"]["content"]
        return ActionResult(success=True, data=content, metadata={"skill": skill_name})

    def _execute_skill(self, step: PlanStep, user_input: str) -> ActionResult:
        """执行 Skill 脚本（未来扩展）"""
        return ActionResult(success=False, error="Skill 脚本执行暂未实现")

    def _build_system_prompt(self, skill_name: str) -> str:
        """构建系统提示词（复用 app.py 逻辑）"""
        now = datetime.now()
        weekday = ['一', '二', '三', '四', '五', '六', '日'][now.weekday()]
        time_str = now.strftime('%Y年%m月%d日 %H:%M')

        base_prompt = (
            '你是一个文旅行业的智能助手"智旅灵犀"，专精于景区、酒店、餐饮三大文旅业态。\n\n'
            f'🕐 当前准确时间：{time_str} (星期{weekday})，北京时间 UTC+8\n'
            '⚠️ 重要：回答任何关于时间、日期、星期几的问题时，必须使用上述时间。\n\n'
            '你的核心能力：\n'
            '1. **智能咨询与规划**：提供目的地信息、个性化行程规划、酒店推荐、餐饮匹配\n'
            '2. **营销与文创**：生成多平台营销文案、短视频脚本、活动策划方案、文创产品创意\n'
            '3. **客源精准匹配**：分析客群画像、精准匹配产品服务、制定转化策略\n\n'
            '输出要求：结构化格式，善用表格、列表、emoji。价格以人民币标注。\n'
        )

        # 附加 Skill 定义
        if skill_name and skill_name in self.skill_definitions:
            skill_content = self.skill_definitions[skill_name]
            lines = skill_content.split('\n')
            essential = '\n'.join(lines[:120])
            base_prompt += (
                f"\n\n## 当前激活的 Skill：{skill_name}\n\n"
                f"以下是该 Skill 的详细定义，请严格遵循其中的工作流程和输出模板：\n\n"
                f"{essential}"
            )

        return base_prompt


# ==================== 格式化器 ====================

class Formatter:
    """
    格式化器: 将 Agent 输出格式化为前端可消费的结构
    """

    @staticmethod
    def format(
        action_results: List[ActionResult],
        conversation_id: str,
        intent: Intent,
    ) -> Dict[str, Any]:
        """
        格式化最终输出

        Args:
            action_results: 所有执行步骤的结果
            conversation_id: 会话 ID
            intent: 意图类型

        Returns:
            Dict: 格式化后的响应
        """
        # 提取 LLM 回复
        llm_response = ""
        plugin_context = ""
        errors: List[str] = []

        for result in action_results:
            if result.success and isinstance(result.data, str):
                if "plugin_context" in result.metadata:
                    plugin_context = result.data
                else:
                    llm_response = result.data
            elif not result.success:
                errors.append(result.error or "未知错误")

        response: Dict[str, Any] = {
            "success": len(errors) == 0 or bool(llm_response),
            "conversation_id": conversation_id,
            "intent": intent.value,
            "reply": llm_response or "抱歉，暂时无法处理您的请求，请稍后重试。",
        }

        if plugin_context:
            response["plugin_data"] = plugin_context
        if errors:
            response["errors"] = errors
        if llm_response:
            response["has_content"] = True

        # 提取行程数据（用于地图标注）
        route_data = Formatter._extract_route_data(llm_response)
        if route_data:
            response["route_data"] = route_data

        return response

    @staticmethod
    def _extract_route_data(text: str) -> Optional[Dict[str, Any]]:
        """
        尝试从 LLM 回复中提取行程结构化数据

        策略: 查找 JSON 代码块或结构化描述
        """
        if not text:
            return None

        # 尝试查找 ```json ... ``` 块
        json_match = re.search(r'```json\s*([\s\S]*?)```', text)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                if isinstance(data, dict) and ('pois' in data or 'days' in data):
                    return data
            except json.JSONDecodeError:
                pass

        # 尝试查找纯 JSON 对象
        json_match = re.search(r'\{[\s\S]*"pois"[\s\S]*\}', text)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        return None


# ==================== ReAct 循环引擎 ====================

class ReActEngine:
    """
    ReAct 循环引擎

    核心流程:
      while step < max_steps:
          intent = classify(input)
          plans = planner.plan(intent)
          for step in plans:
              result = executor.execute(step, input)
          if should_stop(result):
              break

    使用示例:
        engine = ReActEngine(call_llm_fn, get_plugin_data_fn)
        result = engine.run("帮我规划一个北京3日游")
    """

    MAX_STEPS = 15  # 最大循环轮数，防止死循环
    MAX_REPLAN = 3  # 最多重新规划次数

    def __init__(
        self,
        call_llm_fn: Callable,
        get_plugin_data_fn: Callable,
        skill_definitions: Optional[Dict[str, str]] = None,
    ):
        """
        Args:
            call_llm_fn: LLM 调用函数
            get_plugin_data_fn: 插件数据获取函数
            skill_definitions: Skill 定义（可选，默认从 skills/ 加载）
        """
        self.classifier = IntentClassifier()
        self.planner = Planner()
        self.executor = Executor(call_llm_fn, get_plugin_data_fn, skill_definitions)
        self.formatter = Formatter()
        self.step_count = 0

    def run(
        self,
        user_input: str,
        conversation_id: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        use_llm_classify: bool = False,
    ) -> Dict[str, Any]:
        """
        运行 ReAct 循环，处理用户输入

        Args:
            user_input: 用户输入文本
            conversation_id: 会话 ID（不传则自动生成）
            history: 历史对话列表
            use_llm_classify: 是否使用 LLM 进行意图分类（更准确但更慢）

        Returns:
            Dict: 包含 reply、intent、conversation_id 等字段的响应
        """
        if conversation_id is None:
            conversation_id = str(uuid.uuid4())

        self.step_count = 0
        action_results: List[ActionResult] = []
        errors: List[str] = []

        # ---- Step 1: 意图分类 ----
        intent_result = self.classifier.classify(user_input)

        # 低置信度时用 LLM 兜底
        if intent_result.confidence < 0.5 and use_llm_classify:
            try:
                llm_result = self.classifier.classify_with_llm(user_input, self.executor.call_llm)
                if llm_result.confidence > intent_result.confidence:
                    intent_result = llm_result
            except Exception as e:
                logger.warning(f"LLM 分类回退失败: {e}")

        logger.info(f"意图分类: {intent_result.intent.value} (置信度: {intent_result.confidence:.2f})")

        # ---- Step 2: 规划 ----
        plans = self.planner.plan(intent_result)

        # ---- Step 3: 执行 ----
        for i, plan in enumerate(plans):
            if self.step_count >= self.MAX_STEPS:
                logger.warning(f"达到最大循环轮数 {self.MAX_STEPS}，强制终止")
                errors.append(f"处理步骤过多，已截断在第 {self.step_count} 轮")
                break

            self.step_count += 1
            result = self.executor.execute(plan, user_input)

            # 将插件数据注入 LLM 步
            if plan.action == "call_api" and result.success and result.data:
                # 把插件数据注入到后续 LLM 步骤中
                user_input_enhanced = (
                    f"{user_input}\n\n---\n"
                    f"🔴 **以下是通过数据插件获取的真实实时数据，"
                    f"你必须严格基于这些数据回答：**\n\n{result.data}"
                )
                # 重新规划 LLM 调用
                for j in range(i + 1, len(plans)):
                    if plans[j].action == "call_llm":
                        plans[j].params["enhanced_input"] = user_input_enhanced
                        break

            action_results.append(result)

        # ---- Step 4: 格式化输出 ----
        formatted = self.formatter.format(action_results, conversation_id, intent_result.intent)

        # ---- Step 5: 附加元数据 ----
        formatted["metadata"] = {
            "intent_confidence": intent_result.confidence,
            "steps_executed": self.step_count,
            "plan_count": len(plans),
            "errors": errors if errors else None,
        }

        return formatted

    def run_stream(
        self,
        user_input: str,
        conversation_id: Optional[str] = None,
        **kwargs,
    ) -> Generator[str, None, None]:
        """
        流式运行 ReAct 循环（generator）

        Yields:
            str: SSE 格式的数据块
        """
        import json as _json

        result = self.run(user_input, conversation_id, **kwargs)

        # 模拟流式输出
        reply = result.get("reply", "")
        chunk_size = 10  # 每次输出 10 个字符

        for i in range(0, len(reply), chunk_size):
            chunk = reply[i:i + chunk_size]
            yield f"data: {_json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"

        # 发送元数据
        meta = {
            "done": True,
            "conversation_id": result["conversation_id"],
            "intent": result["intent"],
            "metadata": result.get("metadata", {}),
        }
        if result.get("route_data"):
            meta["route_data"] = result["route_data"]

        yield f"data: {_json.dumps(meta, ensure_ascii=False)}\n\n"


# ==================== 便捷工厂 ====================

def create_engine_from_app() -> ReActEngine:
    """
    从 app.py 导入依赖创建引擎

    用法:
        from server.agent_engine import create_engine_from_app
        engine = create_engine_from_app()
    """
    # 延迟导入，避免循环依赖
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    try:
        from app import call_llm, get_plugin_data, SKILL_DEFINITIONS
    except ImportError:
        # 如果无法导入，使用占位函数
        def call_llm(msg, stream=False):
            return {"error": "app.py 未加载"}

        def get_plugin_data(query, dest=None):
            return ""

        SKILL_DEFINITIONS = {}

    return ReActEngine(call_llm, get_plugin_data, SKILL_DEFINITIONS)


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("🧪 ReAct 引擎单元测试")
    print("=" * 60)

    # 测试意图分类器
    classifier = IntentClassifier()

    test_cases = [
        "帮我规划一个杭州3日游",
        "西安有什么好吃的推荐？",
        "帮我写一篇三亚酒店的小红书推广文案",
        "设计一个故宫文创产品",
        "今天天气怎么样",
        "分析一下亲子客群的画像",
    ]

    for case in test_cases:
        result = classifier.classify(case)
        print(f"\n输入: {case}")
        print(f"  意图: {result.intent.value} | 目的地: {result.destination or '无'}")
        print(f"  天数: {result.days} | 预算: {result.budget} | 人群: {result.crowd_type or '无'}")

    # 测试规划器
    planner = Planner()
    print("\n" + "-" * 40)
    for case in test_cases[:3]:
        intent_result = classifier.classify(case)
        plans = planner.plan(intent_result)
        print(f"\n{case}:")
        for step in plans:
            print(f"  → {step.action}: {step.skill_name or '(无Skill)'}")

    print("\n✅ 测试完成")
