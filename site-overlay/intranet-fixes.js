/* Intranet localizations that must survive .zdoc imports.
 * Re-applied by overlay_apply after every content swap; also runs in the
 * browser so even an unpatched HTML snapshot gets the same UX.
 */
(function () {
  "use strict";
  if (window.__ZCODE_INTRANET_FIXES__) return;
  window.__ZCODE_INTRANET_FIXES__ = true;

  var WIN_SVG =
    '<svg aria-hidden="true" class="size-4 shrink-0" data-zc-win-icon="1" fill="currentColor" ' +
    'focusable="false" height="1em" viewBox="0 0 32 32" width="1em" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M4 4h10.5v10.5H4zm13.5 0H28v10.5H17.5zM4 17.5h10.5V28H4zm13.5 0H28V28H17.5z"></path></svg>';

  function textOf(el) {
    return (el.textContent || "").replace(/\s+/g, " ").trim();
  }

  function applyDownload(version) {
    var href = version ? "/releases/" + version + "/ZCode-" + version + "-win-x64.exe" : null;
    document.querySelectorAll("a").forEach(function (a) {
      var t = textOf(a);
      var aria = a.getAttribute("aria-label") || "";
      var cur = a.getAttribute("href") || "";
      var isCta =
        t.indexOf("立即下载") !== -1 ||
        aria.indexOf("Download ZCODE") !== -1 ||
        ((t === "下载" || t === "下载 Windows") &&
          (cur.indexOf("#unavailable") === 0 || cur.indexOf("/releases/") !== -1));
      if (isCta) {
        if (href) a.setAttribute("href", href);
        a.removeAttribute("aria-disabled");
        a.removeAttribute("tabindex");
        if ((a.getAttribute("title") || "").indexOf("仅提供 Windows") !== -1) {
          a.removeAttribute("title");
        }
        a.style.opacity = "";
        a.style.pointerEvents = "";
        a.style.filter = "";
        a.style.cursor = "";
        a.setAttribute("aria-label", "Download ZCODE 适用于 Windows (x64)");
        var svg = a.querySelector("svg");
        if (!svg || svg.getAttribute("data-zc-win-icon") !== "1") {
          var wrap = document.createElement("span");
          wrap.innerHTML = WIN_SVG;
          var icon = wrap.firstChild;
          if (svg && svg.getAttribute("class")) icon.setAttribute("class", svg.getAttribute("class"));
          if (svg) svg.replaceWith(icon);
          else a.insertBefore(icon, a.firstChild);
        }
        if (t === "下载") {
          var keep = a.querySelector("svg");
          a.textContent = "";
          if (keep) a.appendChild(keep);
          a.appendChild(document.createTextNode("下载 Windows"));
        }
        a.querySelectorAll("p").forEach(function (p) {
          var pt = textOf(p);
          if (pt.indexOf("适用于") === 0 || pt.indexOf("macOS") !== -1 || pt.indexOf("Apple") !== -1) {
            p.textContent = "适用于 Windows (x64)";
          }
        });
      }
      if (
        (t.indexOf("macOS") === 0 || t.indexOf("MacOS") === 0 || t.indexOf("Linux") === 0) &&
        t.indexOf("排查") === -1 &&
        ((a.getAttribute("class") || "").indexOf("group/opt") !== -1 || cur.indexOf("#unavailable") === 0)
      ) {
        a.style.display = "none";
        a.setAttribute("aria-hidden", "true");
        a.setAttribute("data-zc-hidden-platform", "1");
      }
    });
  }

  function applyBadge() {
    ["ZCode homepage", "ZCode 首页"].forEach(function (aria) {
      document.querySelectorAll('a[aria-label="' + aria + '"]').forEach(function (a) {
        if (a.querySelector("[data-zc-intranet-badge]")) return;
        var svgs = a.querySelectorAll(":scope > svg, :scope > span > svg");
        var wordmark = svgs.length > 1 ? svgs[svgs.length - 1] : svgs[0];
        if (!wordmark) return;
        var col = document.createElement("span");
        col.className = "flex flex-col items-start gap-1.5";
        wordmark.parentNode.insertBefore(col, wordmark);
        col.appendChild(wordmark);
        var badge = document.createElement("span");
        badge.setAttribute("data-zc-intranet-badge", "1");
        badge.className =
          aria === "ZCode 首页"
            ? "text-[10px] font-semibold leading-tight tracking-wide text-muted-foreground whitespace-nowrap"
            : "text-[11px] font-semibold leading-tight tracking-wide text-white/80 whitespace-nowrap";
        badge.textContent = "Jushri内网版";
        col.appendChild(badge);
        a.classList.remove("items-center");
        a.classList.add("items-start");
      });
    });
  }

  function applyDocsScroll() {
    document.querySelectorAll("header .h-12, header [class~='h-12']").forEach(function (bar) {
      bar.classList.remove("h-12");
      bar.classList.add("min-h-12", "h-auto", "py-1.5");
      bar.setAttribute("data-zc-docs-header", "1");
    });
    document.querySelectorAll("div.overflow-auto.flex-1.min-w-0").forEach(function (div) {
      div.classList.add("min-h-0");
      div.setAttribute("data-zc-docs-scroll", "1");
    });
  }

  function run(version) {
    applyDownload(version);
    applyBadge();
    applyDocsScroll();
  }

  function boot() {
    fetch("/_zc/version.json", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (j) { run(j.content_version || null); })
      .catch(function () { run(null); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
