// Производство: что нужно на рецепт, сколько этого на складе и сколько штук можно сделать.
document.addEventListener("DOMContentLoaded", function () {
  const dataEl = document.getElementById("recipe-stock");
  const product = document.getElementById("id_product");
  const qty = document.getElementById("id_qty");
  if (!dataEl || !product) return;
  const recipes = JSON.parse(dataEl.textContent);
  const fmt = (v) => (Math.round(v * 1000) / 1000).toLocaleString("ru-RU");
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

  const box = document.createElement("div");
  box.className = "dastyor-recipe";
  product.closest(".form-row").after(box);

  function update() {
    const lines = recipes[product.value];
    if (!product.value) { box.innerHTML = ""; return; }
    if (!lines || !lines.length) {
      box.innerHTML = "<p class=\"bad\">" + t("У продукта нет рецепта. Сначала заполните рецепт.") + "</p>";
      return;
    }
    const n = parseInt(qty && qty.value, 10) || 0;
    const canMake = Math.min(...lines.map((x) => (x.per > 0 ? Math.floor(x.stock / x.per + 1e-9) : Infinity)));
    let rows = "";
    lines.forEach((x) => {
      const need = x.per * n;
      const short = n && need > x.stock + 1e-9;
      rows += "<tr" + (short ? " class=\"bad\"" : "") + "><td>" + esc(x.name) + "</td><td class=\"num\">" + fmt(x.per) + " " + esc(x.unit) +
        "</td><td class=\"num\">" + (n ? fmt(need) + " " + esc(x.unit) : "") + "</td><td class=\"num\">" + fmt(x.stock) + " " + esc(x.unit) + "</td></tr>";
    });
    box.innerHTML = "<p>" + t("Можно сделать:") + " <b class=\"" + (canMake > 0 ? "" : "bad") + "\">" +
      (isFinite(canMake) ? canMake : "—") + " " + t("шт") + "</b>" +
      (n > canMake ? " · <span class=\"bad\">" + t("на столько не хватает сырья") + "</span>" : "") + "</p>" +
      "<table><thead><tr><th>" + t("Сырьё") + "</th><th class=\"num\">" + t("На 1 шт") + "</th><th class=\"num\">" +
      t("Нужно") + "</th><th class=\"num\">" + t("На складе") + "</th></tr></thead><tbody>" + rows + "</tbody></table>";
  }

  product.addEventListener("change", update);
  if (qty) qty.addEventListener("input", update);
  if (window.django && django.jQuery) django.jQuery(product).on("select2:select", update);
  update();
});
