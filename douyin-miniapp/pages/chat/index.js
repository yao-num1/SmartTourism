const { request } = require("../../utils/request");

function createMessage(role, content) {
  return {
    id: "m" + Date.now() + Math.floor(Math.random() * 1000),
    role,
    content
  };
}

Page({
  data: {
    message: "",
    messages: [
      createMessage("assistant", "你好，我是智旅灵犀。你可以问我目的地攻略、路线安排、美食推荐和出行避坑。")
    ],
    loading: false,
    scrollTo: ""
  },

  onMessageInput(e) {
    this.setData({ message: e.detail.value });
  },

  useQuick(e) {
    this.setData({ message: e.currentTarget.dataset.text || "" });
  },

  async sendMessage() {
    const text = (this.data.message || "").trim();
    if (!text || this.data.loading) return;

    const app = getApp();
    const userMsg = createMessage("user", text);
    const nextMessages = this.data.messages.concat(userMsg);
    this.setData({
      messages: nextMessages,
      message: "",
      loading: true,
      scrollTo: userMsg.id
    });

    try {
      const data = await request("/api/chat", {
        method: "POST",
        data: {
          message: text,
          skill: "consult",
          conversation_id: app.globalData.conversationId || "",
          stream: false
        }
      });
      if (data.conversation_id) {
        app.globalData.conversationId = data.conversation_id;
        tt.setStorageSync("conversation_id", data.conversation_id);
      }
      const reply = createMessage("assistant", data.reply || "我暂时没有生成有效回复，请稍后再试。");
      this.setData({
        messages: this.data.messages.concat(reply),
        scrollTo: reply.id
      });
    } catch (err) {
      const errorMsg = createMessage("assistant", "连接服务失败。请确认后端 HTTPS 域名已配置，并且服务可访问。");
      this.setData({
        messages: this.data.messages.concat(errorMsg),
        scrollTo: errorMsg.id
      });
    } finally {
      this.setData({ loading: false });
    }
  }
});
