# 05 - 组件库与交互模式

## 一、按钮组件

### 液态玻璃按钮（2025 爆款）
```css
.glass-btn {
  background: rgba(255,255,255,0.15);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(255,255,255,0.2);
  border-radius: 9999px;
  padding: 10px 24px;
  color: #fff;
  font-weight: 600;
  transition: all 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
.glass-btn:hover {
  background: rgba(255,255,255,0.25);
  box-shadow: 0 4px 20px rgba(255,255,255,0.1);
  transform: scale(1.03);
}
```

### 霓虹按钮（赛博朋克）
```css
.neon-btn {
  border: 2px solid var(--c-accent);
  color: var(--c-accent);
  box-shadow: 0 0 12px rgba(200,148,74,0.3), inset 0 0 12px rgba(200,148,74,0.1);
}
.neon-btn:hover {
  box-shadow: 0 0 24px rgba(200,148,74,0.5), inset 0 0 24px rgba(200,148,74,0.2);
}
```

## 二、输入框系统

```css
.modern-input {
  border: 1.5px solid var(--border);
  border-radius: 12px;
  padding: 12px 18px;
  font-size: 14px;
  background: var(--bg-surface);
  transition: all 0.2s;
}
.modern-input:focus {
  border-color: var(--c-accent);
  box-shadow: 0 0 0 3px rgba(200,148,74,0.15);
  outline: none;
}
```

## 三、卡片组件

### 3D Tilt Card（倾斜卡片）
鼠标移动时卡片轻微倾斜，模拟真实空间感。
```js
card.addEventListener('mousemove', (e) => {
  const rect = card.getBoundingClientRect();
  const x = (e.clientX - rect.left) / rect.width - 0.5;
  const y = (e.clientY - rect.top) / rect.height - 0.5;
  card.style.transform = `perspective(800px) rotateY(${x*8}deg) rotateX(${-y*8}deg)`;
});
card.addEventListener('mouseleave', () => {
  card.style.transform = 'perspective(800px) rotateY(0) rotateX(0)';
});
```

## 四、Skeleton Loading（骨架屏）
```css
.skeleton {
  background: linear-gradient(90deg, var(--border) 25%, transparent 50%, var(--border) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.5s infinite;
  border-radius: 8px;
}
@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
```

## 五、Toggle Switch（液态开关）
```css
.toggle {
  width: 52px; height: 28px; border-radius: 14px;
  background: var(--border); position: relative; cursor: pointer;
  transition: background 0.3s;
}
.toggle.active { background: var(--c-primary); }
.toggle::after {
  content: ''; position: absolute; top: 2px; left: 2px;
  width: 24px; height: 24px; border-radius: 50%;
  background: #fff; box-shadow: 0 2px 4px rgba(0,0,0,0.2);
  transition: transform 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
.toggle.active::after { transform: translateX(24px); }
```
