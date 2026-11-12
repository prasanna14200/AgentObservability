(() => {
  const rows = document.getElementById("transaction-rows");
  const status = document.getElementById("transaction-load-status");
  const refresh = document.getElementById("refresh-transactions");
  if (!rows || !status || !refresh) return;

  const values = {
    count: document.getElementById("transaction-count"),
    pending: document.getElementById("transaction-pending"),
    completed: document.getElementById("transaction-completed"),
    failed: document.getElementById("transaction-failed"),
    totals: document.getElementById("transaction-totals"),
  };

  const appendCell = (row, value) => {
    const cell = document.createElement("td");
    cell.textContent = value || "—";
    row.appendChild(cell);
  };

  const render = (payload) => {
    const counts = payload.summary.status_counts;
    values.count.textContent = String(payload.summary.transaction_count);
    values.pending.textContent = String(counts.PENDING);
    values.completed.textContent = String(counts.COMPLETED);
    values.failed.textContent = String(counts.FAILED + counts.RECONCILIATION_ERROR);
    values.totals.textContent = Object.entries(payload.summary.totals_by_currency)
      .map(([currency, amount]) => `${currency} ${amount}`)
      .join(" · ") || "No data";

    rows.replaceChildren();
    if (payload.transactions.length === 0) {
      const empty = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 8;
      cell.className = "muted";
      cell.textContent = "No persisted transactions.";
      empty.appendChild(cell);
      rows.appendChild(empty);
    } else {
      for (const transaction of payload.transactions) {
        const row = document.createElement("tr");
        appendCell(row, transaction.operation_id);
        appendCell(row, transaction.source === "manual_sandbox_evidence"
          ? "Manual Sandbox evidence"
          : "Gateway operation");
        appendCell(row, transaction.provider_mode);
        appendCell(row, `${transaction.currency} ${transaction.amount}`);
        appendCell(row, transaction.status);
        const providerState = [
          transaction.provider_order_status && `order ${transaction.provider_order_status}`,
          transaction.provider_capture_status && `capture ${transaction.provider_capture_status}`,
        ].filter(Boolean).join(" · ");
        appendCell(row, providerState);
        appendCell(row, [transaction.provider_order_id, transaction.provider_capture_id]
          .filter(Boolean)
          .join(" / "));
        const timestamp = new Date(transaction.updated_at);
        appendCell(row, Number.isNaN(timestamp.getTime())
          ? transaction.updated_at
          : timestamp.toLocaleString());
        rows.appendChild(row);
      }
    }
    status.textContent = `Updated ${new Date(payload.updated_at).toLocaleTimeString()}.`;
  };

  const load = async () => {
    refresh.disabled = true;
    status.textContent = "Refreshing persisted transactions…";
    try {
      const response = await fetch("/dashboard/api/transactions", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error("transaction data unavailable");
      render(await response.json());
    } catch {
      status.textContent = "Transaction data unavailable. Refresh to retry.";
    } finally {
      refresh.disabled = false;
    }
  };

  refresh.addEventListener("click", load);
  load();
  window.setInterval(load, 15000);
})();
