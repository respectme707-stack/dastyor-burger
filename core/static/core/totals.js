// Закупка и отгрузка: сумма каждой строки, итог и долг считаются сразу, пока вводите.
document.addEventListener("DOMContentLoaded", function () {
  const group = document.getElementById("items-group");
  if (!group) return;
  const pricesEl = document.getElementById("product-prices");
  const prices = pricesEl ? JSON.parse(pricesEl.textContent) : {};
  const unitsEl = document.getElementById("material-units");
  const units = unitsEl ? JSON.parse(unitsEl.textContent) : {};
  const paid = document.getElementById("id_paid");
  const num = (v) => parseFloat(String(v || "").replace(/\s/g, "").replace(",", ".")) || 0;
  const fmt = (v) => Math.round(v).toLocaleString("ru-RU");

  const box = document.createElement("div");
  box.className = "dastyor-totals";
  group.after(box);

  function rowTotal(row) {
    const qty = row.querySelector('input[name$="-qty"]');
    const price = row.querySelector('input[name$="-price"]');
    if (!qty || !price) return null;
    const del = row.querySelector('input[name$="-DELETE"]');
    if (del && del.checked) return 0;
    let p = num(price.value);
    const product = row.querySelector('select[name$="-product"]');
    if (!price.value && product && prices[product.value]) p = prices[product.value];
    let total = num(qty.value) * p;
    let cell = row.querySelector(".dastyor-sum");
    if (!cell) {
      cell = document.createElement("div");
      cell.className = "dastyor-line dastyor-sum";
      price.closest("td").appendChild(cell);
    }
    cell.textContent = total ? "= " + fmt(total) + t(" сум") : "";
    stockHint(row, num(qty.value));
    return total;
  }

  // Закупка: в чём вводить количество и цену, и сколько придёт на склад.
  function stockHint(row, qty) {
    const material = row.querySelector('select[name$="-material"]');
    if (!material) return;
    const u = units[material.value];
    const qtyInput = row.querySelector('input[name$="-qty"]');
    const priceInput = row.querySelector('input[name$="-price"]');
    let hint = row.querySelector(".dastyor-stock");
    if (!hint) {
      hint = document.createElement("div");
      hint.className = "dastyor-line dastyor-stock";
      qtyInput.closest("td").appendChild(hint);
    }
    if (!u) { hint.textContent = ""; return; }
    // Прошлая цена: подставляем, когда сырьё только выбрали и цену ещё не вводили.
    if (material.dataset.priced !== material.value) {
      material.dataset.priced = material.value;
      if (!priceInput.value || priceInput.dataset.auto === priceInput.value) {
        priceInput.value = u.last_price || "";
        priceInput.dataset.auto = priceInput.value;
      }
    }
    const size = u.pack_size;
    const packs = [u.pack, t("по"), (size || 0).toLocaleString("ru-RU"), u.unit].filter(Boolean).join(" ");
    qtyInput.placeholder = size ? t("сколько упаковок") : t("сколько") + " " + u.unit;
    priceInput.placeholder = size ? t("цена за 1 упаковку") : t("цена за 1") + " " + u.unit;
    hint.textContent = (size
      ? packs + (qty ? " " + t("→ на склад") + " " + (qty * size).toLocaleString("ru-RU") + " " + u.unit : "")
      : (t("в") + " " + u.unit).trim()) +
      (u.last_price ? " · " + t("прошлая цена") + " " + u.last_price.toLocaleString("ru-RU") : "");
  }

  function update() {
    let sum = 0;
    group.querySelectorAll("tr.form-row:not(.empty-form)").forEach((row) => { sum += rowTotal(row) || 0; });
    const p = paid ? num(paid.value) : 0;
    box.innerHTML = "<span>" + t("Итого:") + " <b>" + fmt(sum) + t(" сум") + "</b></span>" +
      (paid ? "<span>" + t("Оплачено сразу:") + " <b>" + fmt(p) + "</b></span><span>" + t("В долг:") + " <b class=\"" +
        (sum - p > 0 ? "debt" : "") + "\">" + fmt(Math.max(sum - p, 0)) + t(" сум") + "</b></span>" : "");
  }

  document.addEventListener("input", update);
  document.addEventListener("change", update);
  document.addEventListener("formset:added", update);
  if (window.django && django.jQuery) django.jQuery(document).on("select2:select", update);
  update();
});
