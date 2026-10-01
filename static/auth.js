/* Auth pages: login tabs, show/hide password, "type it again" checks,
   and the 3-step signup wizard. Pages work without this file. */
(function () {
  "use strict";

  /* ---------- Show / hide password ---------- */
  document.querySelectorAll('.login_box input[type="password"]').forEach(function (input) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "reveal-btn";
    btn.setAttribute("aria-label", "Show password");
    btn.innerHTML = '<i class="ri-eye-line" aria-hidden="true"></i>';
    btn.addEventListener("click", function () {
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
      btn.innerHTML = '<i class="' + (show ? "ri-eye-off-line" : "ri-eye-line") + '" aria-hidden="true"></i>';
      input.focus();
    });
    input.parentNode.appendChild(btn);
  });

  /* ---------- "Type it again" fields ---------- */
  function checkMatch(confirmEl) {
    var original = document.getElementById(confirmEl.dataset.match);
    if (!original) return true;
    var ok = !confirmEl.value || confirmEl.value === original.value;
    confirmEl.setCustomValidity(ok ? "" : "This doesn't match what you typed above.");
    return ok;
  }
  document.querySelectorAll("[data-match]").forEach(function (el) {
    var original = document.getElementById(el.dataset.match);
    el.addEventListener("input", function () { checkMatch(el); });
    if (original) original.addEventListener("input", function () { checkMatch(el); });
  });

  /* ---------- Login tabs ---------- */
  var tabs = document.querySelectorAll('.auth-tabs [role="tab"]');
  function selectTab(tab, focus) {
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      var panel = document.getElementById(t.getAttribute("aria-controls"));
      if (panel) panel.hidden = !on;
    });
    if (focus) tab.focus();
    var first = document.querySelector("#" + tab.getAttribute("aria-controls") + " input:not([type=hidden])");
    if (first && !focus) first.focus();
    try { history.replaceState(null, "", tab.id === "tab-team" ? "#team" : "#owner"); } catch (e) {}
  }
  if (tabs.length) {
    tabs.forEach(function (t, i) {
      t.tabIndex = t.getAttribute("aria-selected") === "true" ? 0 : -1;
      t.addEventListener("click", function () { selectTab(t, false); });
      t.addEventListener("keydown", function (e) {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          e.preventDefault();
          var n = (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
          selectTab(tabs[n], true);
        }
      });
    });
    if (location.hash === "#team") selectTab(document.getElementById("tab-team"), false);
  }

  /* ---------- Signup wizard ---------- */
  var form = document.getElementById("signup-wizard");
  if (!form) return;
  form.classList.add("js");

  var steps = Array.prototype.slice.call(form.querySelectorAll(".wizard-step"));
  var back = form.querySelector("[data-wizard-back]");
  var next = form.querySelector("[data-wizard-next]");
  var submit = form.querySelector("[data-wizard-submit]");
  var bar = document.querySelectorAll("#wizard-bar span");
  var count = document.getElementById("wizard-count");
  var current = 0;

  // If the server bounced us back with an error, open the step it's about.
  var flash = document.querySelector(".flash.error, .flash.danger");
  if (flash) {
    var msg = flash.textContent.toLowerCase();
    if (/recovery|passphrase/.test(msg)) current = 2;
    else if (/company|passcode|currency/.test(msg)) current = 1;
  }

  function show(i) {
    current = i;
    steps.forEach(function (s, n) { s.classList.toggle("is-current", n === i); });
    bar.forEach(function (b, n) {
      b.classList.toggle("is-done", n < i);
      b.classList.toggle("is-active", n === i);
    });
    if (count) count.textContent = "Step " + (i + 1) + " of " + steps.length + " · " + steps[i].dataset.title;
    back.hidden = i === 0;
    var last = i === steps.length - 1;
    next.hidden = last;
    submit.hidden = !last;
    var first = steps[i].querySelector("input:not([type=hidden]):not([tabindex='-1'])");
    if (first) first.focus();
  }

  function stepValid(i) {
    var fields = steps[i].querySelectorAll("input");
    for (var n = 0; n < fields.length; n++) {
      var f = fields[n];
      if (f.dataset.match) checkMatch(f);
      if (!f.checkValidity()) { f.reportValidity(); return false; }
    }
    return true;
  }

  next.addEventListener("click", function () { if (stepValid(current)) show(current + 1); });
  back.addEventListener("click", function () { show(current - 1); });

  // Enter key moves to the next step instead of submitting early.
  form.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && e.target.tagName === "INPUT" && current < steps.length - 1) {
      e.preventDefault();
      next.click();
    }
  });

  form.addEventListener("submit", function (e) {
    for (var i = 0; i < steps.length; i++) {
      if (!stepValid(i)) { e.preventDefault(); show(i); stepValid(i); return; }
    }
    submit.disabled = true;
    submit.textContent = "Creating your account…";
  });

  show(current);
})();
