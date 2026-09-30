// 「服事表」頁：把 Google Sheet（或 CSV）的格子原封不動畫成像試算表的表格。
// 資料來自 /api/roster/sheet，順便標出：表頭那一列、日期格、這次會發的那幾列（或欄）、名單找不到的名字。

(async () => {
  const root = document.getElementById("sheet");
  if (!root) return;
  const status = root.querySelector(".sheet-status");
  const body = root.querySelector(".sheet-body");
  const jump = root.querySelector("[data-sheet-jump]");

  const colName = (index) => {  // 0 → A, 25 → Z, 26 → AA
    let name = "";
    for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) name = String.fromCharCode(64 + ((n - 1) % 26) + 1) + name;
    return name;
  };
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const within = (iso, data) => iso >= data.today && iso <= data.window_end;
  // 跟後端 split_names() 用同一組分隔符，把格子拆成一個一個名字，再逐一比對——
  // 不能用「格子文字整段 includes(某個對不到的名字)」，不然「晨光」對不到會連帶把
  // 完全不相干、但剛好包含這兩個字的「晨光實體團」也標成對不到（子字串誤判）。
  const cellNames = (text) => text.split(/[、,，;；/／&＆+＋\n]+/).map((s) => s.trim()).filter(Boolean);

  function renderSheet(sheet, data) {
    const rows = sheet.rows || [];
    const width = rows.reduce((max, row) => Math.max(max, row.length), 0);
    const unknown = (data.unknown_names || []).filter(Boolean);
    const dateCol = sheet.date_axis === "row" ? sheet.cols.date : -1;

    const wrap = el("div");
    if ((data.sheets || []).length > 1) wrap.appendChild(el("div", "sheet-title", sheet.label));
    const scroll = el("div", "sheet-scroll");
    const table = el("table", "sheet");

    const thead = el("thead");
    const headRow = el("tr");
    headRow.appendChild(el("th", "corner row", ""));
    for (let c = 0; c < width; c++) {
      const th = el("th", "col", colName(c));
      const iso = sheet.date_axis === "col" ? sheet.dates[String(c)] : undefined;
      if (iso && within(iso, data)) th.classList.add("is-current");
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = el("tbody");
    let firstCurrent = null;
    rows.forEach((row, r) => {
      const tr = el("tr");
      const isHeader = r === sheet.header_row;
      if (isHeader) tr.classList.add("is-header");
      const rowIso = sheet.date_axis === "row" ? sheet.dates[String(r)] : undefined;
      if (rowIso) {
        if (within(rowIso, data)) tr.classList.add("is-current");
        else if (rowIso < data.today) tr.classList.add("is-past");
      }
      tr.appendChild(el("th", "row", String(r + 1)));
      for (let c = 0; c < width; c++) {
        const text = row[c] == null ? "" : String(row[c]);
        const td = el("td", "", text);
        if (text) td.title = text;
        if (!isHeader) {
          if (sheet.date_axis === "row" && c === dateCol && rowIso) td.classList.add("is-date");
          if (sheet.date_axis === "col") {
            const colIso = sheet.dates[String(c)];
            if (colIso && r > sheet.header_row) {
              if (within(colIso, data)) td.classList.add("is-current");
              else if (colIso < data.today) td.classList.add("is-past");
            }
          }
          const isNameCell = r > sheet.header_row && c !== dateCol && !(sheet.date_axis === "col" && c === 0);
          if (isNameCell && text && cellNames(text).some((name) => unknown.includes(name))) {
            td.classList.add("is-unknown");
            td.title = text + "　←　同工名單找不到這個名字";
          }
        } else if (sheet.date_axis === "col" && sheet.dates[String(c)]) {
          td.classList.add("is-date");
        }
        tr.appendChild(td);
      }
      if (tr.classList.contains("is-current") && !firstCurrent) firstCurrent = tr;
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scroll.appendChild(table);
    wrap.appendChild(scroll);

    const layoutZh = { wide: "日期在左、一列一次聚會", long: "一列一項服事", matrix: "日期在上、一欄一次聚會" }[sheet.layout];
    const parts = [`${rows.length} 列 × ${width} 欄`];
    if (layoutZh) parts.push(`排法：${layoutZh}`, `表頭在第 ${sheet.header_row + 1} 列`);
    else parts.push("看不出表頭在哪一列，所以沒有上色");
    wrap.appendChild(el("div", "sheet-caption", parts.join(" · ")));

    wrap.firstCurrent = firstCurrent;
    wrap.scroll = scroll;
    return wrap;
  }

  try {
    const response = await fetch("/api/roster/sheet", { credentials: "same-origin" });
    const data = await response.json();
    if (!data.ok) {
      status.classList.add("error");
      status.textContent = `讀不到：${data.error}${data.hint ? "。" + data.hint : ""}`;
      if (jump) jump.hidden = true;
      return;
    }
    body.replaceChildren(...data.sheets.map((sheet) => renderSheet(sheet, data)));
    status.remove();
    const target = Array.from(body.children).find((node) => node.firstCurrent);
    if (jump) {
      if (!target) jump.hidden = true;
      else jump.addEventListener("click", () => {
        target.scroll.scrollTo({ top: Math.max(target.firstCurrent.offsetTop - 60, 0), behavior: "smooth" });
      });
    }
  } catch (error) {
    status.classList.add("error");
    status.textContent = "讀取時出了問題，請重新整理再試一次。";
  }
})();
