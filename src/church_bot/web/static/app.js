// 服事提醒機器人 — 管理網頁的小功能（不依賴任何外部套件；沒有 JavaScript 時頁面照樣能用）

// <form data-confirm="..."> 送出前先跳確認視窗
document.addEventListener("submit", (event) => {
  const message = event.target.dataset && event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

// 送出後把按鈕鎖住，避免連點造成重複發送
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

// 「恢復預設模板」
document.querySelectorAll("[data-reset-template]").forEach((button) => {
  button.addEventListener("click", () => {
    const textarea = document.getElementById("template");
    if (textarea && window.confirm("要把訊息模板改回預設值嗎？（按「儲存設定」後才會生效）")) {
      textarea.value = textarea.dataset.default;
    }
  });
});

// 點一下 ID 就複製
document.querySelectorAll(".copy").forEach((element) => {
  element.title = "點一下複製";
  element.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(element.textContent.trim());
      element.classList.add("copied");
      setTimeout(() => element.classList.remove("copied"), 1200);
    } catch (_) {
      /* 瀏覽器不允許就算了，使用者還是可以手動選取複製 */
    }
  });
});

// 手機：☰ 打開／關閉左邊選單
(() => {
  const toggle = document.querySelector(".menu-toggle");
  const backdrop = document.querySelector(".backdrop");
  if (!toggle) return;
  const set = (open) => {
    document.body.classList.toggle("nav-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    if (backdrop) backdrop.hidden = !open;
  };
  toggle.addEventListener("click", () => set(!document.body.classList.contains("nav-open")));
  if (backdrop) backdrop.addEventListener("click", () => set(false));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") set(false); });
})();

// 主控台的「第一次來？三句話看懂」：按「我知道了」就收起來（記在這台瀏覽器），按 ⓘ 再叫回來
(() => {
  const storage = { get: (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } },
                    set: (k, v) => { try { localStorage.setItem(k, v); } catch (_) { /* 私密視窗等情況就算了 */ } },
                    del: (k) => { try { localStorage.removeItem(k); } catch (_) { /* 同上 */ } } };
  document.querySelectorAll("[data-dismissable]").forEach((box) => {
    const key = "dismissed:" + box.dataset.dismissable;
    if (storage.get(key)) box.hidden = true;
    box.querySelectorAll("[data-dismiss]").forEach((button) => button.addEventListener("click", () => {
      storage.set(key, "1");
      box.hidden = true;
    }));
  });
  document.querySelectorAll("[data-undismiss]").forEach((button) => button.addEventListener("click", () => {
    const box = document.querySelector(`[data-dismissable="${button.dataset.undismiss}"]`);
    if (!box) return;
    storage.del("dismissed:" + button.dataset.undismiss);
    box.hidden = false;
    box.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
})();

// 頁籤（設定頁）：只顯示一個頁籤的內容；網址的 #段落 決定一開始開哪一頁，舊連結（#chat）也對得到
(() => {
  const ALIAS = { chat: "notify" };
  document.querySelectorAll("[data-tabs]").forEach((bar) => {
    const buttons = Array.from(bar.querySelectorAll("[data-tab]"));
    const panels = Array.from(document.querySelectorAll("[data-tab-panel]"));
    const saveBar = document.querySelector(".save-bar");
    const ids = buttons.map((b) => b.dataset.tab);
    const show = (id, push) => {
      buttons.forEach((b) => {
        const on = b.dataset.tab === id;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", String(on));
      });
      panels.forEach((p) => { p.hidden = p.dataset.tabPanel !== id; });
      if (saveBar) saveBar.hidden = !(saveBar.dataset.forPanels || "").split(" ").includes(id);
      if (push) history.replaceState(null, "", "#" + id);
    };
    buttons.forEach((b) => b.addEventListener("click", () => show(b.dataset.tab, true)));
    const fromHash = () => {
      const raw = location.hash.slice(1);
      const id = ALIAS[raw] || raw;
      show(ids.includes(id) ? id : ids[0], false);
    };
    window.addEventListener("hashchange", fromHash);
    fromHash();
  });
})();

// 表格搜尋框：<input data-filter="#table-id">，邊打邊過濾列
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

// 「點一下就加進去」的 chip：<button class="chip pick" data-pick-target="roles" data-pick-value="敬拜">
document.querySelectorAll(".chip.pick").forEach((chip) => {
  chip.addEventListener("click", () => {
    const input = document.getElementById(chip.dataset.pickTarget);
    if (!input) return;
    const parts = input.value.split(/[、,，;；/／\n]+/).map((s) => s.trim()).filter(Boolean);
    if (!parts.includes(chip.dataset.pickValue)) parts.push(chip.dataset.pickValue);
    input.value = parts.join("、");
    input.focus();
  });
});
