// The version menu: latest, each release and each docs-* preview, read from
// <site>/versions.json (written by docs/publish_site.py).  Choosing one opens
// the same page there, or that copy's front page if the page is not in it.
//
// The <script> tag carries data-site (the site root, e.g. /cubevis/) and
// data-current (this copy's directory under it: "", "v1.2/", "dev/docs-x/").
// A local build has no versions.json; the menu is then not shown.
(function () {
  "use strict";
  var me = document.currentScript;
  if (!me) return;
  var site = me.dataset.site || "/";
  var current = me.dataset.current || "";

  function here() {
    var path = decodeURIComponent(location.pathname);
    var base = site + current;
    return path.indexOf(base) === 0 ? path.slice(base.length) : "";
  }

  function go(entry) {
    var page = here();
    var root = site + entry.path;
    if (!page) { location.href = root; return; }
    fetch(root + page, { method: "HEAD" })
      .then(function (r) { location.href = r.ok ? root + page + location.hash : root; })
      .catch(function () { location.href = root; });
  }

  function build(entries) {
    var brand = document.querySelector(".sidebar-brand");
    if (!brand || !entries.length) return;
    var box = document.createElement("div");
    box.className = "cubevis-versions";
    var label = document.createElement("label");
    label.textContent = "Version";
    var select = document.createElement("select");
    select.setAttribute("aria-label", "Documentation version");
    entries.forEach(function (e, i) {
      var opt = document.createElement("option");
      opt.value = i;
      opt.textContent = e.title || e.name;
      if (e.path === current) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener("change", function () { go(entries[select.value]); });
    label.appendChild(select);
    box.appendChild(label);
    brand.insertAdjacentElement("afterend", box);
  }

  function start() {
    fetch(site + "versions.json", { cache: "no-cache" })
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(build)
      .catch(function () { /* local build: no menu */ });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
