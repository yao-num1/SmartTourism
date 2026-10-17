const { request, CONFIG } = require("../../utils/request");

const DEFAULT_POSTS = [
  {
    id: "p1",
    title: "杭州 2 天 1 晚宋韵 Citywalk",
    author: "阿晚",
    tag: "路线攻略",
    views: 3420,
    favs: 612,
    content: "第一天走西湖和南宋御街，第二天去运河边喝茶看展，晚上安排夜游更有氛围。"
  },
  {
    id: "p2",
    title: "龙井虾仁之外，本地小馆更稳",
    author: "七月饭团",
    tag: "美食餐饮",
    views: 2880,
    favs: 845,
    content: "不要只盯热门商圈，居民区附近的小馆更稳定，适合安排在行程中段。"
  },
  {
    id: "p3",
    title: "运河夜游拍照机位",
    author: "橘子相机",
    tag: "拍照打卡",
    views: 4120,
    favs: 1090,
    content: "蓝调时刻比完全天黑更好拍，桥洞、灯影和船只都能形成层次。"
  }
];

function sortBy(list, field) {
  return list.slice().sort((a, b) => b[field] - a[field]).slice(0, 5);
}

Page({
  data: {
    posts: [],
    clickRank: [],
    favRank: [],
    hotspots: [],
    hotspotStatus: "正在读取官方/授权平台热点...",
    draftTitle: "",
    draftTag: "",
    draftContent: ""
  },

  onLoad() {
    this.loadPosts();
    this.loadHotspots();
  },

  loadPosts() {
    const saved = tt.getStorageSync("community_posts");
    const posts = Array.isArray(saved) && saved.length ? saved : DEFAULT_POSTS;
    this.refreshPosts(posts);
  },

  refreshPosts(posts) {
    this.setData({
      posts,
      clickRank: sortBy(posts, "views"),
      favRank: sortBy(posts, "favs")
    });
    tt.setStorageSync("community_posts", posts);
  },

  onTitleInput(e) {
    this.setData({ draftTitle: e.detail.value });
  },
  onTagInput(e) {
    this.setData({ draftTag: e.detail.value });
  },
  onContentInput(e) {
    this.setData({ draftContent: e.detail.value });
  },

  publishPost() {
    const title = (this.data.draftTitle || "").trim();
    const content = (this.data.draftContent || "").trim();
    if (!title || !content) {
      tt.showToast({ title: "请填写标题和内容", icon: "none" });
      return;
    }
    const post = {
      id: "p" + Date.now(),
      title,
      author: "旅行者",
      tag: this.data.draftTag || "用户攻略",
      views: 1,
      favs: 0,
      content
    };
    this.refreshPosts([post].concat(this.data.posts));
    this.setData({ draftTitle: "", draftTag: "", draftContent: "" });
    tt.showToast({ title: "已发布", icon: "success" });
  },

  viewPost(e) {
    const id = e.currentTarget.dataset.id;
    const posts = this.data.posts.map(post => post.id === id ? Object.assign({}, post, { views: post.views + 1 }) : post);
    this.refreshPosts(posts);
  },

  favPost(e) {
    const id = e.currentTarget.dataset.id;
    const posts = this.data.posts.map(post => post.id === id ? Object.assign({}, post, { favs: post.favs + 1 }) : post);
    this.refreshPosts(posts);
  },

  async loadHotspots() {
    try {
      const data = await request("/api/social/hotspots?keyword=文旅&city=" + encodeURIComponent(CONFIG.DEFAULT_CITY));
      const items = Array.isArray(data.items) ? data.items.slice(0, 6) : [];
      if (items.length) {
        this.setData({ hotspots: items, hotspotStatus: "" });
        return;
      }
      const sources = Array.isArray(data.sources) ? data.sources : [];
      const statusText = sources.map(source => source.provider + "：" + source.message).join("\n\n");
      this.setData({
        hotspotStatus: statusText || "暂未获取到官方热点。请先配置抖音/小红书官方授权接口。"
      });
    } catch (err) {
      this.setData({ hotspotStatus: "官方热点接口暂不可用。请确认后端 HTTPS 域名和安全域名配置。" });
    }
  }
});
