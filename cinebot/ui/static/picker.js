const $ = (id) => document.getElementById(id);

const selects = {
  location: $("location-select"),
  date: $("date-select"),
  movie: $("movie-select"),
  show: $("show-select"),
  seatClass: $("class-select"),
};

let config = null;
let dates = [];
let shows = [];
let seatCatalog = null;
let selectedShow = null;
let selectedClass = null;
let assignments = [[], [], [], []];
let activePayment = 0;
let runState = null;
let pollHandle = null;
let activeOtpSessionId = null;
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

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Request failed");
  return payload;
}

function setCatalogStatus(message, kind = "loading") {
  const status = $("catalog-status");
  status.className = `inline-status ${kind}`;
  status.textContent = message;
}

function setSelect(select, placeholder, items = [], formatter = (item) => item.title) {
  select.innerHTML = "";
  const first = document.createElement("option");
  first.value = "";
  first.textContent = placeholder;
  select.appendChild(first);
  items.forEach((item) => {
    const option = document.createElement("option");
    option.value = String(item.id ?? item.program_id ?? item.date);
    option.textContent = formatter(item);
    select.appendChild(option);
  });
  select.disabled = items.length === 0;
  select.value = "";
}

function clearFrom(level) {
  const order = ["date", "movie", "show", "seatClass"];
  const index = order.indexOf(level);
  const placeholders = {
    date: "Pick location first",
    movie: "Pick date first",
    show: "Pick movie first",
    seatClass: "Pick show first",
  };
  order.slice(index).forEach((name) => setSelect(selects[name], placeholders[name]));
  if (index === 0) dates = [];
  if (index <= 2) shows = [];
  selectedShow = null;
  selectedClass = null;
  seatCatalog = null;
  resetAssignments();
  $("show-summary").className = "show-summary empty";
  $("show-summary").textContent = "Complete the choices above to load the exact live seat map.";
  $("seat-stage").classList.add("locked");
  $("seat-map").innerHTML = '<div class="seat-placeholder">Choose a show and seat class to load its live seat map.</div>';
}

function dateLabel(value) {
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString("en-BD", {
    weekday: "short",
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

async function loadLocations() {
  $("reload-catalog").disabled = true;
  setCatalogStatus("Connecting to the public Cineplex schedule...", "loading");
  setSelect(selects.location, "Loading locations...");
  clearFrom("date");
  try {
    const payload = await api("/api/catalog/locations");
    setSelect(selects.location, "Choose a location", payload.locations);
    const sl = $("snipe-location");
    if (sl) {
      sl.innerHTML = "";
      payload.locations.forEach((item) => {
        const o = document.createElement("option");
        o.value = String(item.id);
        o.textContent = item.title;
        sl.appendChild(o);
      });
      if ([...sl.options].some((o) => o.value === "1")) sl.value = "1";
    }
    setCatalogStatus(
      `${payload.locations.length} Cineplex locations loaded. Pick from top to bottom.`,
      "ready",
    );
  } catch (error) {
    setCatalogStatus(error.message, "error");
    setSelect(selects.location, "Could not load locations");
  } finally {
    $("reload-catalog").disabled = false;
  }
}

async function onLocationChange() {
  clearFrom("date");
  if (!selects.location.value) return;
  selects.location.disabled = true;
  setCatalogStatus("Fetching every published date and movie for this location...", "loading");
  try {
    const payload = await api(`/api/catalog/dates/${encodeURIComponent(selects.location.value)}`);
    dates = payload.dates || [];
    setSelect(selects.date, "Choose a published date", dates, (item) => `${dateLabel(item.date)} - ${item.movies.length} movies`);
    setCatalogStatus(`${dates.length} published date${dates.length === 1 ? "" : "s"} loaded.`, "ready");
  } catch (error) {
    setCatalogStatus(error.message, "error");
  } finally {
    selects.location.disabled = false;
  }
}

function onDateChange() {
  clearFrom("movie");
  const day = dates.find((item) => item.date === selects.date.value);
  if (!day) return;
  setSelect(selects.movie, "Choose a movie", day.movies, (movie) => {
    const details = [movie.language, movie.category, movie.length].filter(Boolean).join(" / ");
    return details ? `${movie.title} - ${details}` : movie.title;
  });
  setCatalogStatus(`${day.movies.length} movies are published for ${dateLabel(day.date)}.`, "ready");
}

async function onMovieChange() {
  clearFrom("show");
  if (!selects.movie.value) return;
  selects.movie.disabled = true;
  setCatalogStatus("Fetching every hall, show time, seat class and price...", "loading");
  const params = new URLSearchParams({
    location_id: selects.location.value,
    movie_id: selects.movie.value,
    show_date: selects.date.value,
  });
  try {
    const payload = await api(`/api/catalog/shows?${params}`);
    shows = payload.shows || [];
    setSelect(selects.show, "Choose hall and show time", shows, (show) => {
      const priceText = show.seat_types
        .map((seatType) => `${seatType.title} BDT ${seatType.price}`)
        .join(" / ");
      return `${show.hall} - ${show.time_label}${priceText ? ` - ${priceText}` : ""}`;
    });
    setCatalogStatus(`${shows.length} hall/show option${shows.length === 1 ? "" : "s"} loaded.`, "ready");
  } catch (error) {
    setCatalogStatus(error.message, "error");
  } finally {
    selects.movie.disabled = false;
  }
}

async function onShowChange() {
  setSelect(selects.seatClass, "Pick show first");
  selectedShow = shows.find((item) => String(item.program_id) === selects.show.value) || null;
  selectedClass = null;
  seatCatalog = null;
  resetAssignments();
  if (!selectedShow) return;
  setSelect(selects.seatClass, "Choose a seat class", selectedShow.seat_types, (item) => `${item.title} - BDT ${item.price}`);
  selects.show.disabled = true;
  setCatalogStatus("Loading the current seat availability for this show...", "loading");
  const params = new URLSearchParams({
    location_id: selects.location.value,
    program_id: String(selectedShow.program_id),
  });
  try {
    seatCatalog = await api(`/api/catalog/seats?${params}`);
    setCatalogStatus("Live seat map loaded. Choose a seat class, then click exact seats.", "ready");
    if (selectedShow.seat_types.length === 1) {
      selects.seatClass.value = String(selectedShow.seat_types[0].id);
      onClassChange();
    }
  } catch (error) {
    setCatalogStatus(error.message, "error");
  } finally {
    selects.show.disabled = false;
  }
}

function onClassChange() {
  resetAssignments();
  if (!selectedShow || !seatCatalog || !selects.seatClass.value) {
    selectedClass = null;
    return;
  }
  selectedClass = selectedShow.seat_types.find((item) => String(item.id) === selects.seatClass.value) || null;
  const seatType = seatCatalog.seat_types.find((item) => String(item.id) === selects.seatClass.value);
  if (!selectedClass || !seatType) {
    $("seat-error").textContent = "That seat class is missing from the live seat map. Reload the show.";
    return;
  }
  $("seat-stage").classList.remove("locked");
  $("show-summary").className = "show-summary";
  $("show-summary").textContent = `${selectedShow.movie_title} / ${dateLabel(selectedShow.date)} / ${selectedShow.hall} / ${selectedShow.time_label} / ${selectedClass.title} / BDT ${selectedClass.price} each`;
  renderSeatMap(seatType);
  updateAssignments();
}

function renderSeatMap(seatType) {
  const rows = seatType.rows || [];
  $("seat-map").innerHTML = rows.map((row) => {
    const cells = row.cells.map((cell) => {
      if (cell.status === "gap") return '<span class="seat-gap" aria-hidden="true"></span>';
      const disabled = cell.status !== "available" ? "disabled" : "";
      const className = cell.status === "available" ? "seat" : "seat taken";
      return `<button class="${className}" type="button" data-seat="${escapeHtml(cell.label)}" ${disabled} aria-label="Seat ${escapeHtml(cell.label)}, ${cell.status}">${escapeHtml(cell.label)}</button>`;
    }).join("");
    return `<div class="seat-row"><span class="row-label">${escapeHtml(row.label)}</span><div class="row-cells" style="--cols:${seatType.n_cols}">${cells}</div></div>`;
  }).join("");
  document.querySelectorAll("button[data-seat]").forEach((button) => {
    button.addEventListener("click", () => toggleSeat(button.dataset.seat));
  });
  paintSeatAssignments();
}

function findAssigned(label) {
  return assignments.findIndex((seats) => seats.includes(label));
}

function toggleSeat(label) {
  $("seat-error").textContent = "";
  const owner = findAssigned(label);
  if (owner >= 0) {
    assignments[owner] = assignments[owner].filter((seat) => seat !== label);
  } else {
    if (assignments[activePayment].length >= config.max_seats_per_payment) {
      $("seat-error").textContent = `Payment ${activePayment + 1} already has 10 seats. Choose another payment tab.`;
      return;
    }
    assignments[activePayment].push(label);
    if (assignments[activePayment].length === config.max_seats_per_payment) {
      const next = assignments.findIndex((seats, index) => index > activePayment && seats.length < config.max_seats_per_payment);
      if (next >= 0) setActivePayment(next);
    }
  }
  updateAssignments();
  paintSeatAssignments();
}

function setActivePayment(index) {
  activePayment = index;
  document.querySelectorAll(".session-tab").forEach((tab) => {
    tab.classList.toggle("active", Number(tab.dataset.session) === index);
  });
}

function paymentCount() {
  return Math.max(1, Math.min(8, Number($("payment-count")?.value) || 4));
}

function minPayments() {
  return Math.ceil((Number($("snipe-total")?.value) || 36) / 10);
}

function clampPaymentCount() {
  const el = $("payment-count");
  if (!el) return;
  const mn = Math.max(1, Math.min(8, minPayments()));
  el.min = String(mn);
  if (paymentCount() < mn) el.value = String(mn);
}

function renderSessionTabs(count) {
  const root = $("session-tabs");
  if (!root) return;
  root.innerHTML = "";
  for (let i = 0; i < count; i++) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `session-tab s${i + 1}` + (i === activePayment ? " active" : "");
    b.dataset.session = String(i);
    b.innerHTML = `Payment ${i + 1} <b>${(assignments[i] || []).length}/10</b>`;
    b.addEventListener("click", () => setActivePayment(i));
    root.appendChild(b);
  }
}

function renderPaymentEntries(count) {
  const root = $("payment-fields");
  if (!root) return;
  const prev = [...root.querySelectorAll(".payment-entry")].map((e) => ({
    name: e.querySelector(".name-input")?.value || "",
    phone: e.querySelector(".phone-input")?.value || "",
  }));
  root.innerHTML = "";
  for (let i = 0; i < count; i++) {
    const a = document.createElement("article");
    a.className = `payment-entry s${i + 1}`;
    a.dataset.payment = String(i);
    const p = prev[i] || {};
    a.innerHTML =
      `<div class="payment-title"><b>${String(i + 1).padStart(2, "0")}</b><div><strong>Payment ${i + 1}</strong><span class="payment-seats">No seats assigned</span></div></div>` +
      `<label><span>Attendee name</span><input class="name-input" autocomplete="name" placeholder="Real attendee name" value="${escapeHtml(p.name)}"></label>` +
      `<label><span>bKash number</span><input class="phone-input" type="tel" inputmode="numeric" autocomplete="tel" maxlength="14" placeholder="01XXXXXXXXX" value="${escapeHtml(p.phone)}"></label>`;
    root.appendChild(a);
  }
  document.querySelectorAll(".payment-entry input").forEach((input) => input.addEventListener("input", updateStartButton));
}

function applyPaymentCount() {
  const count = paymentCount();
  const next = [];
  for (let i = 0; i < count; i++) next.push(assignments[i] || []);
  assignments = next;
  if (activePayment > count - 1) activePayment = count - 1;
  if (activePayment < 0) activePayment = 0;
  renderSessionTabs(count);
  renderPaymentEntries(count);
  const label = $("seat-total-label");
  if (label) label.textContent = `/ ${count * 10} selected`;
  updateAssignments();
}

function resetAssignments() {
  assignments = Array.from({ length: paymentCount() }, () => []);
  activePayment = 0;
  setActivePayment(0);
  $("seat-error").textContent = "";
  updateAssignments();
}

function paintSeatAssignments() {
  document.querySelectorAll("button[data-seat]").forEach((button) => {
    button.classList.remove("selected-0", "selected-1", "selected-2", "selected-3");
    const owner = findAssigned(button.dataset.seat);
    if (owner >= 0) button.classList.add(`selected-${owner}`);
  });
}

function updateAssignments() {
  const total = assignments.reduce((sum, seats) => sum + seats.length, 0);
  $("seat-count").textContent = total;
  document.querySelectorAll(".session-tab").forEach((tab, index) => {
    tab.querySelector("b").textContent = `${assignments[index].length}/10`;
  });
  document.querySelectorAll(".payment-entry").forEach((entry, index) => {
    entry.querySelector(".payment-seats").textContent = assignments[index].length
      ? assignments[index].join(", ")
      : "No seats assigned";
  });
  updateStartButton();
}

function normalizePhone(value) {
  return value.replace(/[\s-]+/g, "").trim().replace(/^\+88/, "").replace(/^88(?=01\d{9}$)/, "");
}

function readPayments() {
  const entries = [...document.querySelectorAll(".payment-entry")];
  const payments = entries.map((entry, index) => ({
    name: entry.querySelector(".name-input").value.replace(/\s+/g, " ").trim(),
    bkash_number: normalizePhone(entry.querySelector(".phone-input").value),
    seats: [...assignments[index]],
  }));
  if (payments.some((item) => !item.name)) throw new Error("Enter all four real attendee names.");
  if (new Set(payments.map((item) => item.name.toLocaleLowerCase())).size !== 4) throw new Error("Use a different attendee name for each payment.");
  if (payments.some((item) => !/^01[3-9]\d{8}$/.test(item.bkash_number))) throw new Error("Enter four valid Bangladesh bKash numbers.");
  if (new Set(payments.map((item) => item.bkash_number)).size !== 4) throw new Error("Use four different bKash numbers.");
  if (payments.some((item) => item.seats.length < 1 || item.seats.length > 10)) throw new Error("Assign between 1 and 10 seats to every payment.");
  return payments;
}

function buildTarget() {
  const locationOption = selects.location.options[selects.location.selectedIndex];
  if (!selectedShow || !selectedClass || !locationOption?.value) throw new Error("Finish choosing the live show and seat class.");
  return {
    location_id: Number(selects.location.value),
    location_name: locationOption.textContent,
    show_date: selects.date.value,
    movie_id: Number(selects.movie.value),
    movie_title: selectedShow.movie_title,
    program_id: selectedShow.program_id,
    screen_id: selectedShow.screen_id,
    hall_name: selectedShow.hall,
    show_time: selectedShow.time,
    seat_type_id: selectedClass.id,
    seat_type_name: selectedClass.title,
    unit_price: selectedClass.price,
  };
}

function updateStartButton() {
  const assigned = assignments.every((seats) => seats.length >= 1 && seats.length <= 10);
  $("start-button").disabled = !selectedShow || !selectedClass || !assigned || Boolean(runState?.busy);
}

async function startRun() {
  $("form-error").textContent = "";
  let body;
  try {
    body = { target: buildTarget(), payments: readPayments() };
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
    lockPicker(true);
    await refreshState();
    beginPolling();
  } catch (error) {
    $("form-error").textContent = error.message;
    updateStartButton();
  }
}

function lockPicker(locked) {
  document.querySelectorAll("select, button[data-seat], .session-tab, .payment-entry input, #reload-catalog").forEach((element) => {
    if (locked) {
      element.dataset.wasDisabled = element.disabled ? "1" : "0";
      element.disabled = true;
    } else if (element.dataset.wasDisabled === "0") {
      element.disabled = false;
      delete element.dataset.wasDisabled;
    }
  });
}

function beginPolling() {
  if (!pollHandle) pollHandle = window.setInterval(refreshState, 650);
}

function stopPolling() {
  if (pollHandle) window.clearInterval(pollHandle);
  pollHandle = null;
}

async function refreshState() {
  try {
    runState = await api("/api/group/state");
    renderRunState();
    if (!runState.busy && ["completed", "error", "stopped", "attention"].includes(runState.status)) {
      stopPolling();
      lockPicker(false);
      $("stop-button").hidden = true;
      updateStartButton();
    }
  } catch {
    // Preserve the form during a short local server restart.
  }
}

function renderRunState() {
  if (!runState) return;
  const badge = $("run-badge");
  badge.className = `run-badge ${runState.status}`;
  badge.textContent = STATUS_LABELS[runState.status] || runState.status;
  $("run-phase").textContent = runState.phase || "Working";
  $("run-detail").textContent = runState.detail || "";
  const sessions = runState.sessions || [];
  if (sessions.length) {
    $("session-list").innerHTML = sessions.map((session) => `
      <article class="run-card ${escapeHtml(session.status)}">
        <b>${String(session.index).padStart(2, "0")}</b>
        <div><strong>${escapeHtml(session.name)} / ${escapeHtml(session.phone_mask)}</strong><small>${escapeHtml(session.seats.join(", "))}</small></div>
        <em>${escapeHtml(sessionStatus(session))}</em>
      </article>`).join("");
  }
  const waiting = sessions.filter((session) => session.otp_required);
  if (waiting.length && !activeOtpSessionId) {
    openOtp(waiting[0].id);
  } else if (activeOtpSessionId) {
    const active = sessions.find((session) => session.id === activeOtpSessionId);
    if (!active || !active.otp_required) {
      closeOtp();
      const next = waiting[0];
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
  const session = runState?.sessions?.find((item) => item.id === sessionId);
  if (!session) return;
  activeOtpSessionId = sessionId;
  fillOtpModal(session);
  $("otp-code").value = "";
  $("otp-error").textContent = "";
  $("otp-modal").hidden = false;
  window.setTimeout(() => $("otp-code").focus(), 50);
}

function fillOtpModal(session) {
  $("otp-position").textContent = `Payment ${session.index} of ${runState.sessions.length}`;
  $("otp-name").textContent = session.name;
  $("otp-phone").textContent = session.phone;
  $("otp-seats").textContent = session.seats.join(", ");
  $("otp-amount").textContent = session.amount == null ? "Pending" : `BDT ${Number(session.amount).toLocaleString("en-BD")}`;
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
    $("otp-error").textContent = "Enter the numeric code from this number's bKash SMS.";
    return;
  }
  submittingOtp = true;
  $("otp-submit").disabled = true;
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
    lockPicker(false);
    closeOtp();
  }
}

let snipeState = null;
let snipePoll = null;

const SNIPE_BADGES = {
  idle: "Off", watching: "Watching", firing: "Firing",
  handed_off: "Live", error: "Error", stopped: "Stopped",
};

function snipeStatusKind(status) {
  if (status === "error") return "error";
  if (status === "firing" || status === "handed_off") return "ready";
  return "loading";
}

function setSnipeStatus(message, kind = "ready") {
  const status = $("snipe-status");
  status.className = `inline-status ${kind}`;
  status.textContent = message;
}

function readAttendees() {
  const entries = [...document.querySelectorAll(".payment-entry")];
  const attendees = entries.map((entry) => ({
    name: entry.querySelector(".name-input").value.replace(/\s+/g, " ").trim(),
    bkash: normalizePhone(entry.querySelector(".phone-input").value),
  }));
  if (attendees.some((item) => !item.name)) throw new Error("Enter all four attendee names in step 03.");
  if (attendees.some((item) => !/^01[3-9]\d{8}$/.test(item.bkash))) throw new Error("Enter four valid Bangladesh bKash numbers in step 03.");
  if (new Set(attendees.map((item) => item.bkash)).size !== attendees.length) throw new Error("Use a different bKash number per attendee.");
  return attendees;
}

function buildSnipeConfig() {
  const sl = $("snipe-location");
  const slOpt = sl && sl.options[sl.selectedIndex];
  if (!slOpt || !slOpt.value) throw new Error("Pick a sniper location.");
  if (paymentCount() < minPayments()) {
    throw new Error(`Need at least ${minPayments()} payment(s) for ${$("snipe-total").value} seats (10 per payment).`);
  }
  const attendees = readAttendees();
  const rows = $("snipe-rows").value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
  if (rows.length < 1) throw new Error("Primary rows are required (e.g. E,F).");
  return {
    target_movie: $("snipe-movie").value.trim() || "Spider-Man: Brand New Day",
    location_id: Number(sl.value),
    location_name: slOpt.textContent,
    hall_id: Number($("snipe-hall").value) || 6,
    show_date: $("snipe-date").value.trim() || "2026-08-01",
    time_start: $("snipe-start").value.trim() || "15:30",
    time_end: $("snipe-end").value.trim() || "18:00",
    poll_seconds: Number($("snipe-poll").value) || 75,
    total_seats: Number($("snipe-total").value) || 36,
    primary_rows: rows,
    fill_row: ($("snipe-fill").value.trim().toUpperCase() || "G"),
    trim_last: 2,
    num_payments: paymentCount(),
    attendees,
  };
}

async function loadSnipeConfig() {
  try {
    const cfg = await api("/api/snipe/config");
    if (!cfg || cfg.saved === false || !cfg.target_movie) return;
    $("snipe-movie").value = cfg.target_movie;
    $("snipe-date").value = cfg.show_date;
    const sl = $("snipe-location");
    if (sl && cfg.location_id) sl.value = String(cfg.location_id);
    $("snipe-hall").value = cfg.hall_id;
    $("snipe-start").value = cfg.time_start;
    $("snipe-end").value = cfg.time_end;
    $("snipe-total").value = cfg.total_seats;
    $("payment-count").value = String(cfg.num_payments || 4);
    clampPaymentCount();
    $("snipe-rows").value = (cfg.primary_rows || ["E", "F"]).join(",");
    $("snipe-fill").value = cfg.fill_row || "G";
    $("snipe-poll").value = cfg.poll_seconds;
    applyPaymentCount();
    (cfg.attendees || []).forEach((att, i) => {
      const e = document.querySelectorAll(".payment-entry")[i];
      if (!e) return;
      if (att.name) e.querySelector(".name-input").value = att.name;
      if (att.bkash) e.querySelector(".phone-input").value = att.bkash;
    });
    setSnipeStatus(
      `Saved target: ${cfg.target_movie} on ${cfg.show_date} — ${cfg.location_name} Hall ${cfg.hall_id}, ${cfg.time_start}-${cfg.time_end}. ${cfg.total_seats} seats (rows ${(cfg.primary_rows||[]).join(",")} minus last ${cfg.trim_last ?? 2}, fill ${cfg.fill_row}). ${cfg.attendees.length} attendees.`,
      "ready",
    );
  } catch {
    // no saved config yet
  }
}

async function snipeSave() {
  try {
    const cfg = buildSnipeConfig();
    await api("/api/snipe/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    setSnipeStatus(
      `Saved. ${cfg.total_seats} seats (rows ${cfg.primary_rows.join(",")} minus last 2 each, fill ${cfg.fill_row}) across ${cfg.attendees.length} attendees. Ready to watch.`,
      "ready",
    );
  } catch (error) {
    setSnipeStatus(error.message, "error");
  }
}

async function snipeStart() {
  try {
    const cfg = buildSnipeConfig();
    delete cfg._seat_count;
    await api("/api/snipe/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    $("snipe-start").hidden = true;
    $("snipe-stop").hidden = false;
    beginSnipePolling();
    beginPolling(); // also reflect the group run once it hands off
  } catch (error) {
    setSnipeStatus(error.message, "error");
  }
}

async function snipeStop() {
  try {
    await api("/api/snipe/stop", { method: "POST" });
  } catch {
    // ignore
  }
  stopSnipePolling();
  $("snipe-start").hidden = false;
  $("snipe-stop").hidden = true;
}

function beginSnipePolling() {
  if (!snipePoll) snipePoll = window.setInterval(refreshSnipe, 2000);
}

function stopSnipePolling() {
  if (snipePoll) window.clearInterval(snipePoll);
  snipePoll = null;
}

async function refreshSnipe() {
  try {
    snipeState = await api("/api/snipe/state");
    const ago = snipeState.ago_seconds == null ? "" : ` (last check ${snipeState.ago_seconds}s ago)`;
    setSnipeStatus(snipeState.detail + ago, snipeStatusKind(snipeState.status));
    const badge = $("snipe-badge");
    badge.className = `run-badge ${snipeState.status}`;
    badge.textContent = SNIPE_BADGES[snipeState.status] || snipeState.status;
    if (!snipeState.busy && ["handed_off", "error", "stopped"].includes(snapeState.status)) {
      stopSnipePolling();
      $("snipe-start").hidden = false;
      $("snipe-stop").hidden = true;
    }
  } catch {
    // keep last status during a brief server hiccup
  }
}

selects.location.addEventListener("change", onLocationChange);
selects.date.addEventListener("change", onDateChange);
selects.movie.addEventListener("change", onMovieChange);
selects.show.addEventListener("change", onShowChange);
selects.seatClass.addEventListener("change", onClassChange);
$("reload-catalog").addEventListener("click", loadLocations);
$("start-button").addEventListener("click", startRun);
$("stop-button").addEventListener("click", stopRun);
$("otp-submit").addEventListener("click", submitOtp);
$("otp-close").addEventListener("click", closeOtp);
$("otp-code").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    submitOtp();
  }
});
$("snipe-total").addEventListener("input", () => { clampPaymentCount(); applyPaymentCount(); });
$("payment-count").addEventListener("input", () => { clampPaymentCount(); applyPaymentCount(); });
$("snipe-save").addEventListener("click", snipeSave);
$("snipe-start").addEventListener("click", snipeStart);
$("snipe-stop").addEventListener("click", snipeStop);

Promise.all([api("/api/group/config"), refreshState(), api("/api/snipe/state")]).then(([loadedConfig, , snipe]) => {
  config = loadedConfig;
  applyPaymentCount();
  if (runState?.busy) beginPolling();
  loadSnipeConfig();
  if (snipe?.busy) {
    $("snipe-start").hidden = true;
    $("snipe-stop").hidden = false;
    beginSnipePolling();
  }
  loadLocations();
});
