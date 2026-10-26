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
  };
  const on = (document.documentElement.lang || "").startsWith("uz");
  return (s) => (on && s in uz ? uz[s] : s);
})();
