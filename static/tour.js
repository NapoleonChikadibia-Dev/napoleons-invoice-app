/* =========================================================
   First-run onboarding tour + one-time page hints.
   No dependencies. Reads from <body>:
     data-user   – current user id (so each user gets their own tour)
     data-role   – "owner" | "worker"
     data-page   – Flask endpoint name, e.g. "home"
     data-first-run="true" – optional server flag to force the tour
   Targets are marked in the HTML with data-tour="…".
   Anything with [data-tour-start] replays the tour.
   ========================================================= */
(function () {
  "use strict";

  var body = document.body;
  var uid = body.dataset.user || "anon";
  var role = body.dataset.role === "owner" ? "owner" : "worker";
  var page = body.dataset.page || "";

  /* ---------- storage (safe if blocked) ---------- */
  function key(k) { return "ig:" + uid + ":" + k; }
  function getFlag(k) { try { return localStorage.getItem(key(k)); } catch (e) { return null; } }
  function setFlag(k, v) { try { localStorage.setItem(key(k), v || "1"); } catch (e) {} }

  /* ---------- step content ---------- */
  var OWNER_STEPS = [
    {
      center: true, emoji: "👋",
      title: "Welcome to Invoice Generator",
      text: "You're three steps away from your first invoice: add a product, add a customer, create the invoice. Want a 60-second look around?",
      next: "Show me around", skip: "I'll explore myself"
    },
    { target: "checklist", title: "Your setup checklist", text: "Work through this top to bottom. It tracks your progress and disappears once you're set up." },
    { target: "products", title: "Products", text: "Add what you sell, with price and stock. Invoices pull from this list, and stock goes down automatically when you bill." },
    { target: "customers", title: "Customers", text: "Save the people and businesses you bill. You can also add a new one on the fly while creating an invoice." },
    { target: "new-invoice", title: "New invoice", text: "Pick a customer, add products and quantities, then create. From the invoice page you can print or email it." },
    { target: "history", title: "Invoice history", text: "Every invoice lives here. Cancelling an invoice puts its stock back." },
    { target: "low-stock", title: "Low stock", text: "Products at or below their alert level show up here so you can restock before you run out." },
    { target: "team", title: "Your team", text: "Add teammates and set the company passcode they'll need to log in. Only you can see this." },
    { target: "reports", title: "Reports", text: "Monthly totals and how much each teammate has billed. Cancelled invoices don't count." },
    { target: "help", title: "Need this again?", text: "You can replay this tour from the bottom of the sidebar any time." },
    {
      center: true, emoji: "🚀",
      title: "You're ready",
      text: "Start with your first product. Everything else builds on it.",
      next: "Add my first product", nextHref: "products", skip: "Close"
    }
  ];

  var WORKER_STEPS = [
    {
      center: true, emoji: "👋",
      title: "Welcome to the team",
      text: "Your job here is simple: create invoices for customers. Here's where everything is.",
      next: "Show me around", skip: "Skip"
    },
    { target: "new-invoice", title: "New invoice", text: "Pick a customer, add products and quantities, then create. Stock updates automatically." },
    { target: "customers", title: "Customers", text: "Look up or add customers. You can also add one directly while creating an invoice." },
    { target: "products", title: "Products", text: "See prices and what's in stock, and restock items when deliveries arrive." },
    { target: "history", title: "Invoice history", text: "Find any invoice to view, print, email or cancel it." },
    { target: "low-stock", title: "Low stock", text: "Items running out. Let your owner know or restock them from Products." },
    { target: "help", title: "Need this again?", text: "You can replay this tour from the bottom of the sidebar any time." },
    {
      center: true, emoji: "🧾",
      title: "Go make your first invoice",
      text: "It takes about 30 seconds.",
      next: "Create an invoice", nextHref: "new-invoice", skip: "Close"
    }
  ];

  /* ---------- helpers ---------- */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function target(name) { return $('[data-tour="' + name + '"]'); }
  function visible(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  var navLinks = $("#nav-links");
  var navToggle = $("#nav-toggle");
  var navOpenedByTour = false;

  function navCollapsed() {
    return navToggle && getComputedStyle(navToggle).display !== "none";
  }
  function ensureNavOpen(el) {
    if (!navLinks || !el || !navLinks.contains(el) || !navCollapsed()) return;
    if (!navLinks.classList.contains("open")) {
      navLinks.classList.add("open");
      navToggle.setAttribute("aria-expanded", "true");
      navOpenedByTour = true;
    }
  }
  function restoreNav() {
    if (navOpenedByTour && navLinks) {
      navLinks.classList.remove("open");
      navToggle.setAttribute("aria-expanded", "false");
    }
    navOpenedByTour = false;
  }

  /* ---------- tour ---------- */
  var root, spot, pop, steps, idx, lastFocus;

  function build() {
    root = document.createElement("div");
    root.className = "tour-root";
    root.innerHTML =
      '<div class="tour-scrim" hidden></div>' +
      '<div class="tour-spot" hidden></div>' +
      '<div class="tour-pop" role="dialog" aria-modal="true" aria-labelledby="tour-title" tabindex="-1">' +
      '  <button type="button" class="tour-close" aria-label="Close tour">×</button>' +
      '  <div class="tour-emoji" aria-hidden="true"></div>' +
      '  <div class="tour-step"></div>' +
      '  <h3 id="tour-title"></h3>' +
      '  <p class="tour-text"></p>' +
      '  <div class="tour-actions">' +
      '    <div class="tour-dots" aria-hidden="true"></div><div class="spacer"></div>' +
      '    <button type="button" class="button-ghost button-sm tour-back">Back</button>' +
      '    <button type="button" class="button-sm tour-next">Next</button>' +
      "  </div>" +
      "</div>";
    document.body.appendChild(root);
    spot = $(".tour-spot", root);
    pop = $(".tour-pop", root);
    $(".tour-close", root).addEventListener("click", function () { end(true); });
    $(".tour-scrim", root).addEventListener("click", function () { end(true); });
    $(".tour-back", root).addEventListener("click", function () {
      if (steps[idx].center && idx === 0) return;
      if (steps[idx].skip) { end(true); return; }
      go(idx - 1, -1);
    });
    $(".tour-next", root).addEventListener("click", function () {
      var s = steps[idx];
      if (s.nextHref) {
        var link = target(s.nextHref);
        end(true);
        if (link && link.href) window.location.href = link.href;
        return;
      }
      if (idx === steps.length - 1) end(true); else go(idx + 1, 1);
    });
  }

  function onKey(e) {
    if (!root || !root.classList.contains("is-active")) return;
    if (e.key === "Escape") { e.preventDefault(); end(true); }
    else if (e.key === "ArrowRight") { e.preventDefault(); $(".tour-next", root).click(); }
    else if (e.key === "ArrowLeft" && idx > 0 && !steps[idx].skip) { e.preventDefault(); go(idx - 1, -1); }
    else if (e.key === "Tab") {
      var f = pop.querySelectorAll("button:not([hidden])");
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }

  function start() {
    var all = role === "owner" ? OWNER_STEPS : WORKER_STEPS;
    // Drop steps whose target isn't on this page (e.g. Team for workers).
    steps = all.filter(function (s) { return s.center || target(s.target); });
    if (!steps.length) return;
    if (!root) build();
    lastFocus = document.activeElement;
    root.classList.add("is-active");
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    go(0, 1);
  }

  function end(markSeen) {
    if (!root) return;
    root.classList.remove("is-active");
    spot.hidden = true;
    $(".tour-scrim", root).hidden = true;
    pop.style.display = "none";
    restoreNav();
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("resize", place);
    window.removeEventListener("scroll", place, true);
    if (markSeen) setFlag("tour-done");
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    var cl = target("checklist");
    if (cl && markSeen) { cl.classList.add("pulse"); }
  }

  function go(i, dir) {
    idx = Math.max(0, Math.min(i, steps.length - 1));
    var s = steps[idx];
    restoreNav();

    // Fill content
    $(".tour-emoji", root).textContent = s.emoji || "";
    $(".tour-emoji", root).hidden = !s.emoji;
    var numbered = steps.filter(function (x) { return !x.center; });
    var pos = numbered.indexOf(s);
    $(".tour-step", root).textContent = pos >= 0 ? "Step " + (pos + 1) + " of " + numbered.length : "";
    $(".tour-step", root).hidden = pos < 0;
    $("#tour-title", root).textContent = s.title;
    $(".tour-text", root).textContent = s.text;

    var back = $(".tour-back", root);
    var next = $(".tour-next", root);
    next.textContent = s.next || (idx === steps.length - 1 ? "Done" : "Next");
    if (s.skip) { back.textContent = s.skip; back.hidden = false; }
    else { back.textContent = "Back"; back.hidden = idx === 0; }

    var dots = $(".tour-dots", root);
    dots.innerHTML = "";
    if (!s.center) {
      numbered.forEach(function (_, n) {
        var d = document.createElement("span");
        if (n === pos) d.className = "on";
        dots.appendChild(d);
      });
    }

    pop.style.display = "";
    var el = s.center ? null : target(s.target);
    if (el) {
      ensureNavOpen(el);
      if (!visible(el)) { go(idx + (dir || 1), dir || 1); return; }
      var r = el.getBoundingClientRect();
      if (r.top < 70 || r.bottom > window.innerHeight - 20) {
        el.scrollIntoView({ block: "center", behavior: "instant" in document.documentElement.style ? "instant" : "auto" });
      }
    }
    place();
    // The mobile sidebar slides in; measure again once it has arrived.
    if (navOpenedByTour) window.setTimeout(place, 320);
    next.focus();
  }

  function place() {
    if (!root || !root.classList.contains("is-active")) return;
    var s = steps[idx];
    var scrim = $(".tour-scrim", root);
    var el = s.center ? null : target(s.target);

    if (!el || !visible(el)) {
      spot.hidden = true;
      scrim.hidden = false;
      pop.classList.add("is-center");
      pop.style.top = pop.style.left = "";
      return;
    }

    scrim.hidden = true; // the spotlight's own shadow is the scrim
    pop.classList.remove("is-center");
    var pad = 6;
    var r = el.getBoundingClientRect();
    spot.hidden = false;
    spot.style.top = r.top - pad + "px";
    spot.style.left = r.left - pad + "px";
    spot.style.width = r.width + pad * 2 + "px";
    spot.style.height = r.height + pad * 2 + "px";

    var pw = pop.offsetWidth, ph = pop.offsetHeight, gap = 14, vw = window.innerWidth, vh = window.innerHeight;
    var top = r.bottom + pad + gap;
    if (top + ph > vh - 12) top = r.top - pad - gap - ph;       // flip above
    if (top < 12) top = Math.max(12, Math.min(vh - ph - 12, r.top)); // fall back beside
    var left = r.left + r.width / 2 - pw / 2;
    left = Math.max(12, Math.min(vw - pw - 12, left));
    pop.style.top = top + "px";
    pop.style.left = left + "px";
  }

  /* ---------- one-time hints ---------- */
  function initHints() {
    var hints = document.querySelectorAll("[data-hint]");
    Array.prototype.forEach.call(hints, function (h) {
      var id = "hint-" + h.getAttribute("data-hint");
      if (getFlag(id)) return;
      h.hidden = false;
      var btn = h.querySelector("[data-hint-dismiss]");
      if (btn) btn.addEventListener("click", function () { setFlag(id); h.hidden = true; });
    });
  }

  /* ---------- boot ---------- */
  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest("[data-tour-start]");
    if (!t) return;
    e.preventDefault();
    if (page !== "home" && t.getAttribute("href")) {
      window.location.href = t.getAttribute("href");  // go to dashboard with ?tour=1
      return;
    }
    start();
  });

  initHints();

  // Auto-start only on an account's very first dashboard visit (the server
  // sets data-first-run) or when the user asks for it with ?tour=1.
  var wanted = /[?&]tour=1\b/.test(window.location.search) || body.dataset.firstRun === "true";
  if (page === "home" && wanted) {
    // Let fonts/layout settle so the spotlight lands in the right place.
    window.setTimeout(start, 350);
  }

  window.InvoiceTour = { start: start, end: end };
})();
