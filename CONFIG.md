# SmartTourism 系统配置

## 系统信息
- 项目名称: SmartTourism 文旅全场景智能辅助系统
- 版本: v1.0.0
- 创建日期: 2026-06-24
- 引擎: OpenClaw
- 项目路径: D:\SmartTourism\

## 三大核心 Skill

### tourism-advisor（智能咨询与规划）
- SKILL.md 位置: D:\SmartTourism\skills\tourism-advisor\SKILL.md
- 状态: ✅ 已就绪
- 能力: 智能问答、性格画像识别、多人偏好平衡、个性化游玩旅居餐饮规划、消费转化点设计

### tourism-marketing（营销与文创）
- SKILL.md 位置: D:\SmartTourism\skills\tourism-marketing\SKILL.md
- 状态: ✅ 已就绪
- 能力: 营销文案、短视频脚本、活动策划、海报文案、文创产品线、平台适配、创收转化路径

### smart-matching（客源精准匹配）
- SKILL.md 位置: D:\SmartTourism\skills\smart-matching\SKILL.md
- 状态: ✅ 已就绪
- 能力: 客群画像、需求匹配、多人满意度平衡、消费触点匹配、转化策略、产品定位

## 数据存储路径
- 知识库: D:\SmartTourism\data\knowledge-base\
- 数据导出: D:\SmartTourism\data\exports\
- 方案模板: D:\SmartTourism\data\templates\
- 操作面板: D:\SmartTourism\dashboard\index.html

## 部署方式
- 本地部署: ✅ 全部数据本地存储
- 数据导出: ✅ 支持 JSON/CSV/Markdown 格式
- 零门槛操作: ✅ 通过自然语言对话直接使用
- 场景智能: ✅ 支持情侣、亲子、银发、学生、商务、团建等多场景差异化输出
- 创收增效: ✅ 行程、营销、文创均输出门票/住宿/餐饮/文创/活动消费转化设计

## 安全与异常维护
- 维护指南: D:\SmartTourism\安全与异常维护指南.md
- 环境变量模板: D:\SmartTourism\.env.example
- 日志目录: D:\SmartTourism\data\logs\
- 生产环境要求: `SMARTTOURISM_DEBUG=0`，并配置 `SMARTTOURISM_CORS_ORIGINS`

## 安装到 OpenClaw
将 skills 目录下的 Skill 复制到 OpenClaw skills 目录：
```powershell
Copy-Item -Recurse D:\SmartTourism\skills\* C:\Users\zkg\.easyclaw\workspace\skills\
```
