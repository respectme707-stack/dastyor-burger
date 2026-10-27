// Районы, магазины и машина нужны только агентам. Остальным их не показываем.
document.addEventListener("DOMContentLoaded", function () {
  const role = document.getElementById("id_role");
  if (!role) return;
  const sellers = ["agent", "van"];
  const rows = [".field-districts", ".field-shops"]
    .map((sel) => document.querySelector(sel)).filter(Boolean);
  const sync = () => {
    const show = sellers.includes(role.value);
    rows.forEach((row) => { row.style.display = show ? "" : "none"; });
  };
  role.addEventListener("change", sync);
  sync();
});
