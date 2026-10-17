const CONFIG = require("./config");

function request(path, options) {
  const opts = options || {};
  return new Promise((resolve, reject) => {
    tt.request({
      url: CONFIG.API_BASE_URL + path,
      method: opts.method || "GET",
      data: opts.data || {},
      header: Object.assign({
        "content-type": "application/json"
      }, opts.header || {}),
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data);
          return;
        }
        reject(new Error("接口请求失败：" + res.statusCode));
      },
      fail(err) {
        reject(err);
      }
    });
  });
}

module.exports = {
  request,
  CONFIG
};
