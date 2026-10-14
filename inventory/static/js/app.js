(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-confirm]");
    if (trigger && !window.confirm(trigger.dataset.confirm)) {
      event.preventDefault();
    }
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-dismiss]");
    if (!button) return;
    var alert = button.closest(".alert");
    if (alert) alert.remove();
  });

  document.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-toggle-sidebar]");
    if (!toggle) return;
    var sidebar = document.getElementById("sidebar");
    if (sidebar) sidebar.classList.toggle("open");
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-table-export]");
    if (!button) return;
    var table = document.querySelector(button.dataset.tableExport);
    if (!table) return;

    var rows = [];
    table.querySelectorAll("tr").forEach(function (tr) {
      var cells = Array.prototype.map.call(
        tr.querySelectorAll("th, td"),
        function (cell) {
          return '"' + (cell.innerText || "").replace(/"/g, '""').trim() + '"';
        }
      );
      if (cells.length) rows.push(cells.join(","));
    });

    var blob = new Blob([rows.join("\n")], { type: "text/csv;charset=utf-8" });
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = (button.dataset.filename || "export") + ".csv";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(link.href);
  });

  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!select.matches("[data-product-select]")) return;

    var row = select.closest("tr") || select.closest(".line-item");
    if (!row) return;

    var option = select.options[select.selectedIndex];
    if (!option || !option.value) return;

    var priceInput = row.querySelector("[data-price-input]");
    if (priceInput && option.dataset.price) priceInput.value = option.dataset.price;

    var costInput = row.querySelector("[data-cost-input]");
    if (costInput && option.dataset.cost) costInput.value = option.dataset.cost;

    var stockNote = row.querySelector("[data-stock-note]");
    if (stockNote) {
      stockNote.textContent = option.dataset.stock
        ? "on hand: " + option.dataset.stock + " @ " + option.dataset.warehouseName
        : "";
    }

    recalcTotals(row.closest("table") || row.closest("form"));
  });

  function recalcTotals(scope) {
    if (!scope) return;
    var total = 0;
    scope.querySelectorAll("[data-line-row]").forEach(function (row) {
      var qty = parseFloat((row.querySelector("[name='quantity']") || {}).value) || 0;
      var price = parseFloat(
        ((row.querySelector("[data-price-input], [name='unit_cost']") || {})).value
      ) || 0;
      var cell = row.querySelector("[data-line-total]");
      if (cell) cell.textContent = qty * price ? (qty * price).toFixed(2) : "0.00";
      total += qty * price;
    });
    var grand = scope.querySelector("[data-grand-total]");
    if (grand) grand.textContent = total.toFixed(2);
  }

  document.addEventListener("input", function (event) {
    if (event.target.matches("[name='quantity'], [name='unit_cost'], [data-price-input]")) {
      recalcTotals(event.target.closest("table") || event.target.closest("form"));
    }
  });

  window.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[data-lines]").forEach(recalcTotals);
  });

  var idleTimer;
  function resetIdle() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(function () {
      fetch(window.location.pathname, { method: "HEAD", keepalive: true }).catch(function () {});
    }, 5 * 60 * 1000);
  }
  ["click", "keydown", "mousemove"].forEach(function (evt) {
    document.addEventListener(evt, resetIdle, { passive: true });
  });
})();