"use strict";

const state = {
  rooms: [],
  reservations: [],
  connected: false
};

const chinaTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'short',
});

function roomInUse(reservation) {
  const now = Date.now();
  return !reservation.startsAt || Date.parse(reservation.startsAt) <= now && now < Date.parse(reservation.endsAt);
}

const elements = {
  connection: document.querySelector("#connection"),
  connectionText: document.querySelector("#connectionText"),
  roomsGrid: document.querySelector("#roomsGrid"),
  reservationsBody: document.querySelector("#reservationsBody"),
  reserveForm: document.querySelector("#reserveForm"),
  reserveButton: document.querySelector("#reserveButton"),
  refreshButton: document.querySelector("#refreshButton"),
  roomSelect: document.querySelector("#roomSelect"),
  userName: document.querySelector("#userName"),
  lastSync: document.querySelector("#lastSync"),
  toast: document.querySelector("#toast"),
  totalRooms: document.querySelector("#totalRooms"),
  availableRooms: document.querySelector("#availableRooms"),
  occupiedRooms: document.querySelector("#occupiedRooms"),
  activeReservations: document.querySelector("#activeReservations")
};

let socket;
let sequence = 0;
let reconnectTimer;
let toastTimer;
const pending = new Map();

function request(type, payload = {}) {
  return new Promise((resolve, reject) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      reject(new Error("调度网络尚未连接，请稍候重试。"));
      return;
    }

    const id = `web-${Date.now().toString(36)}-${++sequence}`;
    const timer = window.setTimeout(() => {
      pending.delete(id);
      reject(new Error("请求超时，请重新同步数据。"));
    }, 8000);

    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, type, ...payload }));
  });
}

function setConnection(status, text) {
  state.connected = status === "online";
  elements.connection.dataset.state = status;
  elements.connectionText.textContent = text;
  elements.reserveButton.disabled = !state.connected;
  elements.refreshButton.disabled = !state.connected;
  renderRooms();
}

function connect() {
  window.clearTimeout(reconnectTimer);
  setConnection("connecting", "正在接入调度网络");
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const activeSocket = new WebSocket(`${scheme}://${location.host}/ws`);
  socket = activeSocket;

  activeSocket.addEventListener("open", async () => {
    setConnection("online", "调度网络已连接");
    try {
      await refreshData();
    } catch (error) {
      showError(error);
    }
  });

  activeSocket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }

    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    window.clearTimeout(waiter.timer);

    if (message.type === "error") {
      const error = new Error(message.error?.message || "操作未能完成。");
      error.code = message.error?.code || "UNKNOWN_ERROR";
      waiter.reject(error);
    } else {
      waiter.resolve(message);
    }
  });

  activeSocket.addEventListener("close", () => {
    if (socket !== activeSocket) return;
    setConnection("offline", "连接中断，正在重试");
    for (const waiter of pending.values()) {
      window.clearTimeout(waiter.timer);
      waiter.reject(new Error("连接已中断，请重试。"));
    }
    pending.clear();
    reconnectTimer = window.setTimeout(connect, 3000);
  });

  activeSocket.addEventListener("error", () => {
    if (activeSocket.readyState === WebSocket.OPEN) activeSocket.close();
  });
}

async function refreshData() {
  elements.refreshButton.disabled = true;
  try {
    const [roomMessage, reservationMessage] = await Promise.all([
      request("list_rooms"),
      request("list_reservations")
    ]);
    state.rooms = roomMessage.rooms;
    state.reservations = reservationMessage.reservations;
    renderAll();
    elements.lastSync.textContent = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    }).format(new Date());
  } finally {
    elements.refreshButton.disabled = !state.connected;
  }
}

function renderAll() {
  renderSummary();
  renderRoomOptions();
  renderRooms();
  renderReservations();
}

function renderSummary() {
  const occupied = new Set(state.reservations.filter(roomInUse).map(item => item.roomId)).size;
  elements.totalRooms.textContent = String(state.rooms.length).padStart(2, "0");
  elements.availableRooms.textContent = String(state.rooms.length - occupied).padStart(2, "0");
  elements.occupiedRooms.textContent = String(occupied).padStart(2, "0");
  elements.activeReservations.textContent = String(state.reservations.filter(item =>
    !item.endsAt || Date.parse(item.endsAt) > Date.now()).length).padStart(2, "0");
}

function renderRoomOptions() {
  const selected = elements.roomSelect.value;
  const placeholder = new Option("选择教室", "");
  const options = state.rooms.map(room => {
    const occupied = state.reservations.some(item => item.roomId === room.roomId && roomInUse(item));
    return new Option(`${room.name} · ${room.capacity} 人${occupied ? "（当前占用）" : ""}`, room.roomId);
  });
  elements.roomSelect.replaceChildren(placeholder, ...options);
  if (state.rooms.some(room => room.roomId === selected)) elements.roomSelect.value = selected;
}

function renderRooms() {
  if (!state.rooms.length) {
    const note = document.createElement("p");
    note.className = "loading-note";
    note.textContent = state.connected ? "当前没有登记教室。" : "正在读取校园空间坐标…";
    elements.roomsGrid.replaceChildren(note);
    return;
  }

  const cards = state.rooms.map(room => {
    const reservation = state.reservations.find(item => item.roomId === room.roomId && roomInUse(item));
    const card = document.createElement("article");
    card.className = `room-card${reservation ? " is-occupied" : ""}`;

    const topLine = document.createElement("div");
    topLine.className = "room-topline";
    const code = document.createElement("span");
    code.className = "room-code";
    code.textContent = room.roomId.toUpperCase();
    const status = document.createElement("span");
    status.className = `status-tag${reservation ? " occupied" : ""}`;
    status.textContent = reservation ? "当前占用" : "当前空闲";
    topLine.append(code, status);

    const title = document.createElement("h3");
    title.textContent = room.name;
    const capacity = document.createElement("p");
    capacity.className = "room-capacity";
    const capacityValue = document.createElement("strong");
    capacityValue.textContent = room.capacity;
    capacity.append(capacityValue, document.createTextNode(" 个座位"));

    const select = document.createElement("button");
    select.type = "button";
    select.className = "room-select";
    select.disabled = !state.connected;
    select.textContent = "选择此教室 →";
    select.addEventListener("click", () => {
      elements.roomSelect.value = room.roomId;
      elements.userName.focus();
      document.querySelector("#bookingTitle").scrollIntoView({
        behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "start"
      });
    });

    card.append(topLine, title, capacity, select);
    return card;
  });
  elements.roomsGrid.replaceChildren(...cards);
}

function renderReservations() {
  if (!state.reservations.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "empty-cell";
    cell.textContent = "暂无活跃预约。选择一间教室，完成第一条登记。";
    row.append(cell);
    elements.reservationsBody.replaceChildren(row);
    return;
  }

  const rows = state.reservations.map(reservation => {
    const room = state.rooms.find(item => item.roomId === reservation.roomId);
    const row = document.createElement("tr");

    const idCell = document.createElement("td");
    idCell.dataset.label = "预约编号";
    const id = document.createElement("code");
    id.className = "reservation-id";
    id.textContent = reservation.reservationId;
    id.title = reservation.reservationId;
    idCell.append(id);

    const roomCell = document.createElement("td");
    roomCell.dataset.label = "教室坐标";
    roomCell.className = "room-cell";
    const roomName = document.createElement("strong");
    roomName.textContent = room?.name || reservation.roomId;
    const roomCode = document.createElement("small");
    roomCode.textContent = reservation.roomId;
    roomCell.append(roomName, roomCode);

    const userCell = makeCell(reservation.userName, "使用人");
    const timeCell = makeCell(reservation.startsAt
      ? `${chinaTime.format(new Date(reservation.startsAt))} – ${chinaTime.format(new Date(reservation.endsAt))}`
      : '未登记时段', '预约时段');
    const statusCell = document.createElement("td");
    statusCell.dataset.label = "状态";
    const status = document.createElement("span");
    status.className = roomInUse(reservation) ? "status-tag occupied" : "status-tag";
    status.textContent = roomInUse(reservation) ? "使用中" : Date.parse(reservation.endsAt) <= Date.now() ? "已结束" : "已预约";
    statusCell.append(status);

    const actionCell = document.createElement("td");
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "cancel-button";
    cancel.textContent = "取消预约";
    cancel.disabled = !state.connected;
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try {
        await request("cancel_reservation", { reservation_id: reservation.reservationId });
        showToast(`已取消 ${room?.name || reservation.roomId} 的预约。`);
        await refreshData();
      } catch (error) {
        cancel.disabled = false;
        showError(error);
      }
    });
    actionCell.append(cancel);

    row.append(idCell, roomCell, userCell, timeCell, statusCell, actionCell);
    return row;
  });
  elements.reservationsBody.replaceChildren(...rows);
}

function makeCell(text, label) {
  const cell = document.createElement("td");
  cell.dataset.label = label;
  cell.textContent = text;
  return cell;
}

function showToast(message, isError = false) {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("is-error", isError);
  elements.toast.hidden = false;
  toastTimer = window.setTimeout(() => { elements.toast.hidden = true; }, 4200);
}

function showError(error) {
  showToast(error.message || "操作未能完成，请重试。", true);
}

elements.reserveForm.addEventListener("submit", async event => {
  event.preventDefault();
  const roomId = elements.roomSelect.value;
  const userName = elements.userName.value.trim();
  const times = new FormData(elements.reserveForm);
  const start = times.get("starts_at");
  const end = times.get("ends_at");
  if (!roomId || !userName) {
    showToast("请选择教室并填写使用人姓名。", true);
    return;
  }
  if (!start || !end || end <= start) {
    showToast("请选择起止时间，结束时间须晚于开始时间。", true);
    return;
  }

  elements.reserveButton.disabled = true;
  try {
    await request("reserve_room", { room_id: roomId, user_name: userName, starts_at: `${start}+08:00`, ends_at: `${end}+08:00` });
    const room = state.rooms.find(item => item.roomId === roomId);
    elements.reserveForm.reset();
    showToast(`预约成功：${room?.name || roomId} · ${userName}`);
    await refreshData();
  } catch (error) {
    showError(error);
  } finally {
    elements.reserveButton.disabled = !state.connected;
  }
});

elements.refreshButton.addEventListener("click", async () => {
  try {
    await refreshData();
    showToast("教室与预约台账已同步。 ");
  } catch (error) {
    showError(error);
  }
});

connect();
