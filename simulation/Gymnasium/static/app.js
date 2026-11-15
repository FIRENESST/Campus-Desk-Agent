"use strict";

const state = { venues: [], reservations: [] };
const elements = {
  venueGrid: document.querySelector("#venue-grid"),
  venueSelect: document.querySelector("#venue-select"),
  reservationForm: document.querySelector("#reservation-form"),
  reservationList: document.querySelector("#reservation-list"),
  emptyReservations: document.querySelector("#empty-reservations"),
  reservationCount: document.querySelector("#reservation-count"),
  totalRemaining: document.querySelector("#total-remaining"),
  totalVenues: document.querySelector("#total-venues"),
  totalReservations: document.querySelector("#total-reservations"),
  userName: document.querySelector("#user-name"),
  refreshButton: document.querySelector("#refresh-button"),
  submitButton: document.querySelector("#submit-button"),
  submitLabel: document.querySelector("#submit-label"),
  lastSynced: document.querySelector("#last-synced"),
  toast: document.querySelector("#toast"),
  toastCode: document.querySelector("#toast-code"),
  toastMessage: document.querySelector("#toast-message"),
};
let toastTimer;

function escapeHtml(value) {
  const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" };
  return String(value).replace(/[&<>"']/g, (character) => entities[character]);
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
  } catch (_error) {
    const error = new Error("无法连接场地服务，请确认服务仍在运行后重试。");
    error.code = "NETWORK_ERROR";
    throw error;
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch (_error) {
    // The error below gives an empty or non-JSON response a consistent UI result.
  }
  if (!response.ok) {
    const error = new Error(payload?.message || `请求失败（HTTP ${response.status}）`);
    error.code = payload?.code || "REQUEST_FAILED";
    error.retryable = Boolean(payload?.retryable);
    throw error;
  }
  return payload;
}

function showToast(code, message, isError = false) {
  window.clearTimeout(toastTimer);
  elements.toastCode.textContent = code;
  elements.toastMessage.textContent = message;
  elements.toast.classList.toggle("is-error", isError);
  elements.toast.classList.add("is-visible");
  elements.toast.setAttribute("aria-hidden", "false");
  toastTimer = window.setTimeout(() => {
    elements.toast.classList.remove("is-visible");
    elements.toast.setAttribute("aria-hidden", "true");
  }, isError ? 6000 : 3600);
}

function renderSummary() {
  elements.totalRemaining.textContent = state.venues.reduce((sum, venue) => sum + venue.remaining, 0);
  elements.totalVenues.textContent = state.venues.length;
  elements.totalReservations.textContent = state.reservations.length;
}

function renderVenueOptions() {
  const previous = elements.venueSelect.value;
  elements.venueSelect.innerHTML = state.venues.map((venue) => {
    const full = venue.remaining === 0;
    const label = full ? `${venue.name}（已满）` : `${venue.name}（余 ${venue.remaining} 席）`;
    return `<option value="${escapeHtml(venue.id)}" ${full ? "disabled" : ""}>${escapeHtml(label)}</option>`;
  }).join("");
  const previousAvailable = state.venues.some((venue) => venue.id === previous && venue.remaining > 0);
  if (previousAvailable) elements.venueSelect.value = previous;
  const hasAvailability = state.venues.some((venue) => venue.remaining > 0);
  elements.venueSelect.disabled = !hasAvailability;
  elements.submitButton.disabled = !hasAvailability;
}

function venueStatus(venue) {
  if (venue.remaining === 0) return { label: "名额已满", className: "full" };
  if (venue.remaining === 1) return { label: "仅剩 1 席", className: "low" };
  return { label: "开放预约", className: "" };
}

function renderVenues() {
  elements.venueGrid.setAttribute("aria-busy", "false");
  elements.venueGrid.innerHTML = state.venues.map((venue, index) => {
    const status = venueStatus(venue);
    const occupancy = venue.capacity ? Math.round((venue.reserved / venue.capacity) * 100) : 0;
    const full = venue.remaining === 0;
    return `
      <article class="venue-card ${full ? "is-full" : ""}">
        <div class="venue-card-top">
          <span class="venue-number">AREA / ${String(index + 1).padStart(2, "0")}</span>
          <span class="status-tag ${status.className}">${status.label}</span>
        </div>
        <h3>${escapeHtml(venue.name)}</h3>
        <span class="venue-code">${escapeHtml(venue.id)}</span>
        <div class="capacity-line">
          <div><strong class="remaining-number">${String(venue.remaining).padStart(2, "0")}</strong><span class="remaining-label">剩余名额</span></div>
          <span class="capacity-total">容量 <strong>${venue.capacity}</strong></span>
        </div>
        <div class="capacity-track" role="progressbar" aria-label="${escapeHtml(venue.name)}已预约容量" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${occupancy}"><span style="width:${occupancy}%"></span></div>
        <div class="capacity-caption"><span>已约 ${venue.reserved}</span><span>${occupancy}% OCCUPIED</span></div>
        <button class="button button-primary" type="button" data-action="reserve" data-venue-id="${escapeHtml(venue.id)}" ${full ? "disabled" : ""}>
          <span>${full ? "等待释放席位" : "预约此场地"}</span><span aria-hidden="true">${full ? "—" : "→"}</span>
        </button>
      </article>`;
  }).join("");
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date).replaceAll("/", ".");
}

function renderReservations() {
  const count = state.reservations.length;
  elements.reservationCount.textContent = `${count} 条进行中`;
  elements.emptyReservations.hidden = count !== 0;
  elements.reservationList.innerHTML = state.reservations.map((reservation) => {
    const id = escapeHtml(reservation.reservation_id);
    return `<tr>
      <td data-label="预约编号"><span class="reservation-id" title="${id}">#${escapeHtml(reservation.reservation_id.slice(-8).toUpperCase())}</span></td>
      <td data-label="训练者">${escapeHtml(reservation.user_name)}</td>
      <td data-label="训练场地" class="venue-name-cell">${escapeHtml(reservation.venue_name)}</td>
      <td data-label="预约时间" class="time-cell">${escapeHtml(formatTime(reservation.created_at))}</td>
      <td data-label="管理操作"><button class="cancel-button" type="button" data-action="cancel" data-reservation-id="${id}" aria-label="取消预约 ${id}">取消预约</button></td>
    </tr>`;
  }).join("");
}

function renderAll() {
  renderSummary();
  renderVenues();
  renderVenueOptions();
  renderReservations();
}

async function loadDashboard() {
  elements.refreshButton.disabled = true;
  elements.refreshButton.classList.add("is-loading");
  try {
    const [venuePayload, reservationPayload] = await Promise.all([
      request("/api/venues"),
      request("/api/reservations"),
    ]);
    state.venues = venuePayload.venues;
    state.reservations = reservationPayload.reservations;
    renderAll();
    elements.lastSynced.textContent = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(new Date());
  } catch (error) {
    elements.venueGrid.setAttribute("aria-busy", "false");
    elements.venueGrid.innerHTML = `<div class="load-error"><strong>场地数据同步失败</strong><p>${escapeHtml(error.message)}</p></div>`;
    showToast(error.code || "SYNC_ERROR", error.message, true);
  } finally {
    elements.refreshButton.disabled = false;
    elements.refreshButton.classList.remove("is-loading");
  }
}

async function cancelReservation(reservationId, button) {
  if (!window.confirm("确定取消这条预约吗？对应场地将立即释放一个名额。")) return;
  button.disabled = true;
  button.textContent = "正在取消";
  try {
    const result = await request(`/api/reservations/${encodeURIComponent(reservationId)}`, { method: "DELETE" });
    await loadDashboard();
    showToast("CANCELLED", `已取消 ${result.reservation.user_name} 的预约，席位已经释放。`);
  } catch (error) {
    showToast(error.code || "CANCEL_FAILED", error.message, true);
    button.disabled = false;
    button.textContent = "取消预约";
  }
}

elements.reservationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const venueId = elements.venueSelect.value;
  const userName = elements.userName.value.trim();
  if (!venueId || !userName) {
    showToast("INVALID_ARGUMENT", "请选择可用场地，并填写训练者姓名。", true);
    return;
  }

  elements.submitButton.disabled = true;
  elements.submitLabel.textContent = "正在锁定席位";
  try {
    const reservation = await request("/api/reservations", {
      method: "POST",
      body: JSON.stringify({ venue_id: venueId, user_name: userName }),
    });
    elements.reservationForm.reset();
    await loadDashboard();
    showToast("RESERVED", `已为 ${reservation.user_name} 保留 ${reservation.venue_name} 的 1 个席位。`);
  } catch (error) {
    showToast(error.code || "RESERVE_FAILED", error.message, true);
  } finally {
    elements.submitLabel.textContent = "确认预约";
    elements.submitButton.disabled = !state.venues.some((venue) => venue.remaining > 0);
  }
});

elements.venueGrid.addEventListener("click", (event) => {
  const button = event.target.closest('button[data-action="reserve"]');
  if (!button) return;
  elements.venueSelect.value = button.dataset.venueId;
  elements.userName.focus();
  elements.reservationForm.scrollIntoView({ behavior: "smooth", block: "center" });
});

elements.reservationList.addEventListener("click", (event) => {
  const button = event.target.closest('button[data-action="cancel"]');
  if (button) cancelReservation(button.dataset.reservationId, button);
});

elements.refreshButton.addEventListener("click", loadDashboard);
loadDashboard();
