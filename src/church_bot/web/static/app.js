// 服事提醒機器人 — 管理網頁的小功能（不依賴任何外部套件）

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

// 瀏覽器「上一頁」回來時，把被鎖住的按鈕恢復
window.addEventListener("pageshow", () => {
  document.querySelectorAll("button[disabled][data-label]").forEach((button) => {
    button.disabled = false;
    button.textContent = button.dataset.label;
  });
});
