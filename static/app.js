/* Small page helpers shared by the list pages. No dependencies.
   - <input data-search="LIST_ID">  filters items in #LIST_ID by text
   - <div data-chips="LIST_ID"><button data-chip="all|low|...">  filters by data-state
   - Items: [data-item] with data-text="..." and data-state="..."
   - [data-empty-for="LIST_ID"] shows when nothing matches
   - <tr data-href="/x"> makes the whole row open that link */
(function () {
  "use strict";
  var lists = {};
  function state(id) { return lists[id] || (lists[id] = { q: "", chip: "all" }); }

  function apply(id) {
    var list = document.getElementById(id);
    if (!list) return;
    var s = state(id), shown = 0;
    list.querySelectorAll("[data-item]").forEach(function (el) {
      var text = (el.getAttribute("data-text") || el.textContent).toLowerCase();
      var states = (el.getAttribute("data-state") || "").split(" ");
      var ok = (!s.q || text.indexOf(s.q) !== -1) && (s.chip === "all" || states.indexOf(s.chip) !== -1);
      el.hidden = !ok;
      if (ok) shown++;
    });
    var empty = document.querySelector('[data-empty-for="' + id + '"]');
    if (empty) empty.classList.toggle("show", shown === 0);
  }

  document.querySelectorAll("[data-search]").forEach(function (input) {
    var id = input.getAttribute("data-search");
    input.addEventListener("input", function () { state(id).q = input.value.trim().toLowerCase(); apply(id); });
  });

  document.querySelectorAll("[data-chips]").forEach(function (group) {
    var id = group.getAttribute("data-chips");
    group.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-chip]");
      if (!btn) return;
      group.querySelectorAll("[data-chip]").forEach(function (b) { b.setAttribute("aria-pressed", b === btn ? "true" : "false"); });
      state(id).chip = btn.getAttribute("data-chip");
      apply(id);
    });
  });

  document.addEventListener("click", function (e) {
    var row = e.target.closest("tr[data-href]");
    if (!row || e.target.closest("a, button, input, form")) return;
    window.location.href = row.getAttribute("data-href");
  });
})();
