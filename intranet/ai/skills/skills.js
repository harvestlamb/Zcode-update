/* Community Skills homepage — limited showcase + jump to catalog. */
(function () {
  "use strict";
  var UI = window.SkillsUI;
  var $ = UI.$;
  var HOME_LIMIT = 6;
  var cache = [];

  function render() {
    var sorted = UI.sortSkills(cache, "updated");
    var items = sorted.slice(0, HOME_LIMIT);
    var more = Math.max(0, cache.length - items.length);

    $("count").textContent = cache.length
      ? ("展示 " + items.length + " / 共 " + cache.length)
      : "";

    var moreEl = $("home-more");
    if (more > 0) {
      moreEl.hidden = false;
      moreEl.innerHTML =
        "还有 <strong>" + more + "</strong> 个技能未在首页展示。" +
        ' <a href="/skills/catalog.html">前往技能索引检索全部 →</a>';
    } else {
      moreEl.hidden = true;
      moreEl.innerHTML = "";
    }

    var list = $("list");
    var empty = $("empty");
    if (!items.length) {
      list.innerHTML = "";
      empty.hidden = false;
      empty.innerHTML =
        '暂无已发布技能。点击 <button type="button" class="linkish js-open-submit">投稿技能</button>，或请管理员审核上架。';
      UI.bindSubmitModal();
      return;
    }
    empty.hidden = true;
    list.innerHTML = items.map(function (s, i) {
      return UI.cardHtml(s, i, { compact: true });
    }).join("");
    UI.bindPreview(list);
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
  $("refresh").addEventListener("click", load);
  load();
})();
