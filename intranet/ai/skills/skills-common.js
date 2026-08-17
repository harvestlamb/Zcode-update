/* Shared helpers for community Skills pages. */
(function (global) {
  "use strict";

  function $(id) {
    return document.getElementById(id);
  }

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

  function formatDate(iso) {
    if (!iso) return "—";
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toLocaleDateString("zh-CN");
    } catch (e) {
      return iso;
    }
  }

  /** Split query into keywords; all must match (AND). */
  function keywords(q) {
    return String(q || "")
      .trim()
      .toLowerCase()
      .split(/[\s,，、]+/)
      .filter(Boolean);
  }

  function haystack(s) {
    return [
      s.name,
      s.dirname,
      s.description_zh,
      s.description,
      s.author,
      s.version,
      s.has_extra_files ? "附件 extra" : ""
    ].join("\n").toLowerCase();
  }

  function matchSkill(s, keys) {
    if (!keys.length) return true;
    var text = haystack(s);
    for (var i = 0; i < keys.length; i++) {
      if (text.indexOf(keys[i]) < 0) return false;
    }
    return true;
  }

  function sortSkills(items, mode) {
    var list = items.slice();
    if (mode === "name") {
      list.sort(function (a, b) {
        return String(a.name || a.dirname || "").localeCompare(
          String(b.name || b.dirname || ""),
          "zh-CN",
          { sensitivity: "base" }
        );
      });
    } else if (mode === "author") {
      list.sort(function (a, b) {
        var aa = String(a.author || "\uffff");
        var bb = String(b.author || "\uffff");
        var c = aa.localeCompare(bb, "zh-CN", { sensitivity: "base" });
        if (c) return c;
        return String(a.name || "").localeCompare(String(b.name || ""), "zh-CN");
      });
    } else {
      // updated (default): newest first
      list.sort(function (a, b) {
        return String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
      });
    }
    return list;
  }

  function cardHtml(s, i, opts) {
    opts = opts || {};
    var dirname = s.dirname || s.name;
    var compact = !!opts.compact;
    return (
      '<article class="card' + (compact ? " card-compact" : "") +
        '" data-name="' + esc(dirname) + '" style="animation-delay:' + (Math.min(i, 8) * 0.04) + 's">' +
        "<div>" +
          "<h3>" + esc(s.name || dirname) + "</h3>" +
          '<p class="desc">' + esc(s.description_zh || s.description || "（无描述）") + "</p>" +
        "</div>" +
        '<div class="meta">' +
          (s.version ? ("v" + esc(s.version) + " · ") : "") +
          (s.author ? ("作者 " + esc(s.author) + " · ") : "") +
          "更新 " + esc(compact ? formatDate(s.updated_at) : formatTs(s.updated_at)) +
          (s.has_extra_files ? " · 含附件" : "") +
        "</div>" +
        '<div class="actions">' +
          '<a class="btn btn-primary btn-sm" href="/api/skills/' + encodeURIComponent(dirname) + '/download">下载 zip</a>' +
          '<button type="button" class="toggle preview-btn">预览 SKILL.md</button>' +
        "</div>" +
        (compact
          ? ""
          : '<p class="install-hint">解压到 <code>~/.zcode/skills/' + esc(dirname) + '/</code></p>') +
        '<div class="preview" hidden><pre class="preview-body">加载中…</pre></div>' +
      "</article>"
    );
  }

  function bindPreview(listEl) {
    if (!listEl) return;
    listEl.querySelectorAll(".preview-btn").forEach(function (btn) {
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

  function fetchSkills() {
    return fetch("/api/skills", { credentials: "same-origin" }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw j;
        return j.skills || [];
      });
    });
  }

  function bindChrome() {
    var header = $("site-header");
    function onScroll() {
      if (!header) return;
      header.classList.toggle("is-scrolled", window.scrollY > 8);
    }
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();

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
  }

  function detailText(exc, fallback) {
    if (!exc) return fallback;
    if (typeof exc.detail === "string") return exc.detail;
    if (Array.isArray(exc.detail) && exc.detail[0] && exc.detail[0].msg) {
      return exc.detail.map(function (d) { return d.msg; }).join("; ");
    }
    return fallback;
  }

  function bindSubmitModal() {
    var modal = $("submit-modal");
    if (!modal || modal.dataset.bound === "1") {
      // Still (re)bind open buttons that may have been injected dynamically.
      document.querySelectorAll(".js-open-submit").forEach(function (btn) {
        if (btn.dataset.submitBound === "1") return;
        btn.dataset.submitBound = "1";
        btn.addEventListener("click", function (e) {
          e.preventDefault();
          openSubmitModal();
        });
      });
      return;
    }
    modal.dataset.bound = "1";

    var form = $("submit-form");
    var dz = $("submit-dropzone");
    var input = $("submit-file");
    var msg = $("submit-msg");
    var btn = $("submit-btn");
    var selectedFile = null;

    function setSelectedFile(file) {
      selectedFile = file || null;
      var label = $("submit-file-label");
      if (!label) return;
      if (selectedFile) {
        label.textContent = "已选择：" + selectedFile.name;
        if (dz) dz.classList.add("has-file");
      } else {
        label.textContent = "点击或拖拽 .zip 到这里";
        if (dz) dz.classList.remove("has-file");
      }
    }

    function openSubmitModal() {
      modal.hidden = false;
      document.body.classList.add("modal-open");
      if (msg) {
        msg.textContent = "";
        msg.className = "status-line muted";
      }
      setTimeout(function () {
        if (dz) dz.focus();
      }, 0);
    }

    function closeSubmitModal() {
      modal.hidden = true;
      document.body.classList.remove("modal-open");
      if (form) form.reset();
      setSelectedFile(null);
      if (input) input.value = "";
      if (msg) {
        msg.textContent = "";
        msg.className = "status-line muted";
      }
    }

    global.openSubmitModal = openSubmitModal;
    global.closeSubmitModal = closeSubmitModal;

    document.querySelectorAll(".js-open-submit").forEach(function (el) {
      el.dataset.submitBound = "1";
      el.addEventListener("click", function (e) {
        e.preventDefault();
        openSubmitModal();
      });
    });
    document.querySelectorAll(".js-close-submit").forEach(function (el) {
      el.addEventListener("click", function (e) {
        e.preventDefault();
        closeSubmitModal();
      });
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !modal.hidden) closeSubmitModal();
    });

    if (dz && input) {
      dz.addEventListener("click", function () { input.click(); });
      dz.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          input.click();
        }
      });
      input.addEventListener("change", function () {
        setSelectedFile(input.files && input.files[0] ? input.files[0] : null);
      });
      dz.addEventListener("dragover", function (e) {
        e.preventDefault();
        dz.classList.add("drag");
      });
      dz.addEventListener("dragleave", function () { dz.classList.remove("drag"); });
      dz.addEventListener("drop", function (e) {
        e.preventDefault();
        dz.classList.remove("drag");
        var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
        if (f) {
          setSelectedFile(f);
          input.value = "";
        }
      });
    }

    if (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        if (!selectedFile) {
          msg.textContent = "请先选择 .zip 技能包";
          msg.className = "status-line err";
          return;
        }
        var name = (selectedFile.name || "").toLowerCase();
        if (!name.endsWith(".zip")) {
          msg.textContent = "请上传 .zip 文件";
          msg.className = "status-line err";
          return;
        }
        msg.textContent = "提交中…";
        msg.className = "status-line muted";
        btn.disabled = true;

        var fd = new FormData();
        fd.append("file", selectedFile);

        fetch("/api/skills/submit", { method: "POST", body: fd, credentials: "same-origin" })
          .then(function (r) {
            return r.json().then(function (j) {
              if (!r.ok) throw j;
              return j;
            });
          })
          .then(function (j) {
            var sub = j.submission || {};
            var skill = sub.skill || {};
            var bits = [j.message || "已提交审核"];
            if (skill.name || skill.dirname) bits.push(skill.name || skill.dirname);
            if (sub.id) bits.push("编号 " + sub.id);
            msg.textContent = "✓ " + bits.join(" · ");
            msg.className = "status-line ok";
            form.reset();
            setSelectedFile(null);
            setTimeout(closeSubmitModal, 1200);
          })
          .catch(function (exc) {
            msg.textContent = "✗ " + detailText(exc, "提交失败");
            msg.className = "status-line err";
          })
          .finally(function () { btn.disabled = false; });
      });
    }
  }

  global.SkillsUI = {
    $: $,
    esc: esc,
    formatTs: formatTs,
    formatDate: formatDate,
    keywords: keywords,
    matchSkill: matchSkill,
    sortSkills: sortSkills,
    cardHtml: cardHtml,
    bindPreview: bindPreview,
    fetchSkills: fetchSkills,
    bindChrome: bindChrome,
    bindSubmitModal: bindSubmitModal,
    detailText: detailText
  };
})(window);
