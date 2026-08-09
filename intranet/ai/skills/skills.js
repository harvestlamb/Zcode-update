/* Public community Skills library — talks to /api/skills (same origin). */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var cache = [];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function formatTs(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toLocaleString("zh-CN", { hour12: false });
    } catch (e) {
      return iso;
    }
  }

  function filtered() {
    var q = ($("q").value || "").trim().toLowerCase();
    if (!q) return cache.slice();
    return cache.filter(function (s) {
      return (s.name || "").toLowerCase().indexOf(q) >= 0 ||
        (s.dirname || "").toLowerCase().indexOf(q) >= 0 ||
        (s.description_zh || "").toLowerCase().indexOf(q) >= 0 ||
        (s.description || "").toLowerCase().indexOf(q) >= 0 ||
        (s.author || "").toLowerCase().indexOf(q) >= 0;
    });
  }

  function render() {
    var items = filtered();
    $("count").textContent = cache.length
      ? ("显示 " + items.length + " / " + cache.length)
      : "";
    var list = $("list");
    var empty = $("empty");
    if (!items.length) {
      list.innerHTML = "";
      empty.hidden = false;
      empty.innerHTML = cache.length
        ? "没有匹配的技能。"
        : '暂无已发布技能。请管理员在 <a href="/admin/">管理后台 → 技能库</a> 上传，或把目录放到 <code>intranet/config/skills/</code>。';
      return;
    }
    empty.hidden = true;
    list.innerHTML = items.map(function (s, i) {
      var dirname = s.dirname || s.name;
      return (
        '<article class="card" data-name="' + esc(dirname) + '" style="animation-delay:' + (Math.min(i, 8) * 0.04) + 's">' +
          "<div>" +
            "<h3>" + esc(s.name || dirname) + "</h3>" +
            '<p class="desc">' + esc(s.description_zh || s.description || "（无描述）") + "</p>" +
          "</div>" +
          '<div class="meta">' +
            (s.version ? ("v" + esc(s.version) + " · ") : "") +
            (s.author ? ("作者 " + esc(s.author) + " · ") : "") +
            "更新 " + esc(formatTs(s.updated_at)) +
            (s.has_extra_files ? " · 含附件" : "") +
          "</div>" +
          '<div class="actions">' +
            '<a class="btn btn-primary btn-sm" href="/api/skills/' + encodeURIComponent(dirname) + '/download">下载 zip</a>' +
            '<button type="button" class="toggle preview-btn">预览 SKILL.md</button>' +
          "</div>" +
          '<p class="install-hint">解压到 <code>~/.zcode/skills/' + esc(dirname) + '/</code></p>' +
          '<div class="preview" hidden><pre class="preview-body">加载中…</pre></div>' +
        "</article>"
      );
    }).join("");

    list.querySelectorAll(".preview-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var card = btn.closest(".card");
        var box = card.querySelector(".preview");
        var body = card.querySelector(".preview-body");
        var name = card.getAttribute("data-name");
        if (!box.hidden) {
          box.hidden = true;
          btn.textContent = "预览 SKILL.md";
          return;
        }
        box.hidden = false;
        btn.textContent = "收起预览";
        if (body.dataset.loaded === "1") return;
        fetch("/api/skills/" + encodeURIComponent(name), { credentials: "same-origin" })
          .then(function (r) {
            return r.json().then(function (j) {
              if (!r.ok) throw j;
              return j;
            });
          })
          .then(function (j) {
            body.textContent = j.content || "(空)";
            body.dataset.loaded = "1";
          })
          .catch(function (exc) {
            body.textContent = (exc && exc.detail) || "加载失败";
          });
      });
    });
  }

  function load() {
    $("err").hidden = true;
    fetch("/api/skills", { credentials: "same-origin" })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw j;
          return j;
        });
      })
      .then(function (data) {
        cache = data.skills || [];
        render();
      })
      .catch(function (exc) {
        cache = [];
        render();
        $("err").hidden = false;
        $("err").textContent = (exc && exc.detail) || "无法加载技能列表，请确认 AI 服务已启动。";
      });
  }

  // Sticky header border on scroll
  var header = $("site-header");
  function onScroll() {
    if (!header) return;
    header.classList.toggle("is-scrolled", window.scrollY > 8);
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // Mobile nav
  var toggle = $("nav-toggle");
  var mobile = $("mobile-nav");
  if (toggle && mobile) {
    toggle.addEventListener("click", function () {
      var open = mobile.hidden;
      mobile.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.setAttribute("aria-label", open ? "关闭菜单" : "打开菜单");
    });
  }

  $("refresh").addEventListener("click", load);
  $("q").addEventListener("input", render);
  load();
})();
