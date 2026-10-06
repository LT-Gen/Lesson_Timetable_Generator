/* School Schedule - спільні клієнтські функції для всіх сторінок */

// ---------- Збереження позиції скролу через POST-редіректи ----------
// Дія (напр. видалення рядка в списку) - це <form method="post">, після якого
// сервер редіректить на ту саму сторінку (GET) - браузер за замовчуванням
// скролить нову сторінку наверх. Запам'ятовуємо позицію перед сабмітом і
// відновлюємо після завантаження, якщо повернулись на той самий шлях.
(function () {
  const KEY = "scroll-pos:" + window.location.pathname;

  document.addEventListener(
    "submit",
    function (e) {
      if (e.target.tagName === "FORM") {
        sessionStorage.setItem(KEY, String(window.scrollY));
      }
    },
    true
  );

  document.addEventListener("DOMContentLoaded", function () {
    const saved = sessionStorage.getItem(KEY);
    if (saved !== null) {
      sessionStorage.removeItem(KEY);
      window.scrollTo(0, parseInt(saved, 10) || 0);
    }
  });
})();

/**
 * Показує/приховує блоки залежно від значення <select>.
 * mapping: { 'idБлоку': ['значення1', 'значення2'], ... }
 * Блок видимий, якщо поточне значення select є в його списку.
 */
function toggleBlocksBySelect(selectId, mapping) {
  const select = document.getElementById(selectId);
  if (!select) return;

  function update() {
    const val = select.value;
    for (const [blockId, values] of Object.entries(mapping)) {
      const el = document.getElementById(blockId);
      if (!el) continue;
      const pokazaty = values.includes(val);
      el.style.display = pokazaty ? "block" : "none";
      if (!pokazaty) {
        // приховуємо блок - одразу знімаємо прапорці всередині, інакше вони
        // все одно надсилаються з формою (display:none не впливає на це) і
        // стара прив'язка "живе" поруч із новою в базі
        el.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
          cb.checked = false;
        });
      }
    }
  }

  select.addEventListener("change", update);
  update();
}

/**
 * Живий підрахунок суми кількох полів у поле-результат (напр. хлопці+дівчата → учнів).
 */
function liveSumDisplay(sourceIds, targetId) {
  const target = document.getElementById(targetId);
  if (!target) return;

  function update() {
    let sum = 0;
    for (const id of sourceIds) {
      sum += parseInt(document.getElementById(id).value) || 0;
    }
    target.value = sum;
  }

  for (const id of sourceIds) {
    const el = document.getElementById(id);
    if (el) el.addEventListener("input", update);
  }
  update();
}

/**
 * Сітка доступності (вчитель/клас/кабінет): день × урок. Компактна, з клікабельними
 * заголовками: клік по дню/уроку перемикає весь рядок/стовпець, клік по кутовій
 * клітинці (▦) перемикає всю сітку одразу.
 * config: { existing, allowNebazhanyi, gridId, dataInputId, formId }
 */
function initAvailabilityGrid(config) {
  const DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт"];
  const LESSONS = [1, 2, 3, 4, 5, 6, 7, 8];
  const STATES = config.allowNebazhanyi
    ? [null, "nedostupnyi", "nebazhanyi"]
    : [null, "nedostupnyi"];
  const COLORS = { null: "#fff", nedostupnyi: "#e74c3c", nebazhanyi: "#f39c12" };

  const store = {};
  for (let d = 1; d <= 5; d++) {
    store[d] = {};
    for (const u of LESSONS) store[d][u] = null;
  }
  for (const item of config.existing) {
    store[item.den][item.urok] = item.status;
  }

  // окремі "курсори" циклу для кожного рядка/стовпця/усієї сітки - щоб масовий клік
  // мав передбачувану послідовність станів, незалежно від поточного мішаного стану клітинок
  let rowCursor = {};
  let colCursor = {};
  let allCursor = 0;

  function renderGrid() {
    const grid = document.getElementById(config.gridId);
    let html = `<tr><th class="dostupnist-corner" style="cursor:pointer;" onclick="window.__availabilityCycleAll()" title="Перемкнути всю сітку">▦</th>`;
    html += DAYS.map((d, i) => `<th style="cursor:pointer;" onclick="window.__availabilityCycleCol(${i + 1})" title="Перемкнути весь стовпець">${d}</th>`).join("");
    html += "</tr>";
    for (const u of LESSONS) {
      html += `<tr><th style="cursor:pointer;" onclick="window.__availabilityCycleRow(${u})" title="Перемкнути весь рядок">${u}</th>`;
      for (let d = 1; d <= 5; d++) {
        const status = store[d][u];
        html += `<td class="dostupnist-cell" style="background:${COLORS[status]};"
                     onclick="window.__availabilityCycle(${d}, ${u})"></td>`;
      }
      html += "</tr>";
    }
    grid.innerHTML = html;
  }

  window.__availabilityCycle = function (den, urok) {
    const current = store[den][urok];
    const idx = STATES.indexOf(current);
    store[den][urok] = STATES[(idx + 1) % STATES.length];
    renderGrid();
  };

  window.__availabilityCycleRow = function (urok) {
    const cur = rowCursor[urok] === undefined ? 0 : rowCursor[urok];
    const next = (cur + 1) % STATES.length;
    rowCursor[urok] = next;
    for (let d = 1; d <= 5; d++) store[d][urok] = STATES[next];
    renderGrid();
  };

  window.__availabilityCycleCol = function (den) {
    const cur = colCursor[den] === undefined ? 0 : colCursor[den];
    const next = (cur + 1) % STATES.length;
    colCursor[den] = next;
    for (const u of LESSONS) store[den][u] = STATES[next];
    renderGrid();
  };

  window.__availabilityCycleAll = function () {
    allCursor = (allCursor + 1) % STATES.length;
    for (let d = 1; d <= 5; d++) {
      for (const u of LESSONS) store[d][u] = STATES[allCursor];
    }
    renderGrid();
  };

  document.getElementById(config.formId).addEventListener("submit", function () {
    const result = [];
    for (let d = 1; d <= 5; d++) {
      for (const u of LESSONS) {
        const status = store[d][u];
        if (status) result.push({ den: d, urok: u, status });
      }
    }
    document.getElementById(config.dataInputId).value = JSON.stringify(result);
  });

  renderGrid();
}

// ---------- Кнопка "?" (довідка по розділу) біля заголовка сторінки ----------
// Slug розділу приходить у <body data-dovidka="..."> (значення `active` з маршруту).
// Підсторінки ("Додатково"/"Доступність"/"Матриця") успадковують active батька,
// тож кнопка веде на той самий файл data/man/<slug>.md.
(function () {
  document.addEventListener("DOMContentLoaded", function () {
    const slug = (document.body.dataset.dovidka || "").trim();
    if (!slug) return;
    const h1 = document.querySelector("main h1");
    if (!h1 || h1.querySelector(".page-help")) return;
    const a = document.createElement("a");
    a.className = "page-help";
    a.href = "/dovidka/" + encodeURIComponent(slug);
    a.target = "_blank";
    a.rel = "noopener";
    a.title = "Довідка по цьому розділу";
    a.setAttribute("aria-label", "Довідка по цьому розділу");
    a.textContent = "?";
    h1.appendChild(a);
  });
})();

// ---------- Згортання бічного меню (кнопка) ----------
(function () {
  // Іконка "панель" (як перемикач бічної панелі) - прямокутник з поділом,
  // залита частина показує, яка сторона зараз розгорнута (як докер-перемикач).
  function ikonka_paneli(zliva_zalyta) {
    const liva = zliva_zalyta
      ? '<rect x="4" y="5" width="6.5" height="14" rx="1.5" fill="currentColor"/>'
      : '<rect x="13.5" y="5" width="6.5" height="14" rx="1.5" fill="currentColor"/>';
    return (
      '<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">' +
      '<rect x="3" y="4" width="18" height="16" rx="3" stroke="currentColor" stroke-width="1.6"/>' +
      liva +
      "</svg>"
    );
  }

  function onovyty_symvol(menu, btn) {
    const zgornuto = menu.classList.contains("zgornuto");
    // розгорнуто -> заливка зліва (видно ліву панель); згорнуто -> заливка справа
    btn.innerHTML = ikonka_paneli(!zgornuto);
  }

  document.addEventListener("DOMContentLoaded", function () {
    const menu = document.getElementById("golovne-menu");
    const btn = document.getElementById("zgortaty-menu-btn");
    if (!menu || !btn) return;

    // відновлюємо стан із попередньої сторінки (звичайний веб-застосунок на
    // машині користувача, не Claude-артефакт - localStorage тут доречний)
    if (localStorage.getItem("menu-zgornuto") === "1") {
      menu.classList.add("zgornuto");
    }
    onovyty_symvol(menu, btn);

    btn.addEventListener("click", function () {
      menu.classList.toggle("zgornuto");
      localStorage.setItem("menu-zgornuto", menu.classList.contains("zgornuto") ? "1" : "0");
      onovyty_symvol(menu, btn);
    });
  });
})();

/**
 * Сітка "Догенерація": клас × день, 3 стани (Замінити / Можливо / Не чіпати).
 * config: { klasy: [{id, nazva}], gridId }
 * Дані читаються через window.__getZamorozkaData() - викликається з обробника
 * подачі форм генерації (немає окремої форми, бо HTML не підтримує вкладені форми).
 */
function initZamorozkaGrid(config) {
  const DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт"];
  const STATES = [null, "mozhlyvo", "ne_chapaty"];
  const LABELS = { null: "Замінити", mozhlyvo: "Можливо", ne_chapaty: "Не чіпати" };
  const COLORS = { null: "#fff", mozhlyvo: "#fff3cd", ne_chapaty: "#f8d7da" };

  const store = {};
  for (const k of config.klasy) {
    store[k.id] = {};
    for (let d = 1; d <= 5; d++) store[k.id][d] = null;
  }

  let rowCursor = {};
  let colCursor = {};

  function renderGrid() {
    const grid = document.getElementById(config.gridId);
    let html = `<tr><th class="dostupnist-corner">Клас</th>`;
    html += DAYS.map((d, i) => `<th style="cursor:pointer;" onclick="window.__zamorozkaCycleCol(${i + 1})" title="Перемкнути весь стовпець">${d}</th>`).join("");
    html += "</tr>";
    for (const k of config.klasy) {
      html += `<tr><th style="cursor:pointer;" onclick="window.__zamorozkaCycleRow(${k.id})" title="Перемкнути весь клас">${k.nazva}</th>`;
      for (let d = 1; d <= 5; d++) {
        const status = store[k.id][d];
        html += `<td class="zamorozka-cell" style="background:${COLORS[status]};"
                     onclick="window.__zamorozkaCycle(${k.id}, ${d})">${LABELS[status]}</td>`;
      }
      html += "</tr>";
    }
    grid.innerHTML = html;
  }

  window.__zamorozkaCycle = function (klasId, den) {
    const current = store[klasId][den];
    const idx = STATES.indexOf(current);
    store[klasId][den] = STATES[(idx + 1) % STATES.length];
    renderGrid();
  };

  window.__zamorozkaCycleRow = function (klasId) {
    const cur = rowCursor[klasId] === undefined ? 0 : rowCursor[klasId];
    const next = (cur + 1) % STATES.length;
    rowCursor[klasId] = next;
    for (let d = 1; d <= 5; d++) store[klasId][d] = STATES[next];
    renderGrid();
  };

  window.__zamorozkaCycleCol = function (den) {
    const cur = colCursor[den] === undefined ? 0 : colCursor[den];
    const next = (cur + 1) % STATES.length;
    colCursor[den] = next;
    for (const k of config.klasy) store[k.id][den] = STATES[next];
    renderGrid();
  };

  window.__getZamorozkaData = function () {
    const result = [];
    for (const k of config.klasy) {
      for (let d = 1; d <= 5; d++) {
        const status = store[k.id][d];
        if (status) result.push({ klas_id: k.id, den: d, status });
      }
    }
    return JSON.stringify(result);
  };

  renderGrid();
}
