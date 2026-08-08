/* ZCode mirror — AI chat widget.
 * Injected by collector/inject_widget.py alongside chat-widget.css.
 *
 * Posts questions to the same-origin /api/ask endpoint (served by the intranet
 * FastAPI container). Renders a lightweight Markdown subset + source chips.
 * Zero external dependencies; safe to run on a fully offline static mirror.
 */
(function () {
  "use strict";
  if (window.__ZCODE_CHAT_LOADED__) return;
  window.__ZCODE_CHAT_LOADED__ = true;

  var ASK_URL = "/api/ask";
  var MODELS_URL = "/api/models";
  var STORAGE_KEY = "zc-chat-history";
  var MODEL_KEY = "zc-chat-model";
  var MAX_HISTORY = 40;

  // ---- minimal Markdown renderer (inline + fenced code) ---------------------
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function renderInline(text) {
    // code spans, bold, links, then escape happened already on input segments
    return text
      .replace(/`([^`]+)`/g, function (_, c) { return "<code>" + escapeHtml(c) + "</code>"; })
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (_, t, u) {
        return '<a href="' + escapeHtml(u) + '" target="_blank" rel="noopener">' + escapeHtml(t) + "</a>";
      });
  }
  function renderMarkdown(md) {
    if (!md) return "";
    var lines = escapeHtml(md).split(/\r?\n/);
    var html = "", inCode = false, codeBuf = [], paraBuf = [];

    function flushPara() {
      if (paraBuf.length) {
        html += "<p>" + renderInline(paraBuf.join(" ")) + "</p>";
        paraBuf = [];
      }
    }
    function flushCode() {
      if (codeBuf.length) {
        html += '<pre><code>' + codeBuf.join("\n") + "</code></pre>";
        codeBuf = [];
      }
    }
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var fence = line.match(/^```(.*)$/);
      if (fence) {
        if (inCode) { flushCode(); inCode = false; }
        else { flushPara(); inCode = true; }
        continue;
      }
      if (inCode) { codeBuf.push(line); continue; }
      if (/^\s*$/.test(line)) { flushPara(); continue; }
      if (/^#{1,3}\s+/.test(line)) {
        flushPara();
        var lvl = line.match(/^(#{1,3})/)[1].length;
        html += "<h" + lvl + ">" + renderInline(line.replace(/^#{1,3}\s+/, "")) + "</h" + lvl + ">";
        continue;
      }
      if (/^[-*]\s+/.test(line)) {
        flushPara();
        html += "<li>" + renderInline(line.replace(/^[-*]\s+/, "")) + "</li>";
        continue;
      }
      paraBuf.push(line);
    }
    flushPara(); flushCode();
    return html;
  }

  // ---- DOM ------------------------------------------------------------------
  var btn = document.createElement("button");
  btn.className = "zc-chat-btn";
  btn.setAttribute("aria-label", "AI 问答");
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';
  document.body.appendChild(btn);

  var panel = document.createElement("div");
  panel.className = "zc-chat-panel";
  panel.innerHTML =
    '<div class="zc-chat-header">' +
      '<div style="flex:1"><div class="zc-title">ZCode 文档助手</div><div class="zc-sub">基于配置的大模型 · 引用本站文档</div></div>' +
      '<button class="zc-chat-close" aria-label="关闭">×</button>' +
    '</div>' +
    '<div class="zc-chat-model-bar" hidden>' +
      '<label>模型 <select class="zc-chat-model" aria-label="选择模型"></select></label>' +
    '</div>' +
    '<div class="zc-chat-messages"></div>' +
    '<div class="zc-chat-input-wrap">' +
      '<textarea class="zc-chat-input" rows="1" placeholder="问问关于 ZCode 的使用问题…"></textarea>' +
      '<button class="zc-chat-send" aria-label="发送">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>' +
      '</button>' +
    '</div>' +
    '<div class="zc-chat-hint">回答由配置的模型生成，仅供参考。请核对引用来源。</div>';
  document.body.appendChild(panel);

  var msgsEl = panel.querySelector(".zc-chat-messages");
  var inputEl = panel.querySelector(".zc-chat-input");
  var sendBtn = panel.querySelector(".zc-chat-send");
  var closeBtn = panel.querySelector(".zc-chat-close");
  var modelBar = panel.querySelector(".zc-chat-model-bar");
  var modelSel = panel.querySelector(".zc-chat-model");
  var selectedModelId = "";
  try { selectedModelId = sessionStorage.getItem(MODEL_KEY) || ""; } catch (e) { selectedModelId = ""; }

  function loadModels() {
    fetch(MODELS_URL, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !data.models || !data.models.length) {
          modelBar.hidden = true;
          return;
        }
        var active = data.active_id || "";
        if (!selectedModelId || !data.models.some(function (m) { return m.id === selectedModelId; })) {
          selectedModelId = active || data.models[0].id;
        }
        modelSel.innerHTML = data.models.map(function (m) {
          return '<option value="' + escapeHtml(m.id) + '"' +
            (m.id === selectedModelId ? " selected" : "") + ">" +
            escapeHtml(m.label || m.model) + "</option>";
        }).join("");
        // Only show the picker when there is more than one profile.
        modelBar.hidden = data.models.length < 2;
      })
      .catch(function () { modelBar.hidden = true; });
  }
  modelSel.addEventListener("change", function () {
    selectedModelId = modelSel.value;
    try { sessionStorage.setItem(MODEL_KEY, selectedModelId); } catch (e) {}
  });
  loadModels();

  // ---- state ---------------------------------------------------------------
  var history = [];
  try { history = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "[]"); } catch (e) { history = []; }

  function persist() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(history.slice(-MAX_HISTORY))); } catch (e) {}
  }

  function addMessage(role, text, sources) {
    var item = { role: role, text: text };
    if (sources) item.sources = sources;
    history.push(item);
    persist();
    renderItem(item);
  }

  function renderItem(item) {
    var div = document.createElement("div");
    div.className = "zc-msg " + (item.role === "user" ? "zc-user" : "zc-bot");
    var bubble = document.createElement("div");
    bubble.className = "zc-bubble";
    bubble.innerHTML = item.role === "user" ? escapeHtml(item.text).replace(/\n/g, "<br>") : renderMarkdown(item.text);
    div.appendChild(bubble);
    if (item.sources && item.sources.length) {
      var s = document.createElement("div");
      s.className = "zc-sources";
      s.appendChild(document.createTextNode("来源："));
      item.sources.forEach(function (src) {
        var a = document.createElement("a");
        a.className = "zc-source-chip";
        a.textContent = src.title || src.url;
        a.title = src.snippet ? src.snippet.slice(0, 120) : "";
        a.href = src.url || "#";
        s.appendChild(a);
      });
      div.appendChild(s);
    }
    msgsEl.appendChild(div);
    msgsEl.scrollTop = msgsEl.scrollHeight;
    return div;
  }

  function showTyping() {
    var div = document.createElement("div");
    div.className = "zc-msg zc-bot zc-typing-msg";
    div.innerHTML = '<div class="zc-bubble"><span class="zc-typing"><span></span><span></span><span></span></span></div>';
    msgsEl.appendChild(div);
    msgsEl.scrollTop = msgsEl.scrollHeight;
    return div;
  }

  function showError(msg) {
    var div = document.createElement("div");
    div.className = "zc-msg zc-bot";
    div.innerHTML = '<div class="zc-bubble zc-chat-error">' + escapeHtml(msg) + "</div>";
    msgsEl.appendChild(div);
    msgsEl.scrollTop = msgsEl.scrollHeight;
  }

  // ---- initial greeting ----------------------------------------------------
  function renderHistory() {
    msgsEl.innerHTML = "";
    if (!history.length) {
      addMessage("bot", "你好！我是 ZCode 文档助手。你可以问我关于 ZCode 安装、Agent、自动化、MCP 等任何使用问题，我会基于本站文档回答并给出来源。");
    } else {
      history.forEach(renderItem);
    }
  }

  // ---- send ----------------------------------------------------------------
  var sending = false;
  function send() {
    var q = inputEl.value.trim();
    if (!q || sending) return;
    sending = true;
    sendBtn.disabled = true;
    inputEl.value = "";
    inputEl.style.height = "auto";
    addMessage("user", q);
    var typing = showTyping();

    var body = new FormData();
    body.append("question", q);
    if (selectedModelId) body.append("model_id", selectedModelId);
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 60000);

    fetch(ASK_URL, { method: "POST", body: body, signal: ctrl.signal })
      .then(function (r) {
        clearTimeout(timer);
        if (!r.ok) throw new Error("服务返回 " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
        addMessage("bot", data.answer || "(无回答)", data.sources || []);
      })
      .catch(function (err) {
        clearTimeout(timer);
        if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
        var msg = err.name === "AbortError"
          ? "请求超时，请稍后重试。"
          : ("问答服务暂不可用：" + (err.message || err) + "。请确认内网 AI 服务已启动。");
        showError(msg);
      })
      .finally(function () { sending = false; sendBtn.disabled = false; inputEl.focus(); });
  }

  // ---- events --------------------------------------------------------------
  btn.addEventListener("click", function () {
    var open = panel.classList.toggle("zc-open");
    btn.style.display = open ? "none" : "flex";
    if (open) { renderHistory(); inputEl.focus(); }
  });
  closeBtn.addEventListener("click", function () {
    panel.classList.remove("zc-open");
    btn.style.display = "flex";
  });
  sendBtn.addEventListener("click", send);
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  inputEl.addEventListener("input", function () {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 100) + "px";
  });

  // ---- wire the header "登录" button to the admin console -----------------
  // The original button is an OAuth gate (dead offline); repurpose it as the
  // entry to the admin console at /admin.
  function gotoAdmin() { window.location.href = "/admin/"; }
  document.querySelectorAll('button[aria-label="登录"]').forEach(function (b) {
    b.addEventListener("click", gotoAdmin);
    b.setAttribute("title", "管理后台");
  });

  // ---- header "社区" dropdown → Skill page --------------------------------
  // Offline mirror cannot hydrate the original hover-card; replace it with a
  // static menu that links to the LAN skill library at /skills/.
  function closeCommunityMenus() {
    document.querySelectorAll(".zc-community-menu").forEach(function (m) {
      m.hidden = true;
    });
    document.querySelectorAll('button[aria-label="社区"]').forEach(function (b) {
      b.setAttribute("data-state", "closed");
      b.setAttribute("aria-expanded", "false");
    });
  }

  function wireCommunityMenu(btn) {
    var wrap = btn.parentElement;
    if (!wrap || wrap.dataset.zcSkillsMenu === "1") return;
    wrap.dataset.zcSkillsMenu = "1";
    wrap.classList.add("zc-community-wrap");
    if (window.getComputedStyle(wrap).position === "static") {
      wrap.style.position = "relative";
    }

    var menu = document.createElement("div");
    menu.className = "zc-community-menu";
    menu.hidden = true;
    menu.setAttribute("role", "menu");
    menu.innerHTML =
      '<a class="zc-community-item" role="menuitem" href="/skills/">' +
        '<span class="zc-community-item-title">社区 Skills</span>' +
        '<span class="zc-community-item-sub">内网共享与下载</span>' +
      "</a>";
    wrap.appendChild(menu);

    btn.setAttribute("aria-haspopup", "menu");
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("title", "社区 · Skill");

    btn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var open = menu.hidden;
      closeCommunityMenus();
      if (open) {
        menu.hidden = false;
        btn.setAttribute("data-state", "open");
        btn.setAttribute("aria-expanded", "true");
      }
    }, true);
  }

  document.querySelectorAll('button[aria-label="社区"]').forEach(wireCommunityMenu);
  document.addEventListener("click", function (e) {
    if (e.target && e.target.closest && e.target.closest(".zc-community-wrap")) return;
    closeCommunityMenus();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeCommunityMenus();
  });

  // Expose for debugging.
  window.__ZCODE_CHAT__ = { send: send, clear: function () { history = []; persist(); renderHistory(); } };
})();
