/* Professional Skills catalog — keyword search, sort, URL sync. */
(function () {
  "use strict";
  var UI = window.SkillsUI;
  var $ = UI.$;
  var cache = [];
  var debounceTimer = null;

  function readParams() {
    var sp = new URLSearchParams(window.location.search);
    return {
      q: sp.get("q") || "",
      sort: sp.get("sort") || "updated",
      extra: sp.get("extra") === "1"
    };
  }

  function writeParams(state, replace) {
    var sp = new URLSearchParams();
    if (state.q) sp.set("q", state.q);
    if (state.sort && state.sort !== "updated") sp.set("sort", state.sort);
    if (state.extra) sp.set("extra", "1");
    var qs = sp.toString();
    var url = window.location.pathname + (qs ? "?" + qs : "") + window.location.hash;
    if (replace) history.replaceState(null, "", url);
    else history.pushState(null, "", url);
  }

  function currentState() {
    return {
      q: ($("q").value || "").trim(),
      sort: $("sort").value || "updated",
      extra: !!$("extra-only").checked
    };
  }

  function applyState(state) {
    $("q").value = state.q || "";
    $("sort").value = state.sort || "updated";
    $("extra-only").checked = !!state.extra;
  }

  function filtered() {
    var state = currentState();
    var keys = UI.keywords(state.q);
    var items = cache.filter(function (s) {
      if (state.extra && !s.has_extra_files) return false;
      return UI.matchSkill(s, keys);
    });
    return UI.sortSkills(items, state.sort);
  }

  function initialLetter(s) {
    var name = String(s.name || s.dirname || "").trim();
    if (!name) return "#";
    var ch = name.charAt(0).toUpperCase();
    if (/[A-Z]/.test(ch)) return ch;
    if (/[0-9]/.test(ch)) return "0-9";
    return "#";
  }

  function renderLetterBar(items) {
    var bar = $("letter-bar");
    if (!items.length) {
      bar.hidden = true;
      bar.innerHTML = "";
      return;
    }
    var seen = {};
    items.forEach(function (s) {
      seen[initialLetter(s)] = true;
    });
    var order = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("");
    order.push("0-9", "#");
    var html = order.filter(function (L) { return seen[L]; }).map(function (L) {
      return '<button type="button" class="letter-chip" data-letter="' + L + '">' + L + "</button>";
    }).join("");
    bar.innerHTML = html || "";
    bar.hidden = !html;
    bar.querySelectorAll(".letter-chip").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var letter = btn.getAttribute("data-letter");
        var target = document.querySelector('[data-letter-group="' + letter + '"]');
        if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  }

  function render() {
    var state = currentState();
    var items = filtered();
    $("count").textContent = cache.length
      ? ("匹配 " + items.length + " / 共 " + cache.length +
          (state.q ? " · 关键字「" + state.q + "」" : ""))
      : "";

    var useLetters = state.sort === "name";
    if (useLetters) renderLetterBar(items);
    else {
      $("letter-bar").hidden = true;
      $("letter-bar").innerHTML = "";
    }

    var list = $("list");
    var empty = $("empty");
    if (!items.length) {
      list.innerHTML = "";
      empty.hidden = false;
      empty.innerHTML = cache.length
        ? '没有匹配的技能。试试更短的关键字，或 <button type="button" class="linkish" id="empty-clear">清空条件</button>。'
        : '暂无已发布技能。可先到 <a href="/skills/#submit">社区首页投稿</a>。';
      var clearBtn = $("empty-clear");
      if (clearBtn) {
        clearBtn.addEventListener("click", function () {
          $("q").value = "";
          $("extra-only").checked = false;
          $("sort").value = "updated";
          syncAndRender(true);
          $("q").focus();
        });
      }
      return;
    }
    empty.hidden = true;

    if (!useLetters) {
      list.innerHTML =
        '<div class="skill-grid">' +
        items.map(function (s, i) {
          return UI.cardHtml(s, i, { compact: false });
        }).join("") +
        "</div>";
      UI.bindPreview(list);
      return;
    }

    var groups = [];
    var map = {};
    items.forEach(function (s) {
      var L = initialLetter(s);
      if (!map[L]) {
        map[L] = [];
        groups.push(L);
      }
      map[L].push(s);
    });

    var cardIndex = 0;
    list.innerHTML = groups.map(function (L) {
      var cards = map[L].map(function (s) {
        return UI.cardHtml(s, cardIndex++, { compact: false });
      }).join("");
      return (
        '<section class="letter-group" data-letter-group="' + L + '" id="letter-' + encodeURIComponent(L) + '">' +
          '<h2 class="letter-heading">' + L + "</h2>" +
          '<div class="skill-grid">' + cards + "</div>" +
        "</section>"
      );
    }).join("");
    UI.bindPreview(list);
  }

  function syncAndRender(replace) {
    writeParams(currentState(), !!replace);
    render();
  }

  function scheduleRender() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () {
      syncAndRender(true);
    }, 180);
  }

  function load() {
    $("err").hidden = true;
    UI.fetchSkills()
      .then(function (skills) {
        cache = skills;
        render();
      })
      .catch(function (exc) {
        cache = [];
        render();
        $("err").hidden = false;
        $("err").textContent = (exc && exc.detail) || "无法加载技能列表，请确认 AI 服务已启动。";
      });
  }

  UI.bindChrome();
  UI.bindSubmitModal();
  applyState(readParams());

  $("q").addEventListener("input", scheduleRender);
  $("sort").addEventListener("change", function () { syncAndRender(true); });
  $("extra-only").addEventListener("change", function () { syncAndRender(true); });
  $("refresh").addEventListener("click", load);
  $("clear").addEventListener("click", function () {
    $("q").value = "";
    $("extra-only").checked = false;
    $("sort").value = "updated";
    syncAndRender(true);
    $("q").focus();
  });
  $("catalog-form").addEventListener("submit", function (e) {
    e.preventDefault();
    syncAndRender(true);
  });

  window.addEventListener("popstate", function () {
    applyState(readParams());
    render();
  });

  load();
})();
