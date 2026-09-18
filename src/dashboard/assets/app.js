"use strict";

const $ = (selector) => document.querySelector(selector);
let source = null;
let page = 1;
const pageSize = 20;
let currentTaskId = null;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[character]));
}

function safeLink(value, label) {
  const url = String(value ?? "");
  if (!/^https?:\/\//i.test(url)) return esc(label || url);
  return `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label || url)}</a>`;
}

function readableState(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function readableDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "Unknown";
  let remaining = Math.max(0, Math.round(Number(seconds)));
  const hours = Math.floor(remaining / 3600); remaining %= 3600;
  const minutes = Math.floor(remaining / 60); const secs = remaining % 60;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function localTime(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown" : date.toLocaleString();
}

async function api(url, options) {
  const response = await fetch(url, options);
  if (response.status === 401) showLogin("Your dashboard session has expired.");
  if (!response.ok) {
    let detail = "Request failed";
    try { detail = (await response.json()).detail || detail; } catch (_) { /* text is optional */ }
    throw new Error(detail);
  }
  return response.json();
}

function showLogin(message) {
  if (source) { source.close(); source = null; }
  $("#login").hidden = false;
  $("#app").hidden = true;
  if (message) $("#login-error").textContent = message;
}

function card(label, value, className = "", detail = "") {
  return `<div class="card ${className}"><span>${esc(label)}</span><strong>${esc(value)}</strong>${detail ? `<small class="card-detail">${esc(detail)}</small>` : ""}</div>`;
}

function renderCards(summary) {
  const incidents = summary.incidents ?? 0;
  const resolved = summary.resolved ?? summary.completed ?? 0;
  const successes = summary.successes ?? resolved;
  const failed = summary.failed ?? 0;
  const denominator = successes + failed;
  const rate = denominator ? `${Math.round((Number(summary.success_rate ?? successes / denominator)) * 100)}% (${successes}/${denominator})` : "No completed attempts (0/0)";
  const samples = Number.isFinite(Number(summary.resolution_samples)) ? Number(summary.resolution_samples) : null;
  const missing = samples === null ? " · timing samples unknown" : ` · ${samples} timing samples · ${Math.max(0, resolved - samples)} unknown`;
  $("#cards").innerHTML = [
    card("Incidents", incidents, "primary"), card("Resolved", resolved, "primary"),
    card("Success rate", rate), card("Failed", failed), card("Cancelled", summary.cancelled ?? 0),
    card("Blocked", summary.blocked ?? 0), card("Resolution mean", readableDuration(summary.resolution_seconds_mean), "", missing.replace(/^ · /, "")),
    card("Resolution median", readableDuration(summary.resolution_seconds_median), "", samples === null ? "timing samples unknown" : `${samples} timing samples`),
    card("PRs opened", summary.prs_opened ?? 0)
  ].join("");
  $("#secondary").innerHTML = [
    `<span>Active: <strong>${esc(summary.active ?? 0)}</strong></span>`,
    `<span>Waiting: <strong>${esc(summary.waiting ?? 0)}</strong></span>`,
    `<span>Waiting for deployment: <strong>${esc(summary.waiting_for_deployment ?? 0)}</strong></span>`,
    `<span>Waiting for review: <strong>${esc(summary.waiting_for_review ?? 0)}</strong></span>`
  ].join("");
}

function renderMetrics(metrics) {
  if (!Array.isArray(metrics) || !metrics.length) {
    $("#metrics").innerHTML = '<span class="muted">No persisted telemetry available.</span>';
    return;
  }
  $("#metrics").innerHTML = metrics.map((metric) =>
    `<div class="metric"><span>${esc(metric.name || metric.operation || "Operation")}</span><strong>${esc(metric.calls ?? 0)}</strong><small>${esc(metric.failures ?? 0)} failures · ${readableDuration(metric.seconds)} total</small></div>`
  ).join("");
}

function filterQuery() {
  const values = new FormData($("#filters"));
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  for (const [key, value] of values.entries()) if (value) query.set(key, value);
  return query;
}

function renderTasks(snapshot) {
  const tasks = Array.isArray(snapshot.tasks) ? snapshot.tasks : [];
  $("#empty").hidden = tasks.length > 0;
  $("#results").textContent = snapshot.total === undefined ? "" : `${snapshot.total} task${snapshot.total === 1 ? "" : "s"}`;
  $("#page-label").textContent = `Page ${snapshot.page || page}`;
  $("#previous").disabled = page <= 1;
  $("#next").disabled = tasks.length < pageSize || page * pageSize >= (snapshot.total ?? Infinity);
  $("#tasks").innerHTML = tasks.map((task) => {
    const prs = Array.isArray(task.pull_requests) ? task.pull_requests : [];
    const repository = prs.length ? `${esc(task.repository || "—")} ${prs.map((pr) => safeLink(pr.url, `PR #${pr.number}`)).join(" ")}` : esc(task.repository || "—");
    return `<tr><th scope="row"><button class="task-link" type="button" data-task-id="${esc(task.id)}">${esc(task.summary || "Untitled incident")}</button></th><td>${esc(task.application || "—")}</td><td>${repository}</td><td>${esc(task.environment || "—")}</td><td><span class="state state-${esc(task.state)}">${esc(readableState(task.state))}</span></td><td>${esc(readableDuration(task.age_seconds))}</td><td>${esc(localTime(task.updated_at))}</td></tr>`;
  }).join("");
  document.querySelectorAll("[data-task-id]").forEach((button) => { button.addEventListener("click", () => openDetail(button.dataset.taskId)); });
}

function render(snapshot) {
  if (!snapshot || !snapshot.available) {
    $("#status").textContent = "Data unavailable";
    $("#runtime-meta").textContent = snapshot?.error || "Runtime database is unavailable; historical data is retained when it returns.";
    renderTasks(snapshot || {});
    return;
  }
  $("#status").textContent = `Live · refreshed ${new Date().toLocaleTimeString()}`;
  $("#runtime-meta").textContent = "Worker health is independent of dashboard health · read-only";
  renderCards(snapshot.summary || {});
  renderMetrics(snapshot.metrics || []);
  renderTasks(snapshot);
}

async function refresh() {
  try { render(await api(`/api/snapshot?${filterQuery()}`)); }
  catch (error) { $("#status").textContent = "Disconnected"; $("#runtime-meta").textContent = error.message; }
}

function renderStatusRecord(name, record) {
  if (typeof record === "string") return `<div class="repo-item"><strong>${esc(name)}</strong><span>${esc(record)}</span></div>`;
  if (!record || typeof record !== "object") return `<div class="repo-item"><strong>${esc(name)}</strong><span>${esc(record)}</span></div>`;
  const repository = record.repository || record.name || name;
  const status = record.status || record.state || record.result || "Unknown";
  const revision = record.pr_head_sha || record.sha || record.revision || record.commit || "";
  const environment = record.deployment_environment || record.environment || "";
  const tests = record.playwright_status || record.test_status || record.tests_status || (record.tests_passed === true ? "Passed" : record.tests_passed === false ? "Failed" : "");
  const deployment = record.deployment_url ? ` · ${safeLink(record.deployment_url, "deployment")}` : "";
  const verification = record.verification_status ? ` · verification ${esc(readableState(record.verification_status))}` : "";
  const revisionChecks = [["Deployment", record.deployment_sha], ["Verification", record.verification_sha]].map(([label, sha]) => {
    const match = !record.pr_head_sha || !sha ? "unknown" : record.pr_head_sha === sha ? "matches PR head" : "differs from PR head";
    return `<small>${label} revision: ${esc(match)}${sha ? ` · ${esc(sha)}` : ""}</small>`;
  }).join("");
  return `<div class="repo-item"><strong>${esc(repository)}</strong><span>${esc(readableState(status))}${environment ? ` · ${esc(environment)}` : ""}${tests ? ` · tests ${esc(readableState(tests))}` : ""}${verification}${revision ? ` · revision ${esc(revision)}` : ""}${deployment}</span>${revisionChecks}</div>`;
}

function renderRepositories(task) {
  const sections = [["Repositories", task.repositories || task.repository_status || task.repository_details], ["Deployments", task.deployments], ["Tests", task.tests]];
  const rendered = sections.filter(([, values]) => values).map(([label, values]) => {
    const items = Array.isArray(values) ? values.map((item, index) => [String(index + 1), item]) : Object.entries(values);
    return `<h3>${esc(label)}</h3>${items.map(([name, value]) => renderStatusRecord(name, value)).join("")}`;
  });
  return rendered.join("") || '<p class="muted">No repository deployment or test details recorded.</p>';
}

async function openDetail(id, moveFocus = true) {
  try {
    const task = await api(`/api/tasks/${encodeURIComponent(id)}`);
    currentTaskId = id;
    $("#detail").hidden = false;
    $("#detail-title").textContent = task.summary || "Incident details";
    $("#detail-meta").textContent = `${task.application || "—"} · ${task.environment || "—"} · ${readableState(task.state)} · updated ${localTime(task.updated_at)}`;
    $("#timeline").innerHTML = (task.events || []).map((event) => `<li><b>${esc(readableState(event.type))}</b> <time datetime="${esc(event.time || "")}">${esc(localTime(event.time))}</time><br>${esc(event.summary || "")}</li>`).join("") || '<li class="muted">No timeline events recorded.</li>';
    const prs = Array.isArray(task.pull_requests) ? task.pull_requests : [];
    $("#repositories").innerHTML = renderRepositories(task) + (prs.length ? `<h3>Pull requests</h3>${prs.map((pr) => `<p>${esc(pr.repository || "Repository")} ${safeLink(pr.url, `PR #${pr.number}`)}${pr.sha ? ` · revision ${esc(pr.sha)}` : ""}</p>`).join("")}` : "");
    if (moveFocus) $("#close-detail").focus();
  } catch (error) { $("#status").textContent = error.message; }
}

function connect() {
  if (source) source.close();
  source = new EventSource("/api/events");
  source.addEventListener("snapshot", () => { refresh(); if (currentTaskId) openDetail(currentTaskId, false); });
  source.onerror = () => { $("#status").textContent = "Disconnected · reconnecting"; };
}

async function signIn() {
  $("#login-error").textContent = "";
  try {
    await api("/login", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ token: $("#token").value }) });
    $("#token").value = ""; $("#login").hidden = true; $("#app").hidden = false; page = 1;
    await refresh(); connect();
  } catch (error) { $("#login-error").textContent = error.message || "Unable to sign in"; }
}

$("#sign-in").addEventListener("click", signIn);
$("#token").addEventListener("keydown", (event) => { if (event.key === "Enter") signIn(); });
$("#logout").addEventListener("click", async () => { try { await api("/logout", { method: "POST" }); showLogin(); } catch (error) { $("#status").textContent = error.message; } });
$("#close-detail").addEventListener("click", () => { $("#detail").hidden = true; currentTaskId = null; });
$("#filters").addEventListener("submit", (event) => { event.preventDefault(); page = 1; refresh(); });
$("#clear-filters").addEventListener("click", () => { $("#filters").reset(); page = 1; refresh(); });
$("#previous").addEventListener("click", () => { if (page > 1) { page -= 1; refresh(); } });
$("#next").addEventListener("click", () => { if (!$("#next").disabled) { page += 1; refresh(); } });

// Resume an already valid HttpOnly session after a browser reload.
(async () => {
  try {
    const snapshot = await api(`/api/snapshot?${filterQuery()}`);
    $("#login").hidden = true; $("#app").hidden = false; render(snapshot); connect();
  } catch (_) { /* unauthenticated browsers stay on the login panel */ }
})();
