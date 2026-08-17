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
  var GEOM_KEY = "zc-chat-geom";
  var DISMISS_KEY = "zc-chat-dismissed";
  var MAX_HISTORY = 40;
  var MIN_W = 320;
  var MIN_H = 380;
  var SUGGESTIONS = [
    "如何安装 ZCode？",
    "怎么配置 MCP 服务？",
    "Agent 和 Subagent 有什么区别？",
    "常用快捷键有哪些？"
  ];

  // ---- minimal Markdown renderer (inline + fenced code) ---------------------
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function renderInline(text) {
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
    var listType = null;

    function flushPara() {
      if (paraBuf.length) {
        html += "<p>" + renderInline(paraBuf.join(" ")) + "</p>";
        paraBuf = [];
      }
    }
    function flushList() {
      if (listType) {
        html += listType === "ol" ? "</ol>" : "</ul>";
        listType = null;
      }
    }
    function flushCode() {
      if (codeBuf.length) {
        html += '<div class="zc-code-wrap"><button type="button" class="zc-copy-code">复制</button><pre><code>' +
          codeBuf.join("\n") + "</code></pre></div>";
        codeBuf = [];
      }
    }
    function openList(type) {
      if (listType !== type) {
        flushPara();
        flushList();
        html += type === "ol" ? "<ol>" : "<ul>";
        listType = type;
      }
    }
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var fence = line.match(/^```(.*)$/);
      if (fence) {
        if (inCode) { flushCode(); inCode = false; }
        else { flushPara(); flushList(); inCode = true; }
        continue;
      }
      if (inCode) { codeBuf.push(line); continue; }
      if (/^\s*$/.test(line)) { flushPara(); flushList(); continue; }
      if (/^#{1,3}\s+/.test(line)) {
        flushPara(); flushList();
        var lvl = line.match(/^(#{1,3})/)[1].length;
        html += "<h" + lvl + ">" + renderInline(line.replace(/^#{1,3}\s+/, "")) + "</h" + lvl + ">";
        continue;
      }
      if (/^>\s?/.test(line)) {
        flushPara(); flushList();
        html += "<blockquote>" + renderInline(line.replace(/^>\s?/, "")) + "</blockquote>";
        continue;
      }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
        flushPara(); flushList();
        html += "<hr>";
        continue;
      }
      var ul = line.match(/^[-*]\s+(.*)$/);
      if (ul) {
        openList("ul");
        html += "<li>" + renderInline(ul[1]) + "</li>";
        continue;
      }
      var ol = line.match(/^\d+\.\s+(.*)$/);
      if (ol) {
        openList("ol");
        html += "<li>" + renderInline(ol[1]) + "</li>";
        continue;
      }
      flushList();
      paraBuf.push(line);
    }
    flushPara(); flushList(); flushCode();
    return html;
  }

  function isDocsPage() {
    return /(?:^|\/)(?:cn\/)?docs(?:\/|$)/.test(location.pathname || "");
  }
  function isMobile() {
    return window.matchMedia && window.matchMedia("(max-width: 480px)").matches;
  }
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    return Promise.resolve();
  }

  // ---- DOM ------------------------------------------------------------------
  var btn = document.createElement("button");
  btn.className = "zc-chat-btn";
  btn.setAttribute("aria-label", "打开 ZCode 文档助手");
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';
  document.body.appendChild(btn);

  var panel = document.createElement("div");
  panel.className = "zc-chat-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "ZCode 文档助手");
  panel.innerHTML =
    '<div class="zc-chat-header">' +
      '<span class="zc-chat-drag" aria-hidden="true"><i></i><i></i><i></i></span>' +
      '<div style="flex:1;min-width:0"><div class="zc-title">ZCode 文档助手</div><div class="zc-sub">基于本站文档回答 · 可拖拽移动</div></div>' +
      '<div class="zc-chat-header-actions">' +
        '<button type="button" class="zc-chat-iconbtn zc-chat-clear" title="清空对话" aria-label="清空对话">' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>' +
        '</button>' +
        '<button type="button" class="zc-chat-iconbtn zc-chat-close" aria-label="关闭">×</button>' +
      '</div>' +
    '</div>' +
    '<div class="zc-chat-model-bar" hidden>' +
      '<label>模型 <select class="zc-chat-model" aria-label="选择模型"></select></label>' +
    '</div>' +
    '<div class="zc-chat-messages"></div>' +
    '<div class="zc-chat-composer">' +
      '<div class="zc-chat-input-wrap">' +
        '<textarea class="zc-chat-input" rows="1" placeholder="问一下 ZCode 怎么用… Enter 发送"></textarea>' +
        '<button type="button" class="zc-chat-send" aria-label="发送" disabled>' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>' +
        '</button>' +
      '</div>' +
      '<div class="zc-chat-hint">Enter 发送 · Shift+Enter 换行 · Esc 关闭 · 拖标题栏移动 · 右下角缩放</div>' +
    '</div>' +
    '<div class="zc-chat-resize" title="拖动调整大小"></div>';
  document.body.appendChild(panel);

  var headerEl = panel.querySelector(".zc-chat-header");
  var msgsEl = panel.querySelector(".zc-chat-messages");
  var inputEl = panel.querySelector(".zc-chat-input");
  var sendBtn = panel.querySelector(".zc-chat-send");
  var closeBtn = panel.querySelector(".zc-chat-close");
  var clearBtn = panel.querySelector(".zc-chat-clear");
  var resizeEl = panel.querySelector(".zc-chat-resize");
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
        modelBar.hidden = data.models.length < 2;
      })
      .catch(function () { modelBar.hidden = true; });
  }
  modelSel.addEventListener("change", function () {
    selectedModelId = modelSel.value;
    try { sessionStorage.setItem(MODEL_KEY, selectedModelId); } catch (e) {}
  });
  loadModels();

  // ---- geometry (drag + resize) --------------------------------------------
  function loadGeom() {
    try { return JSON.parse(localStorage.getItem(GEOM_KEY) || "null"); } catch (e) { return null; }
  }
  function saveGeom() {
    if (isMobile()) return;
    var r = panel.getBoundingClientRect();
    try {
      localStorage.setItem(GEOM_KEY, JSON.stringify({
        x: Math.round(r.left), y: Math.round(r.top),
        w: Math.round(r.width), h: Math.round(r.height)
      }));
    } catch (e) {}
  }
  function clamp(n, min, max) { return Math.max(min, Math.min(max, n)); }
  function applyBox(x, y, w, h) {
    var maxW = Math.max(MIN_W, window.innerWidth - 16);
    var maxH = Math.max(MIN_H, window.innerHeight - 16);
    w = clamp(w, MIN_W, maxW);
    h = clamp(h, MIN_H, maxH);
    x = clamp(x, 8, window.innerWidth - w - 8);
    y = clamp(y, 8, window.innerHeight - h - 8);
    panel.style.left = x + "px";
    panel.style.top = y + "px";
    panel.style.right = "auto";
    panel.style.bottom = "auto";
    panel.style.width = w + "px";
    panel.style.height = h + "px";
  }
  function restoreGeom() {
    if (isMobile()) {
      panel.style.left = panel.style.top = panel.style.right = panel.style.bottom = "";
      panel.style.width = panel.style.height = "";
      return;
    }
    var g = loadGeom();
    if (!g) return;
    applyBox(g.x, g.y, g.w || panel.offsetWidth, g.h || panel.offsetHeight);
  }
  function pinToPixels() {
    var r = panel.getBoundingClientRect();
    applyBox(r.left, r.top, r.width, r.height);
  }

  function bindPointerDrag(handle, onMove) {
    handle.addEventListener("pointerdown", function (e) {
      if (isMobile()) return;
      if (e.button != null && e.button !== 0) return;
      if (e.target && e.target.closest && e.target.closest("button, select, a, textarea, input")) return;
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      var startX = e.clientX, startY = e.clientY;
      pinToPixels();
      var r = panel.getBoundingClientRect();
      var ox = r.left, oy = r.top, ow = r.width, oh = r.height;
      function move(ev) { onMove(ev.clientX - startX, ev.clientY - startY, ox, oy, ow, oh); }
      function up() {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        handle.removeEventListener("pointercancel", up);
        panel.classList.remove("zc-dragging", "zc-resizing");
        headerEl.classList.remove("zc-dragging");
        saveGeom();
      }
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
      handle.addEventListener("pointercancel", up);
    });
  }
  bindPointerDrag(headerEl, function (dx, dy, ox, oy, ow, oh) {
    panel.classList.add("zc-dragging");
    headerEl.classList.add("zc-dragging");
    applyBox(ox + dx, oy + dy, ow, oh);
  });
  bindPointerDrag(resizeEl, function (dx, dy, ox, oy, ow, oh) {
    panel.classList.add("zc-resizing");
    applyBox(ox, oy, ow + dx, oh + dy);
  });
  window.addEventListener("resize", function () {
    if (!panel.classList.contains("zc-open") || isMobile()) return;
    var r = panel.getBoundingClientRect();
    applyBox(r.left, r.top, r.width, r.height);
  });

  // ---- state ---------------------------------------------------------------
  var history = [];
  try { history = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "[]"); } catch (e) { history = []; }

  function persist() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(history.slice(-MAX_HISTORY))); } catch (e) {}
  }

  function syncSendEnabled() {
    sendBtn.disabled = sending || !inputEl.value.trim();
  }

  function addMessage(role, text, sources) {
    var item = { role: role, text: text };
    if (sources) item.sources = sources;
    history.push(item);
    persist();
    renderItem(item);
    hideSuggestions();
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
    if (item.role === "bot" && item.text) {
      var copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "zc-msg-copy";
      copyBtn.textContent = "复制";
      copyBtn.addEventListener("click", function () {
        copyText(item.text).then(function () {
          copyBtn.textContent = "已复制";
          setTimeout(function () { copyBtn.textContent = "复制"; }, 1200);
        });
      });
      div.appendChild(copyBtn);
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

  function hideSuggestions() {
    var el = msgsEl.querySelector(".zc-suggest");
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  function showSuggestions() {
    hideSuggestions();
    var wrap = document.createElement("div");
    wrap.className = "zc-suggest";
    SUGGESTIONS.forEach(function (q) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "zc-suggest-chip";
      chip.textContent = q;
      chip.addEventListener("click", function () {
        inputEl.value = q;
        autosize();
        syncSendEnabled();
        send();
      });
      wrap.appendChild(chip);
    });
    msgsEl.appendChild(wrap);
  }

  function hasUserTurns() {
    return history.some(function (item) { return item.role === "user"; });
  }

  // ---- initial greeting ----------------------------------------------------
  function renderHistory() {
    msgsEl.innerHTML = "";
    if (!history.length) {
      var greeting = {
        role: "bot",
        text: "你好！我是 ZCode 文档助手。你可以问我关于安装、Agent、自动化、MCP 等任何使用问题，我会基于本站文档回答并给出来源。"
      };
      history.push(greeting);
      persist();
      renderItem(greeting);
      showSuggestions();
    } else {
      history.forEach(renderItem);
      if (!hasUserTurns()) showSuggestions();
    }
  }

  function autosize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
  }

  // ---- send ----------------------------------------------------------------
  var sending = false;
  function send() {
    var q = inputEl.value.trim();
    if (!q || sending) return;
    sending = true;
    syncSendEnabled();
    inputEl.value = "";
    autosize();
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
      .finally(function () {
        sending = false;
        syncSendEnabled();
        inputEl.focus();
      });
  }

  function openPanel(opts) {
    opts = opts || {};
    panel.classList.add("zc-open");
    btn.style.display = "none";
    btn.classList.remove("zc-pulse");
    restoreGeom();
    renderHistory();
    if (!opts.keepDismissed) {
      try { sessionStorage.removeItem(DISMISS_KEY); } catch (e) {}
    }
    setTimeout(function () { inputEl.focus(); }, 40);
  }
  function closePanel() {
    panel.classList.remove("zc-open");
    btn.style.display = "flex";
    try { sessionStorage.setItem(DISMISS_KEY, "1"); } catch (e) {}
    if (isDocsPage()) btn.classList.add("zc-pulse");
  }

  // ---- events --------------------------------------------------------------
  btn.addEventListener("click", function () { openPanel(); });
  closeBtn.addEventListener("click", closePanel);
  clearBtn.addEventListener("click", function () {
    history = [];
    persist();
    renderHistory();
    inputEl.focus();
  });
  sendBtn.addEventListener("click", send);
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  inputEl.addEventListener("input", function () {
    autosize();
    syncSendEnabled();
  });
  msgsEl.addEventListener("click", function (e) {
    var copy = e.target && e.target.closest && e.target.closest(".zc-copy-code");
    if (!copy) return;
    var pre = copy.parentNode && copy.parentNode.querySelector("pre");
    if (!pre) return;
    copyText(pre.textContent || "").then(function () {
      copy.classList.add("zc-copied");
      copy.textContent = "已复制";
      setTimeout(function () {
        copy.classList.remove("zc-copied");
        copy.textContent = "复制";
      }, 1200);
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && panel.classList.contains("zc-open")) {
      if (e.target && (e.target.tagName === "SELECT" || e.target.tagName === "TEXTAREA") && e.target.value) return;
      closePanel();
    }
  });

  // Docs pages open the assistant by default so more people notice it.
  // Closing it only dismisses for the rest of this tab session.
  if (isDocsPage()) {
    var dismissed = false;
    try { dismissed = sessionStorage.getItem(DISMISS_KEY) === "1"; } catch (e) {}
    if (dismissed) {
      btn.classList.add("zc-pulse");
    } else {
      setTimeout(function () { openPanel({ keepDismissed: true }); }, 280);
    }
  }

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

  function wireCommunityMenu(btnEl) {
    var wrap = btnEl.parentElement;
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
        '<span class="zc-community-item-sub">精选上架、索引与投稿</span>' +
      "</a>";
    wrap.appendChild(menu);

    btnEl.setAttribute("aria-haspopup", "menu");
    btnEl.setAttribute("aria-expanded", "false");
    btnEl.setAttribute("title", "社区 · Skill");

    btnEl.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var open = menu.hidden;
      closeCommunityMenus();
      if (open) {
        menu.hidden = false;
        btnEl.setAttribute("data-state", "open");
        btnEl.setAttribute("aria-expanded", "true");
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
  window.__ZCODE_CHAT__ = {
    send: send,
    open: openPanel,
    close: closePanel,
    clear: function () { history = []; persist(); renderHistory(); }
  };
})();
