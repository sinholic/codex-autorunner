/**
 * Jira Import - fetch a Jira issue (splitting epics into child tickets) and
 * create them as CAR tickets.
 */
import { api, flash } from "./utils.js";
import { openTicketEditor } from "./ticketEditor.js";
import { publish } from "./bus.js";

type JiraIssueSummary = {
  key: string;
  summary: string;
  is_epic: boolean;
  description_text: string;
  url: string;
};

type JiraPreviewResponse = {
  issue: JiraIssueSummary;
  children: JiraIssueSummary[];
};

type JiraApplyResult = {
  key: string;
  created_path: string;
  filename: string;
  index: number;
};

type JiraApplyResponse = {
  results: JiraApplyResult[];
};

type JiraState = {
  isOpen: boolean;
  loading: boolean;
  preview: JiraPreviewResponse | null;
  selectedKeys: Set<string>;
};

const state: JiraState = {
  isOpen: false,
  loading: false,
  preview: null,
  selectedKeys: new Set(),
};

function els() {
  return {
    modal: document.getElementById("ticket-jira-modal"),
    keyInput: document.getElementById("ticket-jira-key") as HTMLInputElement | null,
    fetchBtn: document.getElementById("ticket-jira-fetch") as HTMLButtonElement | null,
    preview: document.getElementById("ticket-jira-preview"),
    error: document.getElementById("ticket-jira-error"),
    status: document.getElementById("ticket-jira-status"),
    agentSelect: document.getElementById("ticket-jira-agent") as HTMLSelectElement | null,
    cancelBtn: document.getElementById("ticket-jira-cancel") as HTMLButtonElement | null,
    applyBtn: document.getElementById("ticket-jira-apply") as HTMLButtonElement | null,
    closeBtn: document.getElementById("ticket-jira-close") as HTMLButtonElement | null,
    fromJiraBtn: document.getElementById("ticket-new-from-jira") as HTMLButtonElement | null,
    overflowJira: document.getElementById("ticket-overflow-jira") as HTMLButtonElement | null,
  };
}

function showError(message: string): void {
  const { error } = els();
  if (!error) return;
  error.textContent = message;
  error.classList.remove("hidden");
}

function hideError(): void {
  const { error } = els();
  if (!error) return;
  error.textContent = "";
  error.classList.add("hidden");
}

function setLoading(loading: boolean): void {
  state.loading = loading;
  const { fetchBtn, applyBtn, status } = els();
  if (fetchBtn) fetchBtn.disabled = loading;
  if (applyBtn) applyBtn.disabled = loading || state.selectedKeys.size === 0;
  if (status) status.textContent = loading ? "Loading..." : "";
}

function renderIssueRow(issue: JiraIssueSummary, opts: { checkable: boolean }): HTMLElement {
  const row = document.createElement("label");
  row.className = "ticket-jira-issue-row";

  if (opts.checkable) {
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.selectedKeys.has(issue.key);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        state.selectedKeys.add(issue.key);
      } else {
        state.selectedKeys.delete(issue.key);
      }
      const { applyBtn } = els();
      if (applyBtn) applyBtn.disabled = state.loading || state.selectedKeys.size === 0;
    });
    row.appendChild(checkbox);
  }

  const text = document.createElement("span");
  text.textContent = `${issue.key}: ${issue.summary}`;
  row.appendChild(text);
  return row;
}

function renderPreview(): void {
  const { preview, applyBtn } = els();
  if (!preview) return;
  preview.innerHTML = "";

  if (!state.preview) {
    preview.textContent = "Enter a Jira issue key and fetch to preview.";
    if (applyBtn) applyBtn.disabled = true;
    return;
  }

  const { issue, children } = state.preview;
  preview.appendChild(renderIssueRow(issue, { checkable: true }));

  if (issue.is_epic) {
    const label = document.createElement("div");
    label.className = "muted small";
    label.textContent = children.length
      ? `Epic children (${children.length}):`
      : "Epic has no linked children.";
    preview.appendChild(label);
    for (const child of children) {
      preview.appendChild(renderIssueRow(child, { checkable: true }));
    }
  }

  if (applyBtn) applyBtn.disabled = state.selectedKeys.size === 0;
}

async function fetchPreview(): Promise<void> {
  const { keyInput } = els();
  const key = keyInput?.value.trim();
  if (!key) {
    showError("Enter a Jira issue key (e.g. ZEL-964).");
    return;
  }

  setLoading(true);
  hideError();
  try {
    const data = (await api("/api/jira/preview", {
      method: "POST",
      body: { key },
    })) as JiraPreviewResponse;

    state.preview = data;
    state.selectedKeys = new Set([data.issue.key, ...data.children.map((c) => c.key)]);
    renderPreview();
  } catch (err) {
    const error = err as { message?: string; detail?: { message?: string } };
    showError(error.detail?.message || error.message || "Failed to fetch Jira issue");
    state.preview = null;
    renderPreview();
  } finally {
    setLoading(false);
  }
}

async function applyImport(): Promise<void> {
  const { agentSelect } = els();
  if (state.selectedKeys.size === 0) return;

  setLoading(true);
  hideError();
  try {
    const body: Record<string, unknown> = { keys: Array.from(state.selectedKeys) };
    const agent = agentSelect?.value;
    if (agent) body.agent = agent;

    const result = (await api("/api/jira/apply", {
      method: "POST",
      body,
    })) as JiraApplyResponse;

    closeJiraModal();
    flash(`Imported ${result.results.length} ticket(s) from Jira`, "success");
    publish("tickets:updated", {});

    const first = result.results[0];
    if (first) {
      try {
        const ticketData = await api(`/api/flows/ticket_flow/tickets/${first.index}`);
        openTicketEditor(ticketData as Parameters<typeof openTicketEditor>[0]);
      } catch {
        // Best effort only; ticket list will refresh on its own.
      }
    }
  } catch (err) {
    const error = err as { message?: string; detail?: { message?: string } };
    showError(error.detail?.message || error.message || "Failed to import from Jira");
  } finally {
    setLoading(false);
  }
}

export function openJiraModal(): void {
  const { modal, keyInput } = els();
  if (!modal) return;

  state.isOpen = true;
  state.preview = null;
  state.selectedKeys = new Set();
  if (keyInput) keyInput.value = "";
  hideError();
  renderPreview();
  modal.classList.remove("hidden");
  keyInput?.focus();
}

export function closeJiraModal(): void {
  const { modal } = els();
  if (!modal) return;
  state.isOpen = false;
  modal.classList.add("hidden");
}

export function initJiraImport(): void {
  const {
    modal,
    keyInput,
    fetchBtn,
    cancelBtn,
    applyBtn,
    closeBtn,
    fromJiraBtn,
    overflowJira,
  } = els();

  if (!modal) return;
  if (modal.dataset.jiraInitialized === "1") return;
  modal.dataset.jiraInitialized = "1";

  // Jira import doesn't depend on templates being enabled, so make sure the
  // split-button dropdown toggle is reachable even if templates are off.
  document.getElementById("ticket-new-dropdown-toggle")?.classList.remove("hidden");

  fromJiraBtn?.addEventListener("click", () => openJiraModal());
  overflowJira?.addEventListener("click", () => {
    document.getElementById("ticket-overflow-dropdown")?.classList.add("hidden");
    openJiraModal();
  });

  closeBtn?.addEventListener("click", closeJiraModal);
  cancelBtn?.addEventListener("click", closeJiraModal);
  fetchBtn?.addEventListener("click", () => void fetchPreview());
  applyBtn?.addEventListener("click", () => void applyImport());

  keyInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      void fetchPreview();
    }
  });

  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeJiraModal();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.isOpen) closeJiraModal();
  });
}
