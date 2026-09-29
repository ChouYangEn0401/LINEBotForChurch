// 服事提醒機器人 — 管理網頁的小功能。
// 不依賴任何外部套件；沒有 JavaScript 時每一頁照樣能用（只是少了側欄的即時狀況、卡片開合這類方便）。

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* 私密視窗等情況就算了 */ } },
};

// ---------- 送出表單：先確認、再把按鈕鎖住（避免連點造成重複發送） ----------
document.addEventListener("submit", (event) => {
  const message = event.target.dataset && event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});
document.addEventListener("submit", (event) => {
  if (event.defaultPrevented) return;
  event.target.querySelectorAll("button[type=submit]").forEach((button) => {
    button.disabled = true;
    button.dataset.label = button.textContent;
    button.textContent = "處理中…";
  });
});
// 瀏覽器「上一頁」回來時，把被鎖住的按鈕恢復
window.addEventListener("pageshow", () => {
  document.querySelectorAll("button[disabled][data-label]").forEach((button) => {
    button.disabled = false;
    button.textContent = button.dataset.label;
  });
});

// ---------- 深色 / 淺色 ----------
(() => {
  const button = document.getElementById("btn-theme");
  if (!button) return;
  button.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("bot.theme", next);
  });
})();

// ---------- 手機：☰ 打開／關閉側欄 ----------
(() => {
  const toggle = document.querySelector(".menu-toggle");
  if (!toggle) return;
  toggle.addEventListener("click", () => document.body.classList.toggle("nav-open"));
  document.getElementById("view")?.addEventListener("click", () => document.body.classList.remove("nav-open"));
})();

// ---------- 側欄每一項「現在的狀況」 ----------
// 先用上次記下來的（換頁時不會閃），再去問 /api/nav 拿最新的。
(() => {
  const items = Array.from(document.querySelectorAll("[data-nav]"));
  if (!items.length) return;
  const paint = (status) => {
    items.forEach((item) => {
      const info = status && status[item.dataset.nav];
      if (!info) return;
      const sub = item.querySelector("[data-sub]");
      const count = item.querySelector("[data-count]");
      if (sub && info.sub) {
        sub.textContent = info.sub;
        sub.className = "sub " + (info.tone === "bad" || info.tone === "warn" ? info.tone : "");
        sub.title = info.sub;
      }
      if (count) {
        count.textContent = info.count ? String(info.count) : "";
        count.className = "count " + (info.tone || "");
      }
    });
  };
  try { paint(JSON.parse(sessionStorage.getItem("bot.nav") || "null")); } catch (_) { /* 壞掉的暫存就不用 */ }
  fetch("/api/nav", { credentials: "same-origin" })
    .then((response) => (response.ok ? response.json() : null))
    .then((status) => {
      if (!status) return;
      paint(status);
      try { sessionStorage.setItem("bot.nav", JSON.stringify(status)); } catch (_) { /* 同上 */ }
    })
    .catch(() => { /* 問不到就留著原本那行說明 */ });
})();

// ---------- 上方訊息列的 ✕ ----------
document.querySelectorAll("[data-dismiss-banner]").forEach((button) => {
  button.addEventListener("click", () => button.closest(".banner").remove());
});

// ---------- 頁面裡的分頁（服事表：整張表 / 程式讀到的結果 / 換一份） ----------
document.querySelectorAll("[data-seg]").forEach((bar) => {
  const buttons = Array.from(bar.querySelectorAll("[data-seg-btn]"));
  const panels = Array.from(document.querySelectorAll("[data-seg-panel]"));
  const ids = buttons.map((b) => b.dataset.segBtn);
  const show = (id, remember) => {
    buttons.forEach((b) => b.classList.toggle("on", b.dataset.segBtn === id));
    panels.forEach((p) => { p.hidden = p.dataset.segPanel !== id; });
    if (remember) history.replaceState(null, "", "#" + id);
  };
  buttons.forEach((b) => b.addEventListener("click", () => show(b.dataset.segBtn, true)));
  const fromHash = () => {
    const id = location.hash.slice(1);
    show(ids.includes(id) ? id : ids[0], false);
  };
  window.addEventListener("hashchange", fromHash);
  fromHash();
});

// ---------- 設定頁的卡片：點標題開合；記住每一張開著還是關著；網址的 #段落 會把那張打開 ----------
(() => {
  const cards = Array.from(document.querySelectorAll("[data-fcard]"));
  if (!cards.length) return;
  const key = (card) => "bot.fcard." + card.id;
  cards.forEach((card) => {
    const saved = store.get(key(card));
    if (saved === "open") card.classList.remove("closed");
    if (saved === "closed") card.classList.add("closed");
    card.querySelector("[data-fcard-toggle]")?.addEventListener("click", () => {
      card.classList.toggle("closed");
      store.set(key(card), card.classList.contains("closed") ? "closed" : "open");
    });
  });
  const openFromHash = () => {
    const target = document.getElementById(location.hash.slice(1));
    const card = target && target.closest("[data-fcard]");
    if (!card) return;
    card.classList.remove("closed");
    card.scrollIntoView({ block: "start" });
  };
  window.addEventListener("hashchange", openFromHash);
  openFromHash();
})();

// ---------- 抽屜（新增／編輯）：Esc 關掉 ----------
(() => {
  const drawer = document.querySelector("[data-drawer]");
  if (!drawer) return;
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !event.isComposing) location.href = drawer.dataset.close;
  });
})();

// ---------- 表格搜尋框：<input data-filter="#table-id">，邊打邊過濾 ----------
document.querySelectorAll("[data-filter]").forEach((input) => {
  const table = document.querySelector(input.dataset.filter);
  if (!table) return;
  input.addEventListener("input", () => {
    const needle = input.value.trim().toLowerCase();
    table.querySelectorAll("tbody tr").forEach((row) => {
      row.hidden = needle !== "" && !row.textContent.toLowerCase().includes(needle);
    });
  });
});

// ---------- 整列可以點（發送紀錄） ----------
document.querySelectorAll("tr[data-href]").forEach((row) => {
  row.addEventListener("click", (event) => {
    if (event.target.closest("a, button, input, select, form")) return;
    location.href = row.dataset.href;
  });
});

// ---------- 「點一下就加進去」：<button class="chip" data-pick-target="roles" data-pick-value="敬拜"> ----------
document.querySelectorAll("[data-pick-target]").forEach((chip) => {
  chip.addEventListener("click", () => {
    const input = document.getElementById(chip.dataset.pickTarget);
    if (!input) return;
    const parts = input.value.split(/[、,，;；/／\n]+/).map((s) => s.trim()).filter(Boolean);
    if (!parts.includes(chip.dataset.pickValue)) parts.push(chip.dataset.pickValue);
    input.value = parts.join("、");
    input.focus();
  });
});

// ---------- 「恢復預設模板」 ----------
document.querySelectorAll("[data-reset-template]").forEach((button) => {
  button.addEventListener("click", () => {
    const textarea = document.getElementById("template");
    if (textarea && window.confirm("要把訊息模板改回預設值嗎？按「儲存設定」後才會生效。")) {
      textarea.value = textarea.dataset.default;
    }
  });
});

// ---------- 點一下 ID 就複製 ----------
document.querySelectorAll(".copy").forEach((element) => {
  element.title = "點一下複製";
  element.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(element.textContent.trim());
      element.classList.add("copied");
      setTimeout(() => element.classList.remove("copied"), 1200);
    } catch (_) {
      /* 瀏覽器不允許就算了，還是可以手動選取複製 */
    }
  });
});
