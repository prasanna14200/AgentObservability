(() => {
  const form = document.getElementById("scenario-form");
  const scenario = document.getElementById("scenario");
  const runMessage = document.getElementById("run-message");
  const detail = document.getElementById("execution-detail");
  const list = document.getElementById("execution-list");
  const refresh = document.getElementById("refresh-executions");

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function showEvidence(record) {
    detail.replaceChildren();
    const heading = element("h3", `${record.scenario_name} · ${record.status}`);
    detail.append(heading);
    detail.append(element("p", `Request ID: ${record.request_id}`));
    detail.append(element("p", `Started: ${record.started_at} · Completed: ${record.completed_at}`));

    const calls = element("section");
    calls.append(element("h4", "Tool execution timeline"));
    const callList = element("ol");
    for (const call of record.tool_calls) {
      const outcome = call.outcome ? JSON.stringify(call.outcome) : "No tool output (blocked before dispatch)";
      callList.append(element(
        "li",
        `${call.timestamp} · ${call.tool_name} · ${call.status.toUpperCase()} · ${call.reason_code} · ${outcome}`,
      ));
    }
    calls.append(callList);
    detail.append(calls);

    detail.append(element(
      "p",
      `Detection: ${record.detection.anomalous ? "ANOMALY" : "NO ANOMALY"} · ${record.detection.reason_codes.join(", ") || "No rule triggered"} · ${record.detection.explanation}`,
    ));
    detail.append(element(
      "p",
      `Safety decision: ${record.policy_decision.decision} · ${record.policy_decision.reason_code} · ${record.policy_decision.explanation}`,
    ));

    const audit = element("section");
    audit.append(element("h4", "Audit evidence"));
    const auditList = element("ol");
    for (const event of record.audit_events) {
      auditList.append(element(
        "li",
        `${event.timestamp} · ${event.event_type} · ${JSON.stringify(event.details)}`,
      ));
    }
    audit.append(auditList);
    detail.append(audit);
  }

  async function loadExecution(requestId) {
    const response = await fetch(`/evaluator/executions/${encodeURIComponent(requestId)}`, {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error("Saved execution could not be retrieved.");
    showEvidence(await response.json());
  }

  async function loadRecent() {
    const response = await fetch("/evaluator/executions", {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error("Recent executions could not be retrieved.");
    const executions = await response.json();
    list.replaceChildren();
    if (executions.length === 0) {
      list.append(element("li", "No saved scenario runs yet."));
      return;
    }
    for (const execution of executions) {
      const item = element("li");
      const button = element(
        "button",
        `${execution.scenario_name} · ${execution.decision || execution.status} · ${execution.request_id}`,
        "button subtle",
      );
      button.type = "button";
      button.addEventListener("click", async () => {
        try {
          await loadExecution(execution.request_id);
        } catch (error) {
          runMessage.textContent = error.message;
        }
      });
      item.append(button);
      list.append(item);
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    runMessage.textContent = "Running the bounded synthetic scenario…";
    try {
      const response = await fetch("/evaluator/executions", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ scenario: scenario.value }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Scenario execution failed.");
      showEvidence(result);
      runMessage.textContent = "Scenario completed and evidence was stored.";
      await loadRecent();
    } catch (error) {
      runMessage.textContent = error.message;
    }
  });

  refresh.addEventListener("click", async () => {
    try {
      await loadRecent();
      runMessage.textContent = "Saved executions refreshed.";
    } catch (error) {
      runMessage.textContent = error.message;
    }
  });

  loadRecent().catch((error) => {
    runMessage.textContent = error.message;
  });
})();
