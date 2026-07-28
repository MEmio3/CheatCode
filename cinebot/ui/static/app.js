const $ = (id) => document.getElementById(id);

let config = null;
let state = null;
let activeOtpSessionId = null;
let pollHandle = null;
let submittingOtp = false;

const STATUS_LABELS = {
  idle: "Ready",
  starting: "Checking",
  running: "Live",
  attention: "Attention",
  completed: "Complete",
  error: "Error",
  stopped: "Stopped",
};

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
  );
}

function money(value) {
  if (value == null) return "Pending";
  return `৳${Number(value).toLocaleString("en-BD")}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || "Request failed");
  }
  return payload;
}

async function loadConfig() {
  config = await api("/api/group/config");
  $("target-movie").textContent = config.movie;
  $("expected-seats").textContent = config.expected_seats;
}

function readForm() {
  const bkash = $("bkash-number").value.replace(/[\s-]+/g, "").trim();
  const names = [...document.querySelectorAll("#name-fields input")].map((input) =>
    input.value.replace(/\s+/g, " ").trim(),
  );
  if (!/^(?:\+?88)?01[3-9]\d{8}$/.test(bkash)) {
    throw new Error("Enter a valid Bangladesh bKash number.");
  }
  if (names.some((name) => !name)) {
    throw new Error("Enter all four real attendee names.");
  }
  if (new Set(names.map((name) => name.toLocaleLowerCase())).size !== names.length) {
    throw new Error("Use a different attendee name for each payment.");
  }
  return { bkash_number: bkash, names };
}

async function startRun(event) {
  event.preventDefault();
  $("form-error").textContent = "";
  let body;
  try {
    body = readForm();
  } catch (error) {
    $("form-error").textContent = error.message;
    return;
  }
  $("start-button").disabled = true;
  try {
    await api("/api/group/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    $("stop-button").hidden = false;
    await refreshState();
    beginPolling();
  } catch (error) {
    $("form-error").textContent = error.message;
    $("start-button").disabled = false;
  }
}

function beginPolling() {
  if (pollHandle) return;
  pollHandle = window.setInterval(refreshState, 650);
}

function stopPolling() {
  if (!pollHandle) return;
  window.clearInterval(pollHandle);
  pollHandle = null;
}

async function refreshState() {
  try {
    state = await api("/api/group/state");
    renderState();
    if (!state.busy && ["completed", "error", "stopped", "attention"].includes(state.status)) {
      stopPolling();
      $("start-button").disabled = false;
      $("stop-button").hidden = true;
    }
  } catch {
    // A short server restart should not destroy the user's form.
  }
}

function renderState() {
  if (!state) return;
  const badge = $("run-badge");
  badge.className = `run-badge ${state.status}`;
  badge.textContent = STATUS_LABELS[state.status] || state.status;
  $("run-phase").textContent = state.phase || "Working";
  $("run-detail").textContent = state.detail || "";

  if (state.show) {
    $("schedule-alert").innerHTML =
      `<strong>Live show found</strong>` +
      `<span>${escapeHtml(state.show.date)} · ${escapeHtml(state.show.hall)} · ` +
      `${escapeHtml(state.show.time)} · ${state.show.seat_count} seats across ` +
      `${state.show.payments} payments</span>`;
  } else if (state.status === "error") {
    $("schedule-alert").innerHTML =
      `<strong>Stopped safely</strong><span>${escapeHtml(state.error || state.detail)}</span>`;
  }

  const sessions = state.sessions || [];
  if (!sessions.length) return;
  $("session-list").innerHTML = sessions
    .map(
      (session) => `
        <article class="session-card ${escapeHtml(session.status)}">
          <span class="session-index">${String(session.index).padStart(2, "0")}</span>
          <div class="session-copy">
            <strong>${escapeHtml(session.name)} · ${session.seat_count} seats</strong>
            <span>${escapeHtml(session.seats.join(", "))} · ${escapeHtml(session.phone)}</span>
          </div>
          <span class="session-state">${escapeHtml(sessionStatus(session))}</span>
        </article>`,
    )
    .join("");

  const waiting = sessions.filter((session) => session.otp_required);
  if (waiting.length && !activeOtpSessionId) {
    openOtp(waiting[0].id);
  } else if (activeOtpSessionId) {
    const active = sessions.find((session) => session.id === activeOtpSessionId);
    if (!active || !active.otp_required) {
      closeOtp();
      const next = waiting.find((session) => session.id !== activeOtpSessionId);
      if (next) openOtp(next.id);
    } else {
      fillOtpModal(active);
    }
  }
}

function sessionStatus(session) {
  if (session.status === "waiting_otp") return "OTP needed";
  if (session.status === "pin_required") return "PIN in bKash";
  if (session.status === "completed") return "Confirmed";
  if (session.status === "failed") return "Failed";
  return session.detail || session.status.replaceAll("_", " ");
}

function openOtp(sessionId) {
  const session = state?.sessions?.find((item) => item.id === sessionId);
  if (!session) return;
  activeOtpSessionId = sessionId;
  fillOtpModal(session);
  $("otp-code").value = "";
  $("otp-error").textContent = "";
  $("otp-modal").hidden = false;
  window.setTimeout(() => $("otp-code").focus(), 50);
}

function fillOtpModal(session) {
  const total = state.sessions.length;
  $("otp-position").textContent = `Payment ${session.index} of ${total}`;
  $("otp-name").textContent = session.name;
  $("otp-phone").textContent = session.phone;
  $("otp-seats").textContent = session.seats.join(", ");
  $("otp-amount").textContent = money(session.amount);
  $("otp-invoice").textContent = session.invoice || "Pending";
}

function closeOtp() {
  $("otp-modal").hidden = true;
  activeOtpSessionId = null;
  $("otp-error").textContent = "";
  $("otp-code").value = "";
}

async function submitOtp() {
  if (submittingOtp || !activeOtpSessionId) return;
  const code = $("otp-code").value.replace(/\s+/g, "");
  if (!/^\d{4,8}$/.test(code)) {
    $("otp-error").textContent = "Enter the numeric code from the matching bKash SMS.";
    return;
  }
  submittingOtp = true;
  $("otp-submit").disabled = true;
  $("otp-error").textContent = "";
  try {
    await api("/api/group/otp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: activeOtpSessionId, code }),
    });
    closeOtp();
    await refreshState();
  } catch (error) {
    $("otp-error").textContent = error.message;
  } finally {
    submittingOtp = false;
    $("otp-submit").disabled = false;
  }
}

async function stopRun() {
  $("stop-button").disabled = true;
  try {
    await api("/api/group/stop", { method: "POST" });
    await refreshState();
  } finally {
    $("stop-button").disabled = false;
    $("start-button").disabled = false;
    $("stop-button").hidden = true;
    closeOtp();
  }
}

$("booking-form").addEventListener("submit", startRun);
$("stop-button").addEventListener("click", stopRun);
$("otp-submit").addEventListener("click", submitOtp);
$("otp-close").addEventListener("click", closeOtp);
$("otp-code").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    submitOtp();
  }
});
$("otp-modal").addEventListener("click", (event) => {
  if (event.target === $("otp-modal")) closeOtp();
});

Promise.all([loadConfig(), refreshState()]).then(() => {
  if (state?.busy) beginPolling();
});
