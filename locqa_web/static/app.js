// locqa web — progressive enhancement only. Every page works without this
// file; it adds polling, a confirm on destructive buttons, and hides the
// Ren'Py-only translation field when it does not apply.
(function () {
  "use strict";

  // Run page: poll the status endpoint until the worker finishes, then reload
  // so the server renders the result. Backs off so an abandoned tab costs
  // almost nothing.
  var pending = document.querySelector("[data-poll]");
  if (pending) {
    var url = pending.getAttribute("data-poll");
    var last = pending.getAttribute("data-status");
    var delay = 1000;
    var label = pending.querySelector("[data-status-text]");
    var tick = function () {
      fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (run) {
          if (!run) { return schedule(); }
          if (run.status === "done" || run.status === "failed") {
            window.location.reload();
            return;
          }
          if (run.status !== last && label) {
            label.textContent = run.status === "running" ? "Running checks…" : "Waiting for a worker…";
            last = run.status;
          }
          schedule();
        })
        .catch(schedule);
    };
    var schedule = function () {
      delay = Math.min(delay * 1.3, 8000);
      window.setTimeout(tick, delay);
    };
    window.setTimeout(tick, delay);
  }

  // Runs list: refresh while anything is still queued or running.
  var list = document.querySelector("[data-refresh]");
  if (list) {
    window.setTimeout(function () { window.location.reload(); },
      parseInt(list.getAttribute("data-refresh"), 10) || 3000);
  }

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) { event.preventDefault(); }
    });
  });

  var script = document.querySelector("[data-script-input]");
  var translation = document.querySelector("[data-translation-field]");
  if (script && translation) {
    var sync = function () {
      var name = (script.files && script.files[0] && script.files[0].name) || "";
      var isRenpy = /\.rpy$/i.test(name);
      translation.hidden = Boolean(name) && !isRenpy;
      if (translation.hidden) {
        var input = translation.querySelector("input");
        if (input) { input.value = ""; }
      }
    };
    script.addEventListener("change", sync);
    sync();
  }
})();
