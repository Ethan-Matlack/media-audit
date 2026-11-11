// media-audit web UI: progressive enhancement only. Every page works without this file.
(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ------------------------------------------------------------ live filtering
  // Filter forms update the results table in place (and the URL) instead of reloading.
  let inflight = null;
  async function swapResults(url, push) {
    const results = $("#results");
    if (!results) { window.location.href = url; return; }
    if (inflight) inflight.abort();
    inflight = new AbortController();
    results.classList.add("loading");
    try {
      const res = await fetch(url, { signal: inflight.signal, headers: { "X-Partial": "1" } });
      const doc = new DOMParser().parseFromString(await res.text(), "text/html");
      const fresh = $("#results", doc);
      if (!fresh) { window.location.href = url; return; }
      results.replaceWith(fresh);
      bindResults();
      if (push) history.pushState({ partial: true }, "", url);
    } catch (err) {
      if (err.name !== "AbortError") window.location.href = url;
    } finally {
      const r = $("#results");
      if (r) r.classList.remove("loading");
    }
  }

  function formUrl(form) {
    const params = new URLSearchParams();
    for (const [k, v] of new FormData(form)) if (v !== "") params.append(k, v);
    const qs = params.toString();
    return (form.getAttribute("action") || window.location.pathname) + (qs ? "?" + qs : "");
  }

  for (const form of $$("form[data-autosubmit]")) {
    let timer = null;
    const go = () => swapResults(formUrl(form), true);
    form.addEventListener("change", (e) => {
      if (e.target.matches("input[type=search]")) return; // handled by input below
      go();
    });
    form.addEventListener("input", (e) => {
      if (!e.target.matches("input[type=search], input[type=number]")) return;
      clearTimeout(timer);
      timer = setTimeout(go, 350);
    });
    form.addEventListener("submit", (e) => { e.preventDefault(); go(); });
  }

  function bindResults() {
    const results = $("#results");
    if (!results) return;
    results.addEventListener("click", (e) => {
      const a = e.target.closest("thead a, .pager a");
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      swapResults(a.href, true).then(() => {
        const r = $("#results");
        if (r && a.closest(".pager")) r.scrollIntoView({ block: "start" });
      });
      // Keep the hidden sort/dir inputs in step so later filter changes keep the sort.
      const u = new URL(a.href);
      for (const form of $$("form[data-autosubmit]")) {
        for (const key of ["sort", "dir"]) {
          let input = form.querySelector(`input[type=hidden][name=${key}]`);
          const v = u.searchParams.get(key);
          if (v && !input) {
            input = document.createElement("input");
            input.type = "hidden"; input.name = key; form.appendChild(input);
          }
          if (input) input.value = v || "";
        }
      }
    });
  }
  bindResults();
  window.addEventListener("popstate", () => window.location.reload());

  // ------------------------------------------------------------ scan status
  const pill = $("#status-pill");
  const onScans = document.body.dataset.page === "scans";
  let wasRunning = pill ? pill.dataset.running === "true" : false;
  let pollTimer = null;

  const fmtNum = (n) => (n == null ? "" : n.toLocaleString());
  function fmtElapsed(s) {
    if (s == null) return "";
    s = Math.round(s);
    if (s < 90) return `${s} s`;
    const m = Math.floor(s / 60);
    return m < 90 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
  }

  function render(status) {
    const s = status.scan;
    if (pill) {
      pill.dataset.running = String(s.running);
      let text;
      if (s.running) {
        text = s.total ? `Scanning ${fmtNum(s.done)}/${fmtNum(s.total)}` : "Scanning…";
      } else if (status.schedule.next_run) {
        text = `Next scan ${status.schedule.next_run.replace("T", " ")}`;
      } else {
        text = "No scan scheduled";
      }
      pill.innerHTML = (s.running ? '<span class="dot" aria-hidden="true"></span>' : "") +
        '<span class="pill-text"></span>';
      $(".pill-text", pill).textContent = text;
    }
    if (onScans) {
      const box = $("#scan-progress");
      $("[data-when=running]", box).hidden = !s.running;
      $("[data-when=idle]", box).hidden = s.running;
      if (s.running) {
        $("[data-field=trigger]", box).textContent = s.trigger || "";
        $("[data-field=phase-text]", box).textContent = s.message || "starting";
        $("[data-field=bar]", box).style.width = s.total ? `${(100 * s.done) / s.total}%` : "0%";
        $("[data-field=counts]", box).textContent =
          s.total ? `${fmtNum(s.done)} of ${fmtNum(s.total)} files analyzed` : (s.phase || "");
        $("[data-field=elapsed]", box).textContent = fmtElapsed(s.elapsed_s);
        $("[data-field=stopping]", box).hidden = !s.stopping;
      }
      const now = $("#scan-now"), stop = $("#scan-stop");
      if (now) now.disabled = s.running;
      if (stop) stop.hidden = !s.running || s.stopping;
    }
    // A scan just finished: refresh the scans page so the history shows it.
    if (wasRunning && !s.running && onScans) window.location.reload();
    wasRunning = s.running;
  }

  async function poll() {
    clearTimeout(pollTimer);
    let running = wasRunning;
    try {
      const res = await fetch("/api/status", { cache: "no-store" });
      if (res.ok) {
        const status = await res.json();
        render(status);
        running = status.scan.running;
      }
    } catch (_) { /* server restarting; try again later */ }
    const delay = document.hidden ? 30000 : (running ? 2000 : 15000);
    pollTimer = setTimeout(poll, delay);
  }
  if (pill) {
    pollTimer = setTimeout(poll, wasRunning ? 2000 : 15000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  }

  for (const form of $$("form[data-scan-form]")) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = form.querySelector("button");
      if (btn) btn.disabled = true;
      try {
        await fetch(form.action, { method: "POST", headers: { Accept: "application/json" } });
      } finally {
        wasRunning = false; // the next status decides
        poll();
      }
    });
  }

  // ------------------------------------------------------------ ignore / unignore
  // Delegated, so it keeps working after the results area is swapped by a filter change.
  document.addEventListener("submit", async (e) => {
    const form = e.target.closest("form[data-ignore-form]");
    if (!form) return;
    e.preventDefault();
    const btn = form.querySelector("button");
    if (btn) btn.disabled = true;
    const ignoring = form.action.includes("/issues/ignore?");
    try {
      const res = await fetch(form.action, { method: "POST", headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(String(res.status));
      const row = form.closest("[data-issue-row]");
      const showingIgnored = new URLSearchParams(window.location.search).get("ignored") === "1";
      if (row && ignoring && document.body.dataset.page === "issues" && !showingIgnored) {
        row.hidden = true;
      } else if (row) {
        row.classList.toggle("ignored", ignoring);
        form.action = ignoring
          ? form.action.replace("/issues/ignore?", "/issues/unignore?")
          : form.action.replace("/issues/unignore?", "/issues/ignore?");
        if (btn) btn.textContent = ignoring ? "Unignore" : "Ignore";
      }
    } catch (_) {
      form.submit(); // fall back to a normal post + redirect
    } finally {
      if (btn) btn.disabled = false;
    }
  });

  // ------------------------------------------------------------ line chart hover
  for (const fig of $$("[data-linechart]")) {
    const svg = $("svg", fig);
    const tip = $(".tooltip", fig);
    const cross = $(".crosshair", svg);
    const points = $$(".series-point", svg);
    if (!points.length) continue;
    const show = (pt) => {
      points.forEach((p) => p.classList.toggle("active", p === pt));
      const x = +pt.getAttribute("cx"), y = +pt.getAttribute("cy");
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.hidden = false;
      const box = svg.getBoundingClientRect();
      const scale = box.width / svg.viewBox.baseVal.width;
      tip.textContent = pt.dataset.tip;
      tip.style.left = `${x * scale}px`;
      tip.style.top = `${y * scale}px`;
      tip.hidden = false;
    };
    const hide = () => {
      tip.hidden = true; cross.hidden = true;
      points.forEach((p) => p.classList.remove("active"));
    };
    svg.addEventListener("pointermove", (e) => {
      const box = svg.getBoundingClientRect();
      const x = ((e.clientX - box.left) / box.width) * svg.viewBox.baseVal.width;
      let best = points[0], dist = Infinity;
      for (const p of points) {
        const d = Math.abs(+p.getAttribute("cx") - x);
        if (d < dist) { dist = d; best = p; }
      }
      show(best);
    });
    svg.addEventListener("pointerleave", hide);
  }
})();
