App({
  globalData: {
    appName: "智旅灵犀",
    conversationId: ""
  },
  onLaunch() {
    const savedConversationId = tt.getStorageSync("conversation_id");
    if (savedConversationId) {
      this.globalData.conversationId = savedConversationId;
    }
  }
});
