# 客群画像库

## 标准客群画像模板

### 1. 亲子家庭

#### 幼龄亲子（0-6岁）
```json
{
  "crowd_id": "family_young",
  "label": "幼龄亲子家庭",
  "age_range": "25-40",
  "children_age": "0-6",
  "group_size": "3-5人",
  "budget_per_person": "500-1500元",
  "trip_duration": "1-3天（周末为主）",
  "travel_mode": "自驾>高铁",
  "core_needs": ["安全", "便捷", "亲子设施", "短途"],
  "pain_points": ["孩子哭闹影响他人", "没有儿童餐", "设施不安全"],
  "decision_factors": ["儿童政策", "安全性", "亲子设施", "距离"],
  "info_channels": ["小红书", "抖音亲子", "妈妈群", "亲子公众号"],
  "booking_platform": "携程/美团/飞猪",
  "content_preference": ["遛娃攻略", "亲子酒店测评", "亲子vlog"],
  "seasonality": "周末/寒暑假/小长假",
  "product_sensitivity": {
    "price": "中等敏感",
    "quality": "高要求",
    "brand": "中等关注"
  }
}
```

#### 学龄亲子（7-15岁）
```json
{
  "crowd_id": "family_school",
  "label": "学龄亲子家庭",
  "age_range": "30-45",
  "children_age": "7-15",
  "group_size": "3-5人",
  "budget_per_person": "800-2000元",
  "trip_duration": "2-5天",
  "travel_mode": "自驾/高铁/飞机",
  "core_needs": ["寓教于乐", "互动体验", "研学内容"],
  "pain_points": ["孩子觉得无聊", "缺乏互动项目", "排队太长"],
  "decision_factors": ["教育价值", "互动性", "口碑评价"],
  "info_channels": ["小红书", "抖音", "携程攻略", "家长社群"],
  "booking_platform": "携程/美团/马蜂窝",
  "content_preference": ["研学攻略", "亲子路线", "夏令营测评"],
  "seasonality": "寒暑假/国庆/五一"
}
```

### 2. Z世代

```json
{
  "crowd_id": "gen_z",
  "label": "Z世代年轻客群",
  "age_range": "18-28",
  "group_size": "2-4人",
  "budget_per_person": "300-800元",
  "trip_duration": "1-3天",
  "travel_mode": "公共交通/拼车",
  "core_needs": ["出片效果", "氛围感", "性价比", "小众"],
  "pain_points": ["商业化太重", "千篇一律", "预算有限"],
  "decision_factors": ["视觉吸引力", "社交传播性", "价格"],
  "info_channels": ["抖音", "B站", "小红书", "微博"],
  "booking_platform": "美团/飞猪/抖音团购",
  "content_preference": ["打卡攻略", "短视频", "测评种草"],
  "seasonality": "周末/节假日/毕业季",
  "keywords": ["出片", "氛围感", "小众", "宝藏", "打卡", "出逃"]
}
```

### 3. 情侣蜜月

```json
{
  "crowd_id": "couple",
  "label": "情侣蜜月客群",
  "age_range": "22-35",
  "group_size": "2人",
  "budget_per_person": "800-3000元",
  "trip_duration": "2-5天",
  "travel_mode": "自驾/高铁/飞机",
  "core_needs": ["浪漫氛围", "隐私", "仪式感", "纪念价值"],
  "pain_points": ["人多嘈杂", "服务不贴心", "缺乏惊喜"],
  "decision_factors": ["氛围感", "隐私性", "景观", "服务"],
  "info_channels": ["小红书", "抖音", "朋友圈", "婚礼博主"],
  "booking_platform": "携程/飞猪/民宿平台",
  "content_preference": ["浪漫攻略", "情侣vlog", "求婚/纪念日"],
  "seasonality": "节假日/周末/纪念日/情人节",
  "keywords": ["浪漫", "仪式感", "二人世界", "私享", "绝美"]
}
```

### 4. 银发康养

```json
{
  "crowd_id": "senior",
  "label": "银发康养客群",
  "age_range": "55+",
  "group_size": "2-6人（含子女陪同）",
  "budget_per_person": "500-1500元",
  "trip_duration": "3-7天",
  "travel_mode": "跟团/自驾/高铁",
  "core_needs": ["舒适", "康养", "文化", "安全"],
  "pain_points": ["行程太赶", "台阶太多", "医疗不便"],
  "decision_factors": ["舒适度", "健康安全", "文化内涵"],
  "info_channels": ["微信", "今日头条", "电视/报纸", "社区推荐"],
  "booking_platform": "携程/电话预订/门店",
  "content_preference": ["康养攻略", "文化解读", "慢生活vlog"],
  "seasonality": "淡季/春秋季",
  "keywords": ["康养", "慢生活", "文化", "品质", "养生"]
}
```

### 5. 商务出行

```json
{
  "crowd_id": "business",
  "label": "商务出行客群",
  "age_range": "30-50",
  "group_size": "1-5人",
  "budget": "公司报销/中高预算",
  "trip_duration": "1-3天",
  "travel_mode": "飞机/高铁/自驾",
  "core_needs": ["效率", "品质", "便利", "商务配套"],
  "pain_points": ["交通不便", "入住繁琐", "缺少办公设施"],
  "decision_factors": ["位置", "效率", "配套", "品牌"],
  "info_channels": ["携程商旅", "飞猪", "企业协议"],
  "booking_platform": "携程商旅/飞猪/酒店官网",
  "content_preference": ["效率攻略", "商务配套测评"],
  "seasonality": "工作日/展会季"
}
```

### 6. 团建聚会

```json
{
  "crowd_id": "team_building",
  "label": "团建聚会客群",
  "age_range": "22-45",
  "group_size": "10-50人",
  "budget_per_person": "300-1000元",
  "trip_duration": "1-2天",
  "travel_mode": "包车/大巴",
  "core_needs": ["活动场地", "团队项目", "聚餐空间"],
  "pain_points": ["场地不够", "项目单一", "组织麻烦"],
  "decision_factors": ["承载量", "项目丰富度", "餐饮配套"],
  "info_channels": ["企业HR社群", "团建平台", "微信"],
  "booking_platform": "大众点评/小红书/团建平台",
  "content_preference": ["团建方案", "团建vlog"],
  "seasonality": "季末/年末/周年庆"
}
```
