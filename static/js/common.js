/* 通用小工具：fetch 封装、转义、DOM 辅助。 */
(function (global) {
  "use strict";

  async function api(path, options) {
    var opts = options || {};
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    var resp = await fetch(path, opts);
    var data = null;
    try {
      data = await resp.json();
    } catch (e) {
      throw new Error("服务器返回了无法解析的内容（HTTP " + resp.status + "）");
    }
    if (!resp.ok || data.ok === false) {
      var err = new Error(data.error || ("请求失败（HTTP " + resp.status + "）"));
      err.data = data;
      throw err;
    }
    return data;
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
    return Promise.resolve();
  }

  function flash(container, message, kind) {
    container.textContent = message;
    container.className = "flash flash-" + (kind || "info") + " show";
  }

  function clearFlash(container) {
    container.className = "flash";
    container.textContent = "";
  }

  function downloadFile(filename, content, mime) {
    var blob = content instanceof Blob ? content : new Blob([content], { type: mime || "application/octet-stream" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  global.UI = { api: api, esc: esc, copyText: copyText, flash: flash, clearFlash: clearFlash, downloadFile: downloadFile };
})(window);
