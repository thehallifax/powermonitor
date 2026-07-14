(function () {
  document.documentElement.classList.add("js");
  const TERMINAL = new Set(["complete", "completed", "cancelled", "failed"]);
  const PAGE_IDS = new Set(["glance", "home", "bills", "solar", "ev", "whatif", "analytics", "system"]);
  const DEEP_LINK_PAGES = {
    recommendations: "whatif",
    "battery-investment": "whatif",
    "home-goals": "whatif",
    timeline: "analytics",
    setup: "system",
    imports: "system",
    "setup-billing": "system",
    "setup-billing-cycle": "system",
    "setup-tariff": "system",
    "system-collector": "system",
    "system-live-data": "system",
    "system-data-quality": "system",
    "energy-score-explanation": "home",
  };

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function pageForTarget(target) {
    if (!target) return document.getElementById("home");
    if (target.classList.contains("page")) return target;
    if (DEEP_LINK_PAGES[target.id]) return document.getElementById(DEEP_LINK_PAGES[target.id]);
    return target.closest(".page") || document.getElementById("home");
  }

  function updateActivePage(page) {
    document.querySelectorAll(".page").forEach((item) => {
      const active = item === page;
      item.classList.toggle("is-active", active);
      item.hidden = !active;
      item.setAttribute("aria-hidden", active ? "false" : "true");
    });
  }

  function updateActiveNav(page) {
    const activeId = page && page.id === "analytics" ? "analytics" : page && page.id === "whatif" ? "whatif" : page && page.id;
    document.querySelectorAll(".top-nav a, .mobile-shortcuts a").forEach((link) => {
      const linkTarget = (link.getAttribute("href") || "").replace(/^#/, "");
      const active = linkTarget === activeId || (activeId === "analytics" && linkTarget === "timeline");
      link.classList.toggle("active", active);
      if (active) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  function openDeepLinkContainers(target) {
    if (!target) return;
    target.querySelectorAll("details.provider-config-details").forEach((details) => {
      details.open = true;
    });
    let current = target.parentElement;
    while (current) {
      if (current.tagName === "DETAILS") current.open = true;
      current = current.parentElement;
    }
  }

  function navigateToHash(options) {
    const scrollBehavior = options && options.smooth ? "smooth" : "auto";
    document.querySelectorAll(".deep-link-target").forEach((item) => item.classList.remove("deep-link-target"));
    const id = decodeURIComponent((window.location.hash || "").slice(1));
    const target = document.getElementById(id) || document.getElementById("home");
    const page = pageForTarget(target);
    if (!page) return;
    updateActivePage(page);
    updateActiveNav(page);
    openDeepLinkContainers(target);
    if (target && !target.classList.contains("page")) {
      target.classList.add("deep-link-target");
    }
    window.requestAnimationFrame(() => {
      (target || page).scrollIntoView({ behavior: scrollBehavior, block: "start", inline: "nearest" });
    });
  }

  function handleSamePageAnchorClick(event) {
    const link = event.target.closest('a[href^="#"]');
    if (!link) return;
    const rawHash = link.getAttribute("href") || "";
    if (rawHash.length <= 1) return;
    const targetId = decodeURIComponent(rawHash.slice(1));
    if (!document.getElementById(targetId)) return;
    event.preventDefault();
    if (window.location.hash !== rawHash) {
      window.history.pushState(null, "", rawHash);
    }
    navigateToHash({ smooth: true });
  }

  function payloadFromForm(form) {
    const payload = {};
    const mappings = {};
    new FormData(form).forEach((value, key) => {
      const text = String(value || "").trim();
      if (!text) return;
      if (key.startsWith("entity_mappings.")) {
        mappings[key.slice("entity_mappings.".length)] = text;
      } else {
        payload[key] = text;
      }
    });
    if (Object.keys(mappings).length) payload.entity_mappings = mappings;
    return payload;
  }

  function renderResult(container, action) {
    if (!container || !action) return;
    const status = action.status || (action.success ? "success" : "failure");
    container.className = "provider-action-result visible " + status;
    const details = action.technical_details || (action.test && action.test.technical_details) || {};
    container.innerHTML = [
      "<span>" + escapeHtml(status) + "</span>",
      "<strong>" + escapeHtml(action.user_message || "Provider action completed.") + "</strong>",
      "<small>" + escapeHtml(action.next_step || "Review provider status.") + "</small>",
      "<details><summary>Technical details</summary><pre>" + escapeHtml(JSON.stringify(details, null, 2)) + "</pre></details>",
    ].join("");
  }

  async function runProviderAction(form, actionName) {
    const providerId = form.dataset.providerId;
    const result = form.querySelector(".provider-action-result");
    const payload = ["save", "configure", "edit"].includes(actionName) ? payloadFromForm(form) : {};
    if (actionName === "reset") payload.reset_entity_mappings = true;
    result.className = "provider-action-result visible warning";
    result.innerHTML = "<span>Running</span><strong>Working on provider action...</strong>";
    try {
      const response = await fetch("/api/providers/" + encodeURIComponent(providerId) + "/" + encodeURIComponent(actionName), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      const action = data.action || {
        success: false,
        status: "failure",
        user_message: data.error || "Provider action failed.",
        next_step: "Review the provider form and try again.",
        technical_details: data,
      };
      renderResult(result, action);
      if (actionName === "reconnect" && action.success) {
        window.setTimeout(() => window.location.reload(), 700);
      }
    } catch (error) {
      renderResult(result, {
        success: false,
        status: "failure",
        user_message: "Provider action failed before PowerMonitor could process it.",
        next_step: "Check that the local web server is running, then try again.",
        technical_details: { message: String(error) },
      });
    }
  }

  function statusTone(status) {
    if (status === "complete" || status === "completed") return "good";
    if (status === "failed") return "bad";
    return "neutral";
  }

  function actionForms(job) {
    const jobId = escapeHtml(job.job_id);
    return [
      '<div class="provider-action-row">',
      job.resumable
        ? '<form method="post" action="/api/historical-backfill/resume"><input type="hidden" name="job_id" value="' +
          jobId +
          '"><button type="submit">Resume</button></form>'
        : "",
      job.cancellable
        ? '<form method="post" action="/api/historical-backfill/cancel"><input type="hidden" name="job_id" value="' +
          jobId +
          '"><button type="submit">Cancel</button></form>'
        : "",
      '<a class="button-link" href="/api/historical-backfill/diagnostics?job_id=' + jobId + '">View diagnostics</a>',
      "</div>",
    ].join("");
  }

  function renderJob(job) {
    const percentage = Number(job.percentage_complete || 0);
    const failedCount = Number(job.failed_day_count || job.failed_days_count || 0);
    const summary = job.summary_message
      ? '<small class="historical-job-summary">' + escapeHtml(job.summary_message) + "</small>"
      : "";
    const currentDay = job.current_day
      ? "Current day: " + escapeHtml(job.current_day)
      : job.last_successful_day
        ? "Last successful day: " + escapeHtml(job.last_successful_day)
        : "Waiting for the next chunk.";
    const retries = failedCount > 0
      ? '<small>Failed or retried days: ' + escapeHtml(failedCount) + "</small>"
      : "";
    const eta = job.estimated_finish_time
      ? '<small>Estimated finish: ' + escapeHtml(job.estimated_finish_time) + "</small>"
      : "";
    const updated = job.updated_time ? '<small>Updated ' + escapeHtml(job.updated_time) + "</small>" : "";
    const counters = [
      '<small>' + escapeHtml(job.imported_or_updated || job.readings_added || 0) + " readings imported or updated</small>",
      '<small>' + escapeHtml(job.duplicates_skipped || 0) + " duplicates skipped</small>",
    ].join("");
    return [
      '<div class="historical-job-card completeness-row ' + statusTone(job.status) + '" data-job-id="' + escapeHtml(job.job_id) + '" data-job-status="' + escapeHtml(job.status) + '" data-poll-active="' + String(!TERMINAL.has(String(job.status || "").toLowerCase())) + '">',
      '<div class="historical-job-header">',
      '<span>' + escapeHtml(job.provider) + " - " + escapeHtml(job.status_text) + "</span>",
      '<strong>' + escapeHtml(job.requested_range.start) + " to " + escapeHtml(job.requested_range.end) + "</strong>",
      "</div>",
      '<div class="historical-job-progress">',
      '<div class="historical-job-bar" aria-hidden="true"><span style="width:' + percentage.toFixed(1) + '%"></span></div>',
      '<strong>' + escapeHtml(job.percentage_label) + "</strong>",
      "</div>",
      '<div class="historical-job-meta">',
      '<small>' + escapeHtml(job.days_progress_label) + "</small>",
      '<small>' + escapeHtml(currentDay) + "</small>",
      summary,
      counters,
      retries,
      updated,
      eta,
      "</div>",
      actionForms(job),
      "</div>",
    ].join("");
  }

  function renderCoverageSource(source) {
    return [
      '<div class="completeness-row good">',
      '<span>' + escapeHtml(String(source.resolution || "interval data").replace(/_/g, " ")) + "</span>",
      '<strong>' + escapeHtml(source.interval_grade_days || 0) + " days available</strong>",
      '<small>' + escapeHtml(source.earliest_interval_grade_reading || "No first reading") + " to " + escapeHtml(source.latest_interval_grade_reading || "No last reading") + "</small>",
      "</div>",
    ].join("");
  }

  function updateCoverage(coverage) {
    if (!coverage) return;
    const stored = document.getElementById("historical-stored-coverage");
    if (stored) {
      const grid = stored.querySelector(".historical-coverage-sources") || stored.querySelector(".completeness-grid");
      if (grid) {
        const sources = Array.isArray(coverage.sources) ? coverage.sources : [];
        grid.innerHTML = sources.length
          ? sources.map(renderCoverageSource).join("")
          : '<p class="notice">No SolarEdge historical interval readings are stored yet.</p>';
      }
    }
    const seasonal = document.getElementById("historical-seasonal-readiness");
    if (seasonal && coverage.previous_summer) {
      const heading = seasonal.querySelector(".chart-heading span");
      if (heading) {
        heading.textContent = [coverage.previous_summer.start || "", coverage.previous_summer.end || ""].filter(Boolean).join(" to ");
      }
      const values = seasonal.querySelectorAll(".config-grid strong");
      if (values.length >= 2) {
        values[0].textContent = String(coverage.previous_summer.days_available || 0) + " / " + String(coverage.previous_summer.days_in_range || 0);
        values[1].textContent = String(coverage.previous_summer.battery_ready_days || 0);
      }
    }
  }

  async function pollJobs(container) {
    const endpoint = container.dataset.endpoint;
    const intervalMs = Math.max(parseInt(container.dataset.pollIntervalMs || "5000", 10), 5000);
    if (!endpoint) return;

    async function refresh() {
      try {
        const response = await fetch(endpoint, {
          headers: { Accept: "application/json" },
          cache: "no-store",
          credentials: "same-origin",
        });
        if (!response.ok) return false;
        const payload = await response.json();
        const historical = payload.historical_backfill || {};
        const jobs = Array.isArray(historical.jobs) ? historical.jobs : [];
        const active = Boolean(historical.polling && historical.polling.active);
        container.dataset.pollActive = String(active);
        updateCoverage(historical.coverage);
        if (!jobs.length) {
          container.innerHTML = '<p class="notice">No historical data backfill jobs have run yet.</p>';
          return active;
        }
        container.innerHTML = jobs.map(renderJob).join("");
        return active;
      } catch (_error) {
        return container.dataset.pollActive === "true";
      }
    }

    let active = await refresh();
    while (active) {
      await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
      active = await refresh();
    }
  }

  window.addEventListener("DOMContentLoaded", function () {
    navigateToHash();
    window.addEventListener("hashchange", () => navigateToHash({ smooth: true }));
    window.addEventListener("popstate", () => navigateToHash({ smooth: true }));
    window.addEventListener("load", navigateToHash);
    document.addEventListener("click", handleSamePageAnchorClick);

    document.querySelectorAll(".provider-config-form").forEach((form) => {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const submitter = event.submitter;
        const actionName = submitter && submitter.dataset.providerAction ? submitter.dataset.providerAction : "save";
        const confirmation = submitter && submitter.dataset.confirm ? submitter.dataset.confirm : "";
        if (confirmation && !window.confirm(confirmation)) return;
        runProviderAction(form, actionName);
      });
    });

    document.querySelectorAll(".tariff-decision-actions form").forEach((form) => {
      form.addEventListener("submit", (event) => {
        const submitter = event.submitter;
        const confirmation = submitter && submitter.dataset.confirm ? submitter.dataset.confirm : "";
        if (confirmation && !window.confirm(confirmation)) event.preventDefault();
      });
    });

    const container = document.getElementById("historical-backfill-jobs");
    if (!container || container.dataset.pollActive !== "true") return;
    pollJobs(container);
  });
})();
