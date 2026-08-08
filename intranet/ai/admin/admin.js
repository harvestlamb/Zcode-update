/* ZCode mirror admin UI — talks to /api/admin/* (same origin via nginx /api proxy). */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var modelsCache = [];

  // ---- session bootstrap ----
  api("GET", "/api/admin/state").then(function (s) { showApp(); afterLogin(s); })
    .catch(function () { /* not logged in */ });

  // ---- login ----
  $("login-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var btn = $("li-btn"), err = $("li-err");
    btn.disabled = true; err.hidden = true;
    api("POST", "/api/admin/login", { username: $("li-user").value, password: $("li-pass").value })
      .then(function () { return api("GET", "/api/admin/state"); })
      .then(function (s) { showApp(); afterLogin(s); })
      .catch(function (exc) { err.textContent = (exc && exc.detail) || "登录失败"; err.hidden = false; })
      .finally(function () { btn.disabled = false; });
  });

  $("logout-btn").addEventListener("click", function () {
    api("POST", "/api/admin/logout").finally(function () { location.reload(); });
  });

  // ---- sidebar nav ----
  document.querySelectorAll(".nav-item").forEach(function (t) {
    t.addEventListener("click", function () {
      document.querySelectorAll(".nav-item").forEach(function (x) { x.classList.remove("active"); });
      document.querySelectorAll(".tab-pane").forEach(function (x) { x.classList.remove("active"); });
      t.classList.add("active");
      $("tab-" + t.dataset.tab).classList.add("active");
      if ($("page-title") && t.dataset.title) $("page-title").textContent = t.dataset.title;
      if ($("page-sub") && t.dataset.sub) $("page-sub").textContent = t.dataset.sub;
      if (t.dataset.tab === "overview") loadOverview();
      if (t.dataset.tab === "model") loadModels();
      if (t.dataset.tab === "logs") loadLogs(0);
      if (t.dataset.tab === "import") { loadBackupState(); loadUpgradeHistory(); }
      if (t.dataset.tab === "skills") loadSkills();
    });
  });

  function afterLogin(s) {
    if (s && s.username) $("pw-user").value = s.username;
    loadOverview();
    loadModels();
    loadBackupState(s);
    loadUpgradeHistory();
  }

  // ---- overview ----
  function loadOverview() {
    api("GET", "/api/admin/state").then(function (s) {
      var active = (s.llm && s.llm.active) || null;
      var al = s.ask_log || {};
      $("overview-cards").innerHTML = [
        card("内容版本", s.content_version || "—"),
        card("文档数", s.retriever.documents || 0),
        card("今日提问", al.today || 0),
        card("累计提问", al.total || 0),
        card("今日 Token", formatNum(al.tokens_today || 0)),
        card("累计 Token", formatNum(al.tokens_total || 0)),
      ].join("");
      $("ov-retriever").innerHTML =
        kv("后端", s.retriever.backend) + kv("片段数", s.retriever.chunks) +
        kv("文档数", s.retriever.documents) + kv("内容目录", s.retriever.content_dir) +
        kv("旧版备份", (s.backup && s.backup.available)
          ? ("可用 · " + (s.backup.version || "未知版本"))
          : "无");
      var ul = s.upgrade_log || {};
      if (ul.last) {
        $("ov-retriever").innerHTML +=
          kv("上次升级", (ul.last.ts || "") + " · " + (ul.last.action || "") +
            " · " + (ul.last.from_version || "?") + "→" + (ul.last.to_version || "?"));
      }
      if (active) {
        $("ov-llm").innerHTML =
          kv("名称", active.label) + kv("模型 ID", active.model) +
          kv("服务地址", active.base_url) +
          kv("API Key", active.api_key_set ? "已设置" : "未设置（无鉴权）") +
          kv("输入 Token", formatNum(al.prompt_tokens || 0)) +
          kv("输出 Token", formatNum(al.completion_tokens || 0)) +
          kv("访问 IP 数", al.unique_ips || 0);
      } else {
        $("ov-llm").innerHTML = "<div class='muted'>尚未配置模型，请到「模型」页添加。</div>";
      }
      if (s.username) $("pw-user").value = s.username;
      loadHeatmap();
    }).catch(handleAuth);
  }
  $("ov-test").addEventListener("click", function () {
    runTest($("ov-test-result"), {});
  });
  $("ov-heatmap-refresh").addEventListener("click", function () { loadHeatmap(); });

  function heatLevel(value, max) {
    if (!value || !max) return 0;
    var r = value / max;
    if (r > 0.75) return 4;
    if (r > 0.45) return 3;
    if (r > 0.2) return 2;
    return 1;
  }

  function loadHeatmap() {
    api("GET", "/api/admin/ask-logs/heatmap?days=84").then(function (data) {
      renderCalHeatmap(data);
      renderHourHeatmap(data);
    }).catch(function () { /* ignore if tab mid-logout */ });
  }

  function renderCalHeatmap(data) {
    var days = data.days || [];
    var max = data.max_tokens || 0;
    var el = $("ov-cal-heatmap");
    if (!days.length) {
      el.innerHTML = "<span class='muted'>暂无数据</span>";
      $("ov-heat-summary").textContent = "";
      return;
    }
    // Pad leading empty cells so columns align to weeks (Sun-start rows).
    var firstJs = days[0].js_weekday;
    var cells = [];
    for (var i = 0; i < firstJs; i++) {
      cells.push('<div class="heat-cell" style="visibility:hidden"></div>');
    }
    var rangeTok = 0;
    days.forEach(function (d) {
      rangeTok += d.tokens || 0;
      var lv = heatLevel(d.tokens, max);
      var title = d.date + " · " + formatNum(d.tokens || 0) + " tokens · " + (d.asks || 0) + " 次";
      cells.push('<div class="heat-cell l' + lv + '" title="' + esc(title) + '"></div>');
    });
    el.innerHTML = cells.join("");
    $("ov-heat-summary").textContent =
      data.start + " ~ " + data.end + " · 区间合计 " + formatNum(rangeTok) + " tokens";
  }

  function renderHourHeatmap(data) {
    var grid = data.dow_hour || [];
    var max = data.max_dow_hour || 0;
    var el = $("ov-hour-heatmap");
    var labels = ["一", "二", "三", "四", "五", "六", "日"];
    if (!grid.length) {
      el.innerHTML = "";
      return;
    }
    var html = ['<div></div>'];
    for (var h = 0; h < 24; h++) {
      html.push('<div class="hh-hour">' + (h % 3 === 0 ? h : "") + "</div>");
    }
    for (var r = 0; r < 7; r++) {
      html.push('<div class="hh-label">' + labels[r] + "</div>");
      var row = grid[r] || [];
      for (var c = 0; c < 24; c++) {
        var v = row[c] || 0;
        var lv = heatLevel(v, max);
        var title = "周" + labels[r] + " " + c + ":00 · " + formatNum(v) + " tokens";
        html.push('<div class="hh-cell l' + lv + '" title="' + esc(title) + '"></div>');
      }
    }
    el.innerHTML = html.join("");
  }

  // ---- models ----
  function loadModels() {
    api("GET", "/api/admin/models").then(function (data) {
      modelsCache = data.models || [];
      renderModelList();
    }).catch(handleAuth);
  }

  function renderModelList() {
    var list = $("model-list");
    var empty = $("model-empty");
    if (!modelsCache.length) {
      list.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    list.innerHTML = modelsCache.map(function (m) {
      return (
        '<div class="model-row' + (m.active ? " active" : "") + '" data-id="' + esc(m.id) + '">' +
          '<div class="model-meta">' +
            '<div class="name">' + esc(m.label || m.model) +
              (m.active ? ' <span class="badge">启用中</span>' : "") +
            "</div>" +
            '<div class="sub">' + esc(m.model) + " · " + esc(m.base_url) +
              (m.api_key_set ? " · Key 已设置" : " · 无 Key") +
            "</div>" +
          "</div>" +
          '<div class="model-actions">' +
            (m.active ? "" : '<button type="button" class="btn btn-ghost btn-sm" data-act="activate">设为启用</button>') +
            '<button type="button" class="btn btn-ghost btn-sm" data-act="test">测试</button>' +
            '<button type="button" class="btn btn-ghost btn-sm" data-act="edit">编辑</button>' +
            '<button type="button" class="btn btn-ghost btn-sm" data-act="del">删除</button>' +
          "</div>" +
        "</div>"
      );
    }).join("");
  }

  $("model-list").addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-act]");
    if (!btn) return;
    var row = btn.closest(".model-row");
    if (!row) return;
    var id = row.getAttribute("data-id");
    var m = modelsCache.find(function (x) { return x.id === id; });
    if (!m) return;
    var act = btn.getAttribute("data-act");
    if (act === "edit") openEditor(m);
    if (act === "activate") {
      api("POST", "/api/admin/models/" + encodeURIComponent(id) + "/activate")
        .then(function () { loadModels(); loadOverview(); })
        .catch(function (exc) { alert((exc && exc.detail) || "切换失败"); });
    }
    if (act === "test") {
      btn.disabled = true;
      api("POST", "/api/admin/models/test", { id: id })
        .then(function (r) {
          alert(r.ok
            ? ("连接成功" + (r.models && r.models.length ? "\n可用: " + r.models.slice(0, 8).join(", ") : "") + (r.error ? "\n提示: " + r.error : ""))
            : ("连接失败: " + (r.error || "未知错误")));
        })
        .catch(function (exc) { alert((exc && exc.detail) || "测试失败"); })
        .finally(function () { btn.disabled = false; });
    }
    if (act === "del") {
      if (!confirm("确定删除模型「" + (m.label || m.model) + "」？")) return;
      api("DELETE", "/api/admin/models/" + encodeURIComponent(id))
        .then(function () { hideEditor(); loadModels(); loadOverview(); })
        .catch(function (exc) { alert((exc && exc.detail) || "删除失败"); });
    }
  });

  $("model-add").addEventListener("click", function () { openEditor(null); });
  $("m-cancel").addEventListener("click", hideEditor);

  function openEditor(m) {
    $("model-form").hidden = false;
    $("m-msg").textContent = "";
    $("m-msg").className = "muted";
    if (m) {
      $("model-form-title").textContent = "编辑模型";
      $("m-id").value = m.id;
      $("m-label").value = m.label || "";
      $("m-base_url").value = m.base_url || "";
      $("m-model").value = m.model || "";
      $("m-api_key").value = "";
      $("m-api_key").placeholder = m.api_key_set ? "已保存，留空保持不变" : "无鉴权可留空";
    } else {
      $("model-form-title").textContent = "添加模型";
      $("m-id").value = "";
      $("m-label").value = "";
      $("m-base_url").value = "";
      $("m-model").value = "";
      $("m-api_key").value = "";
      $("m-api_key").placeholder = "无鉴权可留空";
    }
    $("m-label").focus();
  }
  function hideEditor() {
    $("model-form").hidden = true;
    $("m-msg").textContent = "";
  }

  $("model-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var msg = $("m-msg");
    msg.textContent = "保存中…"; msg.className = "muted";
    var id = $("m-id").value;
    var body = {
      label: $("m-label").value.trim(),
      base_url: $("m-base_url").value.trim(),
      model: $("m-model").value.trim(),
      api_key: $("m-api_key").value,
    };
    var req = id
      ? api("PUT", "/api/admin/models/" + encodeURIComponent(id), body)
      : api("POST", "/api/admin/models", body);
    req.then(function () {
      msg.textContent = "✓ 已保存"; msg.className = "";
      hideEditor();
      loadModels();
      loadOverview();
    }).catch(showErr(msg));
  });

  $("m-test").addEventListener("click", function () {
    runTest($("m-msg"), {
      id: $("m-id").value || undefined,
      label: $("m-label").value.trim(),
      base_url: $("m-base_url").value.trim(),
      model: $("m-model").value.trim(),
      api_key: $("m-api_key").value,
    });
  });

  function runTest(el, body) {
    el.textContent = "测试中…"; el.className = "muted";
    api("POST", "/api/admin/models/test", body || {})
      .then(function (r) {
        if (r.ok) {
          var extra = r.models && r.models.length ? "可用: " + r.models.slice(0, 6).join(", ") : "";
          var warn = r.error ? "（" + r.error + "）" : "";
          el.textContent = "✓ 连接成功 " + extra + warn;
          el.className = "";
        } else {
          el.textContent = "✗ " + (r.error || "连接失败");
          el.className = "err";
        }
      })
      .catch(showErr(el));
  }

  // ---- ask logs ----
  var logsPage = 0;
  var logsTotal = 0;
  var LOG_PAGE = 50;
  var logsOk = "";
  var logsDays = "";
  var logsLoading = false;
  var logsSearchTimer = null;
  var logsReqSeq = 0;

  function logsOffset() { return logsPage * LOG_PAGE; }

  function setLogsLoading(on) {
    logsLoading = !!on;
    $("logs-list").classList.toggle("loading", logsLoading);
    ["logs-search", "logs-refresh", "logs-clear"].forEach(function (id) {
      if ($(id)) $(id).disabled = logsLoading;
    });
    document.querySelectorAll(".log-seg-filter .seg").forEach(function (b) {
      b.disabled = logsLoading;
    });
    updateLogsPager();
  }

  function loadLogs(page) {
    if (typeof page === "number" && page >= 0) logsPage = page;
    else logsPage = 0;
    var q = $("logs-q").value.trim();
    var ip = $("logs-ip").value.trim();
    var url = "/api/admin/ask-logs?limit=" + LOG_PAGE + "&offset=" + logsOffset();
    if (q) url += "&q=" + encodeURIComponent(q);
    if (ip) url += "&ip=" + encodeURIComponent(ip);
    if (logsOk) url += "&ok=" + encodeURIComponent(logsOk);
    if (logsDays) url += "&days=" + encodeURIComponent(logsDays);
    var seq = ++logsReqSeq;
    setLogsLoading(true);
    api("GET", url).then(function (data) {
      if (seq !== logsReqSeq) return;
      logsTotal = data.total || 0;
      var items = data.items || [];
      var from = logsTotal ? logsOffset() + 1 : 0;
      var to = logsOffset() + items.length;
      var rangeLabel = logsDays === "7" ? "近 7 天" : (logsDays === "30" ? "近一个月" : "");
      var filtered = !!(q || ip || logsOk || logsDays);
      var suffix = filtered
        ? ("（已筛选" + (rangeLabel ? " · " + rangeLabel : "") + "）")
        : "";
      $("logs-stats").textContent = logsTotal
        ? ("第 " + from + "–" + to + " 条 / 共 " + logsTotal + " 条" + suffix)
        : ("共 0 条" + suffix);
      $("logs-empty").hidden = items.length > 0;
      $("logs-list").innerHTML = items.map(renderLogRow).join("");
      bindLogDetails();
    }).catch(handleAuth).finally(function () {
      if (seq === logsReqSeq) setLogsLoading(false);
    });
  }

  function updateLogsPager() {
    var pager = $("logs-pager");
    if (!pager) return;
    var pages = Math.max(1, Math.ceil(logsTotal / LOG_PAGE) || 1);
    pager.hidden = logsTotal <= LOG_PAGE;
    $("logs-page-label").textContent = "第 " + (logsPage + 1) + " / " + pages + " 页";
    $("logs-prev").disabled = logsLoading || logsPage <= 0;
    $("logs-next").disabled = logsLoading || logsTotal === 0 || (logsPage + 1) >= pages;
  }

  function renderLogRow(r) {
    var when = esc(formatTs(r.ts));
    var model = esc(r.label || r.model || "—");
    var status = r.ok ? "" : " bad";
    var tok = Number(r.total_tokens || 0);
    var tokLabel = tok
      ? (formatNum(tok) + " tok" + (r.tokens_estimated ? "≈" : ""))
      : "—";
    var preview = r.answer_preview || r.answer || "";
    return (
      '<details class="log-row' + status + '" data-ts="' + esc(r.ts || "") + '" data-ip="' + esc(r.ip || "") + '">' +
        "<summary>" +
          '<div class="log-top">' +
            '<span class="ip">' + esc(r.ip || "unknown") + "</span>" +
            "<span>" + when + "</span>" +
            "<span>" + model + "</span>" +
            "<span>" + esc(tokLabel) + "</span>" +
            (r.ok ? "" : '<span class="err">失败</span>') +
          "</div>" +
          '<p class="log-q">' + esc(r.question || "") + "</p>" +
          '<div class="log-a">' + esc(preview) + "</div>" +
        "</summary>" +
        '<div class="log-meta">' +
          '<div class="log-detail-body muted">展开加载详情…</div>' +
        "</div>" +
      "</details>"
    );
  }

  function renderLogDetail(r) {
    var tok = Number(r.total_tokens || 0);
    var tokDetail = tok
      ? ("Token: 输入 " + formatNum(r.prompt_tokens || 0) +
         " / 输出 " + formatNum(r.completion_tokens || 0) +
         " / 合计 " + formatNum(tok) +
         (r.tokens_estimated ? "（估算）" : ""))
      : "";
    var src = (r.sources && r.sources.length)
      ? "来源: " + r.sources.map(esc).join("、")
      : "";
    return (
      (tokDetail ? "<div>" + esc(tokDetail) + "</div>" : "") +
      (r.error ? '<div class="err">错误: ' + esc(r.error) + "</div>" : "") +
      (r.answer ? '<div class="log-a" style="max-height:none;color:#ddd">' + esc(r.answer) + "</div>" : "") +
      (src ? "<div>" + src + "</div>" : "") +
      (r.user_agent ? "<div>UA: " + esc(r.user_agent) + "</div>" : "") +
      (r.backend ? "<div>后端: " + esc(r.backend) + "</div>" : "")
    );
  }

  function bindLogDetails() {
    $("logs-list").querySelectorAll("details.log-row").forEach(function (el) {
      el.addEventListener("toggle", function () {
        if (!el.open || el.dataset.loaded === "1") return;
        var body = el.querySelector(".log-detail-body");
        if (!body) return;
        body.textContent = "加载中…";
        var ts = el.getAttribute("data-ts") || "";
        var ip = el.getAttribute("data-ip") || "";
        var url = "/api/admin/ask-logs/detail?ts=" + encodeURIComponent(ts) +
          "&ip=" + encodeURIComponent(ip);
        api("GET", url).then(function (rec) {
          el.dataset.loaded = "1";
          body.className = "log-detail-body";
          body.innerHTML = renderLogDetail(rec) || "<div class=\"muted\">无详情</div>";
          var preview = el.querySelector("summary .log-a");
          if (preview && rec.answer) preview.textContent = rec.answer;
        }).catch(function (exc) {
          body.textContent = (exc && exc.detail) || "详情加载失败";
        });
      });
    });
  }

  function formatTs(ts) {
    if (!ts) return "—";
    // Prefer local-looking ISO without forcing Date parse quirks.
    return String(ts).replace("T", " ").replace(/\+\d{2}:\d{2}$/, "").replace(/Z$/, " UTC");
  }

  function scheduleLogsSearch() {
    if (logsSearchTimer) clearTimeout(logsSearchTimer);
    logsSearchTimer = setTimeout(function () { loadLogs(0); }, 300);
  }

  $("logs-refresh").addEventListener("click", function () { loadLogs(logsPage); });
  $("logs-search").addEventListener("click", function () { loadLogs(0); });
  $("logs-prev").addEventListener("click", function () {
    if (logsPage > 0) loadLogs(logsPage - 1);
  });
  $("logs-next").addEventListener("click", function () {
    if ((logsPage + 1) * LOG_PAGE < logsTotal) loadLogs(logsPage + 1);
  });
  $("logs-q").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); loadLogs(0); }
  });
  $("logs-ip").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); loadLogs(0); }
  });
  $("logs-q").addEventListener("input", scheduleLogsSearch);
  $("logs-ip").addEventListener("input", scheduleLogsSearch);
  document.querySelectorAll(".log-ok-filter .seg").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".log-ok-filter .seg").forEach(function (x) {
        x.classList.remove("active");
      });
      btn.classList.add("active");
      logsOk = btn.getAttribute("data-ok") || "";
      loadLogs(0);
    });
  });
  document.querySelectorAll("[data-days]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll("[data-days]").forEach(function (x) {
        x.classList.remove("active");
      });
      btn.classList.add("active");
      logsDays = btn.getAttribute("data-days") || "";
      loadLogs(0);
    });
  });
  $("logs-clear").addEventListener("click", function () {
    if (!confirm("确定清空全部访问 / 提问记录？此操作不可恢复。")) return;
    api("DELETE", "/api/admin/ask-logs")
      .then(function () { loadLogs(0); loadOverview(); })
      .catch(function (exc) { alert((exc && exc.detail) || "清空失败"); });
  });

  // ---- import ----
  var dz = $("dropzone"), fileInput = $("imp-file");
  dz.addEventListener("click", function () { fileInput.click(); });
  ["dragenter", "dragover"].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add("drag"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove("drag"); });
  });
  dz.addEventListener("drop", function (e) {
    if (e.dataTransfer.files.length) { fileInput.files = e.dataTransfer.files; uploadImport(e.dataTransfer.files[0]); }
  });
  fileInput.addEventListener("change", function () { if (fileInput.files.length) uploadImport(fileInput.files[0]); });

  if ($("imp-hist-refresh")) {
    $("imp-hist-refresh").addEventListener("click", function () { loadUpgradeHistory(); loadBackupState(); });
  }

  if ($("imp-rollback")) {
    $("imp-rollback").addEventListener("click", function () {
      if (!confirm("确定回滚到备份版本？当前内容会被替换。")) return;
      var btn = $("imp-rollback");
      btn.disabled = true;
      api("POST", "/api/admin/rollback")
        .then(function (body) {
          var r = $("imp-result");
          if (r) {
            r.hidden = false;
            r.className = "result " + (body.ok ? "ok" : "err");
            r.innerHTML = "<h4>" + (body.ok ? "已回滚" : "回滚失败") + "</h4>" +
              "<p class='muted'>" + esc(body.message || "") + "</p>" +
              (body.from_version ? "<p>版本：<b>" + esc(body.from_version) + "</b> → <b>" + esc(body.to_version || "") + "</b></p>" : "");
          }
          loadOverview();
          loadBackupState();
          loadUpgradeHistory();
        })
        .catch(function (exc) {
          alert((exc && exc.detail) || "回滚失败");
        })
        .finally(function () { loadBackupState(); });
    });
  }

  function loadBackupState(state) {
    var apply = function (s) {
      var bak = (s && s.backup) || {};
      var meta = $("imp-backup-meta");
      var btn = $("imp-rollback");
      if (!meta || !btn) return;
      if (bak.available) {
        meta.textContent = "备份版本 " + (bak.version || "未知") + " 可用；升级失败会自动回滚到此版本。";
        btn.disabled = false;
      } else {
        meta.textContent = "当前没有旧版备份。";
        btn.disabled = true;
      }
    };
    if (state) { apply(state); return; }
    api("GET", "/api/admin/state").then(apply).catch(function () { /* ignore */ });
  }

  function loadUpgradeHistory() {
    var list = $("imp-history");
    var empty = $("imp-history-empty");
    if (!list) return;
    api("GET", "/api/admin/upgrades?limit=30").then(function (data) {
      var items = data.items || [];
      if (!items.length) {
        list.innerHTML = "";
        if (empty) empty.hidden = false;
        return;
      }
      if (empty) empty.hidden = true;
      list.innerHTML = items.map(function (it) {
        var ok = !!it.ok;
        var badge = it.rolled_back || it.action === "rolled_back" ? "已回滚"
                  : it.action === "skipped" ? "跳过"
                  : it.action === "upgraded" || it.action === "imported" ? "成功"
                  : "失败";
        var badClass = "";
        if (it.action === "skipped") badClass = "";
        else if (!(ok && !it.rolled_back && it.action !== "rolled_back")) badClass = " bad";
        return (
          '<div class="log-row' + badClass + '">' +
            '<div class="log-top">' +
              '<span class="ip">' + esc(badge) + "</span>" +
              "<span>" + esc(formatTs(it.ts)) + "</span>" +
              "<span>" + esc((it.from_version || "—") + " → " + (it.to_version || "—")) + "</span>" +
            "</div>" +
            (it.message ? '<p class="log-q" style="font-weight:500">' + esc(it.message) + "</p>" : "") +
            '<div class="log-meta">' +
              esc([it.source, it.package, it.operator].filter(Boolean).join(" · ")) +
            "</div>" +
          "</div>"
        );
      }).join("");
    }).catch(function () { /* ignore */ });
  }

  function uploadImport(file) {
    $("imp-result").hidden = true;
    var prog = $("imp-progress"), fill = $("imp-bar-fill"), pct = $("imp-pct");
    prog.hidden = false; fill.style.width = "0%"; pct.textContent = "上传中…";
    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/admin/import");
    xhr.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        var p = Math.round(e.loaded / e.total * 100);
        fill.style.width = p + "%"; pct.textContent = p + "%";
      }
    };
    xhr.onload = function () {
      var body; try { body = JSON.parse(xhr.responseText); } catch (err) { body = {}; }
      pct.textContent = "处理完成";
      var r = $("imp-result");
      r.hidden = false;
      var ok = xhr.status < 400 && body.ok !== false;
      r.className = "result " + (ok ? "ok" : (body.action === "skipped" ? "" : "err"));
      var title = body.action === "skipped" ? "已是最新版本"
                : body.action === "upgraded" ? "升级成功"
                : body.action === "imported" ? "导入成功"
                : body.action === "rolled_back" ? "升级失败，已回滚"
                : "导入失败";
      var lines = ["<h4>" + title + "</h4>"];
      if (body.message) lines.push("<p class='muted'>" + esc(body.message) + "</p>");
      if (body.from_version && body.to_version)
        lines.push("<p>版本：<b>" + esc(body.from_version) + "</b> → <b>" + esc(body.to_version) + "</b></p>");
      if (body.files_checked) lines.push("<p class='muted'>SHA-256 校验文件数: " + body.files_checked + "</p>");
      if (body.rolled_back) lines.push("<p class='muted'>已从备份自动恢复上一版本。</p>");
      if (body.errors && body.errors.length)
        lines.push("<ul class='err'>" + body.errors.map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") + "</ul>");
      r.innerHTML = lines.join("");
      loadOverview();
      loadBackupState();
      loadUpgradeHistory();
    };
    xhr.onerror = function () {
      $("imp-result").hidden = false;
      $("imp-result").className = "result err";
      $("imp-result").innerHTML = "<h4>网络错误</h4><p>上传失败，请重试。</p>";
    };
    var fd = new FormData(); fd.append("file", file);
    xhr.send(fd);
  }

  // ---- skills library ----
  function loadSkills() {
    api("GET", "/api/admin/skills").then(function (data) {
      var list = data.skills || [];
      var hint = $("skills-dir-hint");
      if (data.skills_dir) {
        hint.hidden = false;
        hint.innerHTML = "持久化目录：<code>" + esc(data.skills_dir) + "</code>（宿主机对应 <code>intranet/config/skills/</code>）";
      }
      $("skills-count").textContent = list.length ? ("共 " + list.length + " 个") : "";
      var el = $("skills-list");
      var empty = $("skills-empty");
      if (!list.length) {
        el.innerHTML = "";
        empty.hidden = false;
        return;
      }
      empty.hidden = true;
      el.innerHTML = list.map(function (s) {
        var dirname = s.dirname || s.name;
        return (
          "<div class='skill-row' data-name='" + esc(dirname) + "'>" +
            "<div class='skill-meta'>" +
              "<div class='name'>" + esc(s.name || dirname) +
                (s.has_extra_files ? "<span class='badge soft'>含附件</span>" : "") +
              "</div>" +
              "<div class='sub'>" + esc(s.description || "（无描述）") + "</div>" +
              "<div class='sub'>" +
                (s.author ? ("作者 " + esc(s.author) + " · ") : "") +
                "更新 " + esc(formatTs(s.updated_at)) +
              "</div>" +
            "</div>" +
            "<div class='skill-actions'>" +
              "<a class='btn btn-ghost btn-sm' href='/api/skills/" + encodeURIComponent(dirname) + "/download'>下载</a>" +
              "<button type='button' class='btn btn-ghost btn-sm skill-del'>删除</button>" +
            "</div>" +
          "</div>"
        );
      }).join("");
      el.querySelectorAll(".skill-del").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var row = btn.closest(".skill-row");
          var name = row && row.getAttribute("data-name");
          if (!name || !confirm("确认删除技能「" + name + "」？")) return;
          api("DELETE", "/api/admin/skills/" + encodeURIComponent(name))
            .then(loadSkills)
            .catch(function (exc) {
              alert((exc && exc.detail) || "删除失败");
            });
        });
      });
    }).catch(function (exc) {
      $("skills-list").innerHTML = "";
      $("skills-empty").hidden = false;
      $("skills-empty").textContent = (exc && exc.detail) || "加载失败";
    });
  }

  if ($("skills-refresh")) {
    $("skills-refresh").addEventListener("click", loadSkills);
  }

  function uploadSkillZip(file) {
    var msg = $("skills-upload-msg");
    if (!file) return;
    msg.textContent = "上传中…"; msg.className = "status-line muted";
    var fd = new FormData();
    fd.append("file", file);
    fd.append("overwrite", "true");
    fetch("/api/admin/skills/upload", { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw j;
          return j;
        });
      })
      .then(function (j) {
        msg.textContent = "✓ 已上传 " + ((j.skill && j.skill.name) || "");
        msg.className = "status-line";
        loadSkills();
      })
      .catch(function (exc) {
        var d = exc && exc.detail;
        msg.textContent = "✗ " + (d || "上传失败");
        msg.className = "status-line err";
      });
  }

  (function bindSkillsDropzone() {
    var dz = $("skills-dropzone");
    var input = $("skills-file");
    if (!dz || !input) return;
    dz.addEventListener("click", function () { input.click(); });
    input.addEventListener("change", function () {
      if (input.files && input.files[0]) uploadSkillZip(input.files[0]);
      input.value = "";
    });
    dz.addEventListener("dragover", function (e) { e.preventDefault(); dz.classList.add("drag"); });
    dz.addEventListener("dragleave", function () { dz.classList.remove("drag"); });
    dz.addEventListener("drop", function (e) {
      e.preventDefault(); dz.classList.remove("drag");
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) uploadSkillZip(f);
    });
  })();

  if ($("skills-form")) {
    $("skills-form").addEventListener("submit", function (e) {
      e.preventDefault();
      var msg = $("sk-msg");
      msg.textContent = "保存中…"; msg.className = "status-line muted";
      api("POST", "/api/admin/skills", {
        name: $("sk-name").value.trim(),
        description: $("sk-desc").value.trim(),
        author: $("sk-author").value.trim(),
        body: $("sk-body").value,
        overwrite: true,
      }).then(function (j) {
        msg.textContent = "✓ 已保存 " + ((j.skill && j.skill.name) || "");
        msg.className = "status-line";
        $("sk-body").value = "";
        loadSkills();
      }).catch(showErr(msg));
    });
  }

  function formatTs(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toLocaleString("zh-CN", { hour12: false });
    } catch (e) { return iso; }
  }

  // ---- account ----
  $("pw-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var msg = $("pw-msg"); msg.textContent = "保存中…"; msg.className = "muted";
    api("POST", "/api/admin/password", { username: $("pw-user").value || "admin", new_password: $("pw-pass").value })
      .then(function () { msg.textContent = "✓ 已更新，下次用新账密登录"; msg.className = ""; $("pw-pass").value = ""; })
      .catch(showErr(msg));
  });

  // ---- helpers ----
  function api(method, url, body) {
    return fetch(url, {
      method: method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      credentials: "same-origin",
    }).then(function (r) {
      if (r.status === 401 && url.indexOf("/login") === -1) { handleAuth(); throw { detail: "未登录" }; }
      return r.json().then(function (j) {
        if (!r.ok) throw j; return j;
      });
    });
  }
  function showApp() {
    $("login-view").hidden = true;
    $("login-view").style.display = "none";
    $("app-view").hidden = false;
    $("app-view").style.display = "grid";
  }
  function handleAuth() {
    $("app-view").hidden = true;
    $("app-view").style.display = "none";
    $("login-view").hidden = false;
    $("login-view").style.display = "";
  }
  function showErr(el) {
    return function (exc) {
      var d = exc && exc.detail;
      if (Array.isArray(d)) d = d.map(function (x) { return x.msg || JSON.stringify(x); }).join("; ");
      el.textContent = "✗ " + (d || "操作失败");
      el.className = (el.className || "").indexOf("status-line") >= 0 ? "status-line err" : "err";
    };
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function card(k, v) {
    return "<div class='card'><div class='k'>" + esc(k) + "</div><div class='v'>" + esc(v) + "</div></div>";
  }
  function kv(k, v) {
    return "<div>" + esc(k) + "</div><div><b>" + esc(v) + "</b></div>";
  }
  function formatNum(n) {
    var x = Number(n) || 0;
    try { return x.toLocaleString("zh-CN"); } catch (e) { return String(x); }
  }
})();
