// Подписи в скриптах на узбекском. Ключ — русский текст, язык берём из <html lang>.
window.t = (function () {
  const uz = {
    " сум": " soʻm",
    "по": "",
    "сколько упаковок": "nechta qadoq",
    "сколько": "qancha",
    "цена за 1 упаковку": "1 qadoq narxi",
    "цена за 1": "narxi, 1",
    "→ на склад": "→ omborga",
    "в": "",
    "Итого:": "Jami:",
    "Оплачено сразу:": "Darhol toʻlandi:",
    "В долг:": "Qarzga:",
    "прошлая цена": "oldingi narx",
    "У продукта нет рецепта. Сначала заполните рецепт.": "Mahsulotning retsepti yoʻq. Avval retseptni toʻldiring.",
    "Можно сделать:": "Tayyorlash mumkin:",
    "шт": "dona",
    "на столько не хватает сырья": "bunchaga xomashyo yetmaydi",
    "Сырьё": "Xomashyo",
    "На 1 шт": "1 dona uchun",
    "Нужно": "Kerak",
    "На складе": "Omborda",
  };
  const on = (document.documentElement.lang || "").startsWith("uz");
  return (s) => (on && s in uz ? uz[s] : s);
})();
