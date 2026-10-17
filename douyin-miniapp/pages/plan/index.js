const { request, CONFIG } = require("../../utils/request");

Page({
  data: {
    city: CONFIG.DEFAULT_CITY,
    days: "2",
    budget: "人均 800 元",
    preference: "拍照、美食、宋韵文化、轻松不赶路",
    reply: "",
    loading: false
  },

  onCityInput(e) {
    this.setData({ city: e.detail.value });
  },
  onDaysInput(e) {
    this.setData({ days: e.detail.value });
  },
  onBudgetInput(e) {
    this.setData({ budget: e.detail.value });
  },
  onPreferenceInput(e) {
    this.setData({ preference: e.detail.value });
  },

  async generatePlan() {
    if (this.data.loading) return;
    const city = (this.data.city || "").trim();
    if (!city) {
      tt.showToast({ title: "请填写目的地", icon: "none" });
      return;
    }

    this.setData({ loading: true, reply: "" });
    try {
      const tags = (this.data.preference || "")
        .split(/[，,、\s]+/)
        .map(item => item.trim())
        .filter(Boolean);
      const data = await request("/api/plan", {
        method: "POST",
        data: {
          city,
          days: Number(this.data.days) || 2,
          budget: this.data.budget || "舒适型",
          tags,
          stream: false
        }
      });
      this.setData({ reply: data.reply || "暂未生成行程，请稍后再试。" });
    } catch (err) {
      this.setData({ reply: "连接服务失败。请确认后端 HTTPS 域名已配置，并且 `/api/plan` 可访问。" });
    } finally {
      this.setData({ loading: false });
    }
  }
});
