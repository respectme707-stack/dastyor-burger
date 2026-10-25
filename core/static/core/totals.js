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
    cell.textContent = total ? "= " + fmt(total) + " сум" : "";
    stockHint(row, num(qty.value));
    return total;
  }

  // Закупка: подписи «как считаете» под выбранное сырьё и сколько придёт на склад.
  function stockHint(row, qty) {
    const material = row.querySelector('select[name$="-material"]');
    const how = row.querySelector('select[name$="-how"]');
    if (!material || !how) return;
    const u = units[material.value];
    const opts = how.options;
    if (u) {
      opts[0].text = "в " + u.unit;
      opts[1].text = u.big ? "в " + u.big : "в кг / л";
      opts[1].disabled = !u.big_ratio;
      opts[2].text = u.pack_size ? u.pack + " (" + u.pack_size.toLocaleString("ru-RU") + " " + u.unit + ")" : "упаковками";
      opts[2].disabled = !u.pack_size;
    }
    let hint = row.querySelector(".dastyor-stock");
    if (!hint) {
      hint = document.createElement("div");
      hint.className = "dastyor-line dastyor-stock";
      row.querySelector('input[name$="-qty"]').closest("td").appendChild(hint);
    }
    const ratio = !u ? 0 : how.value === "big" ? u.big_ratio : how.value === "pack" ? u.pack_size : 1;
    hint.textContent = u && qty && ratio ? "на склад: " + (qty * ratio).toLocaleString("ru-RU") + " " + u.unit : "";
  }

  function update() {
    let sum = 0;
    group.querySelectorAll("tr.form-row:not(.empty-form)").forEach((row) => { sum += rowTotal(row) || 0; });
    const p = paid ? num(paid.value) : 0;
    box.innerHTML = "<span>Итого: <b>" + fmt(sum) + " сум</b></span>" +
      (paid ? "<span>Оплачено сразу: <b>" + fmt(p) + "</b></span><span>В долг: <b class=\"" +
        (sum - p > 0 ? "debt" : "") + "\">" + fmt(Math.max(sum - p, 0)) + " сум</b></span>" : "");
  }

  document.addEventListener("input", update);
  document.addEventListener("change", update);
  document.addEventListener("formset:added", update);
  if (window.django && django.jQuery) django.jQuery(document).on("select2:select", update);
  update();
});
