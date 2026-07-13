(function () {
  const TERMINAL = new Set(["complete", "completed", "cancelled", "failed"]);

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function revealHashTarget() {
    document.querySelectorAll(".deep-link-target").forEach((item) => item.classList.remove("deep-link-target"));
    const id = decodeURIComponent((window.location.hash || "").slice(1));
    if (!id) return;
    const target = document.getElementById(id);
    if (!target) return;
    const page = target.closest(".page");
    if (page) page.style.removeProperty("display");
    target.querySelectorAll("details.provider-config-details").forEach((details) => {
      details.open = true;
    });
    let current = target.parentElement;
    while (current) {
      if (current.tagName === "DETAILS") current.open = true;
      current = current.parentElement;
    }
    target.classList.add("deep-link-target");
    window.requestAnimationFrame(() => target.scrollIntoView({ block: "start", inline: "nearest" }));
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
    const currentDay = job.current_day
      ? "Current day: " + escapeHtml(job.current_day)
      : job.last_successful_day
        ? "Last successful day: " + escapeHtml(job.last_successful_day)
        : "Waiting for the next chunk.";
    const retries = Number(job.failed_days_count || 0) > 0
      ? '<small>Failed or retried days: ' + escapeHtml(job.failed_days_count) + "</small>"
      : "";
    const eta = job.estimated_finish_time
      ? '<small>Estimated finish: ' + escapeHtml(job.estimated_finish_time) + "</small>"
      : "";
    const updated = job.updated_time ? '<small>Updated ' + escapeHtml(job.updated_time) + "</small>" : "";
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
      retries,
      updated,
      eta,
      "</div>",
      actionForms(job),
      "</div>",
    ].join("");
  }

  async function pollJobs(container) {
    const endpoint = container.dataset.endpoint;
    const intervalMs = Math.max(parseInt(container.dataset.pollIntervalMs || "7000", 10), 5000);
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
    revealHashTarget();
    window.addEventListener("hashchange", revealHashTarget);
    window.addEventListener("load", revealHashTarget);

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
