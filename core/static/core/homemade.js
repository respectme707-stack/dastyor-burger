// Рецепт в карточке сырья нужен только для того, что делаем сами.
document.addEventListener("DOMContentLoaded", function () {
  const box = document.getElementById("id_homemade");
  const recipe = document.getElementById("ingredients-group");
  if (!box || !recipe) return;
  const sync = () => { recipe.style.display = box.checked ? "" : "none"; };
  box.addEventListener("change", sync);
  sync();
});
