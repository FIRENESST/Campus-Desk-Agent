"use strict";

const state = { books: [], loans: [] };
const booksBody = document.querySelector("#books-body");
const loansBody = document.querySelector("#loans-body");
const bookSelect = document.querySelector("#book-select");
const borrowForm = document.querySelector("#borrow-form");
const borrowButton = document.querySelector("#borrow-submit");
const notice = document.querySelector("#notice");
let noticeTimer;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers }
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("服务返回了无法识别的内容");
  }
  if (!response.ok) {
    throw new Error(payload.error?.message || `请求失败（${response.status}）`);
  }
  return payload;
}

function showNotice(message, isError = false) {
  clearTimeout(noticeTimer);
  notice.textContent = message;
  notice.classList.toggle("error", isError);
  notice.classList.add("visible");
  noticeTimer = setTimeout(() => notice.classList.remove("visible"), 3600);
}

function cell(label, className) {
  const element = document.createElement("td");
  element.dataset.label = label;
  if (className) element.className = className;
  return element;
}

function renderBooks() {
  const selected = bookSelect.value;
  const rows = state.books.map((book) => {
    const row = document.createElement("tr");
    const id = cell("编号", "book-id");
    id.textContent = book.id;
    const title = cell("书名");
    const titleText = document.createElement("strong");
    titleText.className = "book-title";
    titleText.textContent = `《${book.title}》`;
    title.append(titleText);
    const author = cell("著者");
    author.textContent = book.author;
    const copies = cell("副本", "copies");
    const available = document.createElement("strong");
    available.textContent = book.available_copies;
    const total = document.createElement("span");
    total.textContent = ` / ${book.total_copies} 册`;
    copies.append(available, total);
    const status = cell("状态");
    const badge = document.createElement("span");
    badge.className = `availability${book.available_copies === 0 ? " unavailable" : ""}`;
    badge.textContent = book.available_copies === 0 ? "已全部借出" : "在架可借";
    status.append(badge);
    row.append(id, title, author, copies, status);
    return row;
  });
  booksBody.replaceChildren(...rows);

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = state.books.length ? "请选择要借阅的书" : "暂无馆藏";
  bookSelect.replaceChildren(placeholder);
  for (const book of state.books) {
    const option = document.createElement("option");
    option.value = book.id;
    option.textContent = `《${book.title}》 · 可借 ${book.available_copies}/${book.total_copies}`;
    option.disabled = book.available_copies === 0;
    bookSelect.append(option);
  }
  if (state.books.some((book) => book.id === selected && book.available_copies > 0)) {
    bookSelect.value = selected;
  }
}

function renderLoans() {
  const rows = state.loans.map((loan) => {
    const row = document.createElement("tr");
    const id = cell("借阅编号", "loan-id");
    id.textContent = loan.loan_id;
    const title = cell("书名");
    const titleText = document.createElement("strong");
    titleText.className = "book-title";
    titleText.textContent = `《${loan.book_title}》`;
    title.append(titleText);
    const borrower = cell("读者");
    borrower.textContent = loan.borrower_name;
    const action = cell("操作");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "return-button";
    button.textContent = "归还此书";
    button.setAttribute("aria-label", `归还 ${loan.borrower_name} 借阅的${loan.book_title}`);
    button.addEventListener("click", () => returnBook(loan, button));
    action.append(button);
    row.append(id, title, borrower, action);
    return row;
  });
  loansBody.replaceChildren(...rows);
  document.querySelector("#loans-empty").hidden = state.loans.length !== 0;
  document.querySelector("#loans-table-wrap").hidden = state.loans.length === 0;
}

function renderSummary() {
  document.querySelector("#stat-books").textContent = state.books.length;
  document.querySelector("#stat-copies").textContent = state.books.reduce((sum, book) => sum + book.available_copies, 0);
  document.querySelector("#stat-loans").textContent = state.loans.length;
}

async function refresh() {
  const refreshButton = document.querySelector("#refresh-button");
  refreshButton.disabled = true;
  try {
    const [bookPayload, loanPayload] = await Promise.all([
      api("/api/books"),
      api("/api/loans")
    ]);
    state.books = bookPayload.books;
    state.loans = loanPayload.loans;
    renderBooks();
    renderLoans();
    renderSummary();
  } catch (error) {
    showNotice(error.message, true);
    const row = document.createElement("tr");
    row.className = "error-row";
    const message = cell("提示");
    message.colSpan = 5;
    message.textContent = "目录暂未展开，请点击“刷新簿册”重试。";
    row.append(message);
    booksBody.replaceChildren(row);
  } finally {
    refreshButton.disabled = false;
  }
}

borrowForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const bookId = bookSelect.value;
  const borrowerName = document.querySelector("#borrower-name").value;
  borrowButton.disabled = true;
  try {
    const payload = await api("/api/loans", {
      method: "POST",
      body: JSON.stringify({ book_id: bookId, borrower_name: borrowerName })
    });
    showNotice(`借阅成功：${payload.loan.borrower_name} ·《${payload.loan.book_title}》`);
    borrowForm.reset();
    await refresh();
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    borrowButton.disabled = false;
  }
});

async function returnBook(loan, button) {
  button.disabled = true;
  try {
    await api(`/api/loans/${encodeURIComponent(loan.loan_id)}`, { method: "DELETE" });
    showNotice(`归还成功：《${loan.book_title}》已重新在架`);
    await refresh();
  } catch (error) {
    showNotice(error.message, true);
    button.disabled = false;
  }
}

document.querySelector("#refresh-button").addEventListener("click", refresh);
refresh();
