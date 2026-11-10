import { FormEvent, useEffect, useMemo, useState } from "react";
import ROBOT_ATLAS_BASE64 from "./assets/robot-atlas.b64?raw";
import {
  Activity, AlertTriangle, ArrowRight, CheckCircle2, ChevronDown, CircleHelp,
  Code2, FileText, LayoutDashboard, LoaderCircle, LockKeyhole,
  MessageSquare, Plus, Radio, Search, Send, Settings2, ShieldCheck,
  Sparkles, Users, Workflow, Zap,
} from "lucide-react";

type Agent = {
  id: string; name: string; role: string; personality: string;
  status: string; task: string; emoji: string;
};
type Subtask = { id: string; title: string; owner: string; status: string; output?: string | null };
type EventItem = { id: string; timestamp: string; actor: string; kind: string; message: string };
type MemoryItem = { id: string; timestamp: string; source_agent: string; title: string; content: string };
type AgentMessage = { id: string; timestamp: string; from_agent: string; to_agent: string; kind: string; content: string };
type QaFeedback = { decision?: string; summary?: string; issues?: string[]; revision_instructions?: string; affected_agents?: string[] };
type ProviderInfo = {
  name: string; model: string; protocol: string;
  max_requests_per_hour: number | null; requests_this_hour: number;
  max_requests_per_minute?: number; requests_this_minute?: number;
  min_request_interval_seconds?: number; cooldown_seconds?: number;
  cooldown_info?: { kind?: string; label?: string; quota_id?: string | null; quota_metric?: string | null; reset_at?: string | null };
  ready?: boolean; cooling_down: boolean;
};
type ProviderTest = { name: string; protocol: string; model: string; status: "connected" | "failed" | "skipped"; latency_ms: number | null; message: string };
type ResearchSource = { source_id: string; query: string; title: string; url: string; snippet: string; score?: number | null; published_date?: string | null; };
type TaskPlan = {
  task_id: string; instruction: string; title: string; category: string;
  execution_mode: "demo" | "provider_planned"; notice: string;
  agents: Agent[]; subtasks: Subtask[];
  execution_status: string; approval_status: string; qa_status: string;
  qa_feedback: string | null; team_discussion: string | null; result_markdown: string | null;
  shared_memory: MemoryItem[]; messages: AgentMessage[]; events: EventItem[];
  research_sources: ResearchSource[]; research_status: string; research_notice: string | null; research_credits: number;
  created_at: string; updated_at: string;
};

const ROBOT_ATLAS_URL = "data:image/webp;base64," + ROBOT_ATLAS_BASE64.trim();
const API_BASE = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");
const terminalStatuses = ["completed", "revision_required", "failed", "rejected"];

const starterPrompts = [
  { icon: Code2, label: "Build an application", prompt: "Design and implement a full-stack task management application with authentication, a dashboard, and automated tests." },
  { icon: Search, label: "Research a topic", prompt: "Research the current state of multi-agent AI systems, compare leading approaches, identify evidence gaps, and prepare a structured report." },
  { icon: Users, label: "Plan an event", prompt: "Create a complete plan for a 200-person technology hackathon, including budget, venue, schedule, logistics, marketing, and risks." },
];

function robotVariant(agent?: Agent, index = 0): "scout" | "forge" | "orbit" | "sentinel" {
  const role = [agent?.name, agent?.role, agent?.task].filter(Boolean).join(" ").toLowerCase();
  const has = (...terms: string[]) => terms.some((term) => role.includes(term));
  if (has("qa", "quality", "test", "review", "verif", "sentinel")) return "sentinel";
  if (has("research", "analyst", "data", "scout", "investigat")) return "scout";
  if (has("engineer", "developer", "code", "builder", "architect", "forge", "programmer")) return "forge";
  if (has("director", "project", "planner", "manager", "coordinator", "operations", "orbit")) return "orbit";
  return (["scout", "forge", "orbit", "sentinel"] as const)[index % 4];
}

function Robot({ agent, index }: { agent?: Agent; index: number }) {
  const state = agent?.status || "idle";
  const variant = robotVariant(agent, index);
  return <div className={"robot robot-" + (index % 4) + " robot-state-" + state} title={agent ? agent.name + " · " + agent.role + " · " + state : "Workforce " + variant + " robot"}>
    <div className={"robot-figure bot-" + variant} style={{ backgroundImage: `url("${ROBOT_ATLAS_URL}")` }} role="img" aria-label={agent?.name || variant + " specialist robot"} />
    <span className="robot-shadow" />
  </div>;
}

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    awaiting_approval: "Awaiting your approval",
    queued: "Queued for execution",
    running: "Team working",
    reviewing: "Independent QA review",
    completed: "QA approved",
    revision_required: "Human review required",
    failed: "Execution stopped",
    rejected: "Plan rejected",
  };
  return labels[status] || status.replaceAll("_", " ");
}

function qaStatusLabel(status: string) {
  const labels: Record<string, string> = {
    pending_execution: "Not run yet",
    not_run: "Not run",
    in_progress: "Review in progress",
    passed: "Passed",
    rejected: "Rejected by QA",
    failed: "Review failed",
  };
  return labels[status] || status.replaceAll("_", " ");
}

function formatWait(seconds: number) {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${remainder}s`;
  return `${remainder}s`;
}

function subtaskStatusLabel(status: string) {
  const labels: Record<string, string> = {
    planned: "Planned",
    in_progress: "In progress",
    completed: "Completed",
    failed: "Failed",
    blocked_by_provider: "Provider blocked",
    revision_required: "Revision required",
    awaiting_human_signoff: "Awaiting human sign-off",
    blocked_by_qa: "Blocked by QA",
    cancelled: "Cancelled",
  };
  return labels[status] || status.replaceAll("_", " ");
}

function visibleAgentStatus(agent: Agent, plan: TaskPlan) {
  if (agent.status === "blocked_by_provider") return agent.status;
  // Interpret legacy V5.2 snapshots: agents rejected before a model response
  // because the shared provider was cooling down are blocked, not failed.
  if (agent.status === "failed" && plan.events.some((event) =>
    event.actor === agent.name && event.kind === "agent_failed" &&
    /ProviderUnavailableError|provider is unavailable|cooling down|request budget|daily quota|rate limit/i.test(event.message)
  )) return "blocked_by_provider";
  return agent.status;
}

function visibleSubtaskStatus(subtask: Subtask, plan: TaskPlan) {
  if (!["in_progress", "failed", "blocked_by_provider"].includes(subtask.status)) return subtask.status;
  const normalize = (value: string) => value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const owner = normalize(subtask.owner);
  const agent = plan.agents.find((item) => [item.name, item.role, item.id].some((alias) => normalize(alias) === owner));
  if (!agent) return subtask.status;
  const hasOwnOutput = plan.shared_memory.some((item) => item.source_agent === agent.name && item.content.trim().length > 0);
  const hasRecoveredOutput = plan.shared_memory.some((item) => item.title.toLowerCase().includes(`recovered work for ${agent.name.toLowerCase()}`));
  const effectiveStatus = visibleAgentStatus(agent, plan);
  if (effectiveStatus === "completed" && hasOwnOutput) return "completed";
  if (effectiveStatus === "reassigned" && hasRecoveredOutput) return "completed";
  if (effectiveStatus === "failed") return "failed";
  if (effectiveStatus === "blocked_by_provider") return "blocked_by_provider";
  return subtask.status;
}

function timestamp(value: string) {
  try { return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  catch { return ""; }
}

export default function App() {
  const [view, setView] = useState<"chat" | "workspace" | "settings">("chat");
  const [prompt, setPrompt] = useState("");
  const [plan, setPlan] = useState<TaskPlan | null>(null);
  const [restoringTask, setRestoringTask] = useState(true);
  const [restoreHadError, setRestoreHadError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [approvalLoading, setApprovalLoading] = useState(false);
  const [retryLoading, setRetryLoading] = useState(false);
  const [error, setError] = useState("");
  const [providerConfigured, setProviderConfigured] = useState<boolean | null>(null);
  const [webResearchConfigured, setWebResearchConfigured] = useState(false);
  const [storageBackend, setStorageBackend] = useState("unknown");
  const [durableStorageConfigured, setDurableStorageConfigured] = useState(false);
  const [taskQueueBackend, setTaskQueueBackend] = useState("background_tasks_preview");
  const [durableQueueConfigured, setDurableQueueConfigured] = useState(false);
  const [fallbackConfigured, setFallbackConfigured] = useState(false);
  const [providerDetails, setProviderDetails] = useState<ProviderInfo[]>([]);
  const [configurationError, setConfigurationError] = useState<string | null>(null);
  const [providerTests, setProviderTests] = useState<ProviderTest[]>([]);
  const [providerTestedAt, setProviderTestedAt] = useState<string | null>(null);
  const [testingProviders, setTestingProviders] = useState(false);
  const [providerTestError, setProviderTestError] = useState("");

  useEffect(() => {
    fetch(API_BASE + "/api/config/status")
      .then((r) => r.ok ? r.json() : Promise.reject(new Error("Backend unavailable")))
      .then((data) => {
        setProviderConfigured(Boolean(data.provider_configured));
        setWebResearchConfigured(Boolean(data.web_research_configured));
        setStorageBackend(String(data.storage_backend || "memory"));
        setDurableStorageConfigured(Boolean(data.durable_storage_configured));
        setTaskQueueBackend(String(data.task_queue_backend || "background_tasks_preview"));
        setDurableQueueConfigured(Boolean(data.durable_queue_configured));
        setFallbackConfigured(Boolean(data.fallback_configured));
        setProviderDetails(Array.isArray(data.providers) ? data.providers as ProviderInfo[] : []);
        setConfigurationError(data.configuration_error ? String(data.configuration_error) : null);
      })
      .catch(() => setProviderConfigured(null));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const urlTaskId = new URLSearchParams(window.location.search).get("task_id");
    const storedTaskId = window.localStorage.getItem("workforce.ai.currentTaskId");
    const taskIdToRestore = urlTaskId || storedTaskId;
    if (!taskIdToRestore) {
      setRestoringTask(false);
      return;
    }

    fetch(API_BASE + "/api/tasks/" + encodeURIComponent(taskIdToRestore))
      .then((response) => {
        if (response.status === 404) {
          window.localStorage.removeItem("workforce.ai.currentTaskId");
          const url = new URL(window.location.href);
          url.searchParams.delete("task_id");
          window.history.replaceState(null, "", url.toString());
          throw new Error("The saved mission could not be found.");
        }
        if (!response.ok) throw new Error("Mission restore failed (" + response.status + ")");
        return response.json() as Promise<TaskPlan>;
      })
      .then((savedTask) => {
        if (cancelled) return;
        setPlan(savedTask);
        setView("workspace");
      })
      .catch((restoreError) => {
        if (cancelled) return;
        setRestoreHadError(true);
        setError(restoreError instanceof Error ? restoreError.message : "Unable to restore the saved mission.");
      })
      .finally(() => {
        if (!cancelled) setRestoringTask(false);
      });

    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (restoringTask || restoreHadError) return;
    const url = new URL(window.location.href);
    if (plan?.task_id) {
      window.localStorage.setItem("workforce.ai.currentTaskId", plan.task_id);
      url.searchParams.set("task_id", plan.task_id);
    } else {
      window.localStorage.removeItem("workforce.ai.currentTaskId");
      url.searchParams.delete("task_id");
    }
    window.history.replaceState(null, "", url.toString());
  }, [restoringTask, restoreHadError, plan?.task_id]);

  const taskId = plan?.task_id;
  const executionStatus = plan?.execution_status;
  useEffect(() => {
    if (!taskId || !executionStatus || terminalStatuses.includes(executionStatus) || executionStatus === "awaiting_approval") return;
    let cancelled = false;
    let fallbackTimer: number | undefined;
    const poll = async () => {
      try {
        const response = await fetch(API_BASE + "/api/tasks/" + taskId);
        if (!response.ok) return;
        const fresh = await response.json() as TaskPlan;
        if (!cancelled) setPlan((current) => current?.task_id === fresh.task_id ? fresh : current);
      } catch {
        // Keep the last known task snapshot visible while the API is temporarily unavailable.
      }
    };
    const startPollingFallback = () => {
      if (fallbackTimer !== undefined || cancelled) return;
      void poll();
      fallbackTimer = window.setInterval(poll, 2500);
    };
    if (typeof window.EventSource === "function") {
      const stream = new EventSource(API_BASE + "/api/tasks/" + taskId + "/stream");
      stream.addEventListener("task_update", () => { void poll(); });
      stream.onerror = () => {
        stream.close();
        startPollingFallback();
      };
      return () => {
        cancelled = true;
        stream.close();
        if (fallbackTimer !== undefined) window.clearInterval(fallbackTimer);
      };
    }
    startPollingFallback();
    return () => {
      cancelled = true;
      if (fallbackTimer !== undefined) window.clearInterval(fallbackTimer);
    };
  }, [taskId, executionStatus]);

  function downloadMarkdownFile(filename: string, content: string) {
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }

  function downloadSpecialistOutput(item: MemoryItem) {
    if (!plan) return;
    const base = `${plan.title}-${item.source_agent}-${item.title}`
      .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    downloadMarkdownFile(`${base || "specialist-output"}.md`, `# ${item.title}\n\n**Mission:** ${plan.title}\n**Specialist:** ${item.source_agent}\n\n${item.content}`);
  }

  function downloadAllSpecialistOutputs() {
    if (!plan || missionOutputs.length === 0) return;
    const content = missionOutputs
      .slice().reverse()
      .map((item) => `# ${item.title}\n\n**Specialist:** ${item.source_agent}\n**Recorded:** ${new Date(item.timestamp).toLocaleString()}\n\n${item.content}`)
      .join("\n\n---\n\n");
    const filename = `${plan.title}-specialist-outputs`.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") + ".md";
    downloadMarkdownFile(filename, `# ${plan.title} — Specialist Outputs\n\n${content}`);
  }

  function downloadDeliverableMarkdown() {
    if (!plan?.result_markdown) return;
    const content = "# " + plan.title + "\n\n" + plan.result_markdown;
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = (plan.title || "workforce-deliverable").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") + ".md";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }

  function printDeliverableAsPdf() {
    if (!plan?.result_markdown) return;
    window.print();
  }
  async function testProviderConnections() {
    if (testingProviders || providerDetails.length === 0) return;
    setTestingProviders(true);
    setProviderTestError("");
    try {
      const response = await fetch(API_BASE + "/api/providers/test", { method: "POST" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "Provider test failed (" + response.status + ")");
      setProviderTests(Array.isArray(payload.results) ? payload.results as ProviderTest[] : []);
      setProviderTestedAt(payload.tested_at || new Date().toISOString());
    } catch (err) {
      setProviderTestError(err instanceof Error ? err.message : "Unable to test providers.");
    } finally {
      setTestingProviders(false);
    }
  }
  async function submitTask(event?: FormEvent) {
    event?.preventDefault();
    if (!prompt.trim() || loading) return;
    setLoading(true);
    setError("");
    try {
      const response = await fetch(API_BASE + "/api/tasks/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instruction: prompt.trim() }),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail.detail || "Task planning failed (" + response.status + ")");
      }
      const result = await response.json() as TaskPlan;
      setPlan(result);
      setView("workspace");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to reach the workforce backend.");
    } finally {
      setLoading(false);
    }
  }

  async function decideApproval(decision: "approve" | "reject") {
    if (!plan || approvalLoading) return;
    setApprovalLoading(true);
    setError("");
    try {
      const response = await fetch(API_BASE + "/api/tasks/" + plan.task_id + "/approval", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, reason: decision === "reject" ? "The user rejected this team plan." : undefined }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "Approval request failed (" + response.status + ")");
      setPlan(payload as TaskPlan);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to submit the approval decision.");
    } finally {
      setApprovalLoading(false);
    }
  }

  async function retryFailedWorkstreams() {
    if (!plan || retryLoading) return;
    setRetryLoading(true);
    setError("");
    try {
      const response = await fetch(API_BASE + "/api/tasks/" + plan.task_id + "/retry", { method: "POST" });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || "Retry request failed (" + response.status + ")");
      setPlan(payload as TaskPlan);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to retry unfinished workstreams.");
    } finally {
      setRetryLoading(false);
    }
  }

  const completedAgentCount = plan?.agents.filter((agent) => ["completed", "reassigned"].includes(visibleAgentStatus(agent, plan))).length ?? 0;
  const failedAgentCount = plan?.agents.filter((agent) => visibleAgentStatus(agent, plan) === "failed").length ?? 0;
  const blockedAgentCount = plan?.agents.filter((agent) => visibleAgentStatus(agent, plan) === "blocked_by_provider").length ?? 0;
  const runningAgentCount = plan?.agents.filter((agent) => visibleAgentStatus(agent, plan) === "running").length ?? 0;
  const waitingAgentCount = Math.max(0, (plan?.agents.length ?? 0) - completedAgentCount - failedAgentCount - blockedAgentCount - runningAgentCount);
  const statusLabelText = providerConfigured === true ? "Live provider connected" : providerConfigured === false ? "Demo planning mode" : "Backend not connected";
  const taskSummary = useMemo(() => plan?.subtasks.length ? plan.subtasks.length + " workstreams" : "Ready for your first task", [plan]);
  const qaFeedback = useMemo(() => {
    if (!plan?.qa_feedback) return null;
    try { return JSON.parse(plan.qa_feedback) as QaFeedback; } catch { return { summary: plan.qa_feedback }; }
  }, [plan?.qa_feedback]);
  const qaHasStarted = plan?.events.some((event) => event.kind === "qa_started") ?? false;
  // Older runs incorrectly stored "rejected" when an upstream worker failed before QA.
  const qaDisplayStatus = !plan ? "pending_execution" : (
    (plan.qa_status === "pending_execution" && plan.execution_status === "failed") ||
    (plan.qa_status === "rejected" && !plan.qa_feedback && !qaHasStarted)
      ? "not_run"
      : plan.qa_status
  );
  const missionOutputs = plan?.shared_memory.filter((item) => item.content.trim().length > 0) ?? [];
  const failureEvents = plan?.events.filter((event) => ["agent_failed", "agent_deferred", "recovery_failed", "recovery_deferred", "execution_failed"].includes(event.kind)) ?? [];
  const failureDetailsCaptured = failureEvents.some((event) => /\([A-Za-z0-9_]+\):\s/.test(event.message));

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark brand-robot-mark"><div className="robot-figure bot-orbit" style={{ backgroundImage: `url("${ROBOT_ATLAS_URL}")` }} role="img" aria-label="Orbit planning robot" /></div>
          <div><strong>workforce<span>.ai</span></strong><small>YOUR AI OPERATIONS TEAM</small></div>
        </div>
        <button className={"nav-item " + (view === "chat" ? "selected" : "")} onClick={() => setView("chat")}><MessageSquare size={18} /> Command Center</button>
        <button className={"nav-item " + (view === "workspace" ? "selected" : "")} onClick={() => setView("workspace")}><LayoutDashboard size={18} /> Workforce HQ</button>
        <button className={"nav-item " + (view === "settings" ? "selected" : "")} onClick={() => setView("settings")}><Settings2 size={18} /> Provider settings</button>
        <div className="sidebar-bottom">
          <div className="sidebar-status"><span className={providerConfigured === null ? "dot amber" : "dot"} /> <span>{statusLabelText}</span></div>
          <div className="user-chip"><div className="avatar">A</div><div><b>Workspace</b><small>Login-free preview</small></div><ChevronDown size={15} /></div>
        </div>
      </aside>

      <section className="main-area">
        <header className="topbar">
          <div className="breadcrumb">Workspace <span>/</span> <b>{view === "chat" ? "Command Center" : view === "workspace" ? "Workforce HQ" : "Provider settings"}</b></div>
          <div className="topbar-right"><span className="secure-label"><ShieldCheck size={15} /> Approval-based autonomy</span><button className="icon-button" aria-label="Help"><CircleHelp size={18} /></button></div>
        </header>

        {view === "chat" && (
          <div className="command-view">
            <div className="hero-copy">
              <div className="eyebrow"><Sparkles size={14} /> THE WORKFORCE IS READY</div>
              <h1>Big tasks need<br /><span>a whole team.</span></h1>
              <p>Give your workforce a goal. It will assemble specialists, collaborate on the work, and submit the result to an independent QA reviewer.</p>
              <div className="hero-pills"><span><Users size={14} /> Dynamic teams</span><span><ShieldCheck size={14} /> You approve execution</span><span><Zap size={14} /> Parallel specialists</span></div>
            </div>

            <div className="robot-stage">
              <div className="stage-glow" />
              <div className="stage-caption"><span className="live-dot" /> YOUR TEAM IS STANDING BY</div>
              <div className="robot-line">{Array.from({ length: 7 }, (_, i) => <Robot key={i} index={i} />)}</div>
              <div className="stage-floor" />
            </div>

            <div className="composer-wrap">
              <form className="composer" onSubmit={submitTask}>
                <div className="composer-top"><span><Sparkles size={15} /> Describe the outcome you want</span><span className="shortcut">ENTER TO PLAN</span></div>
                <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Build a product, research a topic, plan an event… give your team a mission." rows={3} />
                <div className="composer-bottom"><span className="composer-hint"><Plus size={16} /> File uploads arrive in a later milestone</span><button className="send-button" type="submit" disabled={!prompt.trim() || loading}>{loading ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />} {loading ? "Assembling..." : "Assemble my team"}</button></div>
              </form>
              {error && <div className="error-banner">{error}</div>}
              <div className="starter-label">OR START WITH A MISSION</div>
              <div className="starter-grid">{starterPrompts.map((item) => <button key={item.label} className="starter-card" onClick={() => setPrompt(item.prompt)}><item.icon size={17} /><span>{item.label}</span><ArrowRight size={15} /></button>)}</div>
            </div>
          </div>
        )}

        {view === "workspace" && (
          <div className="workspace-view">
            <div className="workspace-heading"><div><div className="eyebrow"><Activity size={14} /> WORKFORCE OPERATIONS</div><h1>Workforce <span>HQ</span></h1><p>Watch your specialists work, share findings, and pass independent quality review.</p></div><button className="secondary-button" onClick={() => { setPlan(null); setPrompt(""); setError(""); setView("chat"); }}><Plus size={16} /> New mission</button></div>
            {!plan ? <div className="empty-workspace"><div className="empty-icon"><Workflow size={30} /></div><h2>No active mission yet</h2><p>Submit a task in Command Center and your team will appear here.</p><button className="send-button" onClick={() => setView("chat")}>Go to Command Center <ArrowRight size={16} /></button></div> : <>
              <div className="mission-card"><div><span className="card-kicker">CURRENT MISSION · {plan.category.toUpperCase()}</span><h2>{plan.title}</h2><p>{taskSummary} · QA: {qaStatusLabel(qaDisplayStatus)}</p></div><span className={"mode-tag " + plan.execution_mode}>{plan.execution_mode === "demo" ? "DEMO PLAN" : "LIVE PROVIDER PLAN"}</span></div>
              <div className="notice-banner"><ShieldCheck size={17} /><span>{plan.notice}</span></div>
              {plan.category === "research" && (
                <section className="panel research-panel">
                  <div className="panel-heading"><h3><Search size={17} /> Live research sources</h3><span>{plan.research_sources.length} sources · {plan.research_credits} search credits</span></div>
                  <div className="research-status-row"><b className={"research-status status-" + plan.research_status}>{plan.research_status.replaceAll("_", " ")}</b><span>{plan.research_notice || "Live search will start after you approve this plan."}</span></div>
                  {plan.research_sources.length === 0 ? <p className="empty-feed">No live web sources have been collected. Add TAVILY_API_KEY in the backend hosting environment to enable capped source search for approved research tasks.</p> : <div className="research-source-list">{plan.research_sources.map((source) => <article className="research-source" key={source.source_id}><div className="research-source-meta"><b>{source.source_id}</b><span>{source.query}</span></div><a href={source.url} target="_blank" rel="noreferrer">{source.title} <ArrowRight size={12} /></a><p>{source.snippet}</p><small>{source.published_date ? "Published: " + source.published_date + " · " : ""}{source.score === null || source.score === undefined ? "" : "Search relevance: " + source.score.toFixed(2) + " · "}{new URL(source.url).hostname}</small></article>)}</div>}
                </section>
              )}

              <div className="execution-banner">
                <div className="execution-status-icon">{plan.execution_status === "completed" ? <CheckCircle2 size={20} /> : plan.execution_status === "failed" ? <LockKeyhole size={20} /> : <Activity size={20} />}</div>
                <div className="execution-status-copy"><b>{statusLabel(plan.execution_status)}</b><small>{plan.execution_status === "awaiting_approval" ? "Review the team and approve before any agent begins work." : plan.execution_status === "queued" || plan.execution_status === "running" || plan.execution_status === "reviewing" ? "The workspace refreshes automatically as the team reports progress." : plan.execution_status === "completed" ? "Independent AI QA approved the deliverable. Check workstreams for any human sign-off still pending." : plan.execution_status === "revision_required" ? "QA did not approve the result after the revision round. Human review is needed." : plan.execution_status === "rejected" ? "You rejected the proposed team plan." : "The run stopped safely. Review the diagnostics and any partial outputs below."}</small></div>
                <span className={"execution-pill " + plan.execution_status}>{plan.execution_status.replaceAll("_", " ")}</span>
              </div>

              <section className="panel mission-output-panel" aria-label="Mission output window">
                <div className="panel-heading output-panel-heading">
                  <h3><FileText size={17} /> Mission output window</h3>
                  <div className="output-header-actions">
                    <span>{missionOutputs.length} specialist document{missionOutputs.length === 1 ? "" : "s"}</span>
                    {missionOutputs.length > 0 && <button className="secondary-button" onClick={downloadAllSpecialistOutputs}>Download all .md</button>}
                  </div>
                </div>
                <div className="output-window-intro">
                  {missionOutputs.length > 0
                    ? <><b>{missionOutputs.length} output{missionOutputs.length === 1 ? " is" : "s are"} available.</b> These are individual specialist findings, not yet a single integrated or QA-approved deliverable.</>
                    : <>No specialist output has been recorded yet. Findings will appear here as specialists report their work.</>}
                  {plan.result_markdown && <p>The integrated deliverable is also available in its own section below.</p>}
                </div>
                {missionOutputs.length > 0 ? (
                  <div className="output-document-list">
                    {missionOutputs.slice().reverse().map((item, index) => (
                      <details className="output-document" key={item.id} open={index === 0}>
                        <summary>
                          <div className="output-document-title"><b>{item.title}</b><span>{item.source_agent} · {timestamp(item.timestamp)}</span></div>
                          <span className="output-open-label">{index === 0 ? "Open" : "View output"} <ChevronDown size={14} /></span>
                        </summary>
                        <div className="output-document-body">
                          <pre>{item.content}</pre>
                          <button className="secondary-button" onClick={() => downloadSpecialistOutput(item)}>Download this output .md</button>
                        </div>
                      </details>
                    ))}
                  </div>
                ) : <div className="output-empty-state"><FileText size={20} /><span>Waiting for the first specialist document.</span></div>}
              </section>

              {plan.execution_status === "failed" && (
                <section className="panel diagnostic-panel">
                  <div className="panel-heading"><h3><AlertTriangle size={17} /> Run diagnostics</h3><span>{failureEvents.length} failure events</span></div>
                  <div className="diagnostic-body">
                    {failureEvents.length > 0 ? failureEvents.slice().reverse().map((item) => (
                      <div className="diagnostic-event" key={item.id}>
                        <div className="feed-meta"><b>{item.actor}</b><span>{timestamp(item.timestamp)}</span></div>
                        <strong>{item.kind === "agent_failed" && /ProviderUnavailableError|cooling down|request budget|daily quota|rate limit/i.test(item.message) ? "blocked by provider" : item.kind.replaceAll("_", " ")}</strong>
                        <p>{item.message}</p>
                      </div>
                    )) : <p>The run stopped, but no detailed failure event was saved.</p>}
                    {!failureDetailsCaptured && <div className="diagnostic-note"><b>Historical log limitation:</b> This run saved only the exception type <code>RuntimeError</code>, not the underlying provider response. The historical record cannot confirm whether it was an HTTP 429 rate limit, a transport issue, or another provider error.</div>}
                    {qaDisplayStatus === "not_run" && <div className="diagnostic-note"><b>QA was not run.</b> Specialist execution stopped before the independent reviewer started. This is not a QA rejection.</div>}
                    {failureEvents.some((item) => /daily quota exhausted/i.test(item.message)) && <div className="diagnostic-note"><b>Gemini daily quota exhausted.</b> This is a provider/project quota reset, not an application timer. Check the live usage in <a href="https://ai.dev/usage?tab=rate-limit" target="_blank" rel="noreferrer">Google AI Studio usage</a> before retrying.</div>}
                    {blockedAgentCount > 0 && <div className="diagnostic-note"><b>{blockedAgentCount} specialist(s) were blocked before receiving a model response.</b> They are not counted as execution failures. Their workstreams can be resumed when the provider becomes available.</div>}
                    {failureEvents.some((item) => /cooling down for about (\d+)s|retry after about (\d+) seconds/i.test(item.message)) && <div className="diagnostic-note"><b>Provider-supplied retry window.</b> The long wait comes from the upstream provider response, not a hardcoded app timer. Check the current project/model quota in <a href="https://ai.dev/usage?tab=rate-limit" target="_blank" rel="noreferrer">Google AI Studio usage</a>; a daily quota may remain blocked until its reset.</div>}
                    {plan.approval_status === "approved" && qaDisplayStatus !== "passed" && qaDisplayStatus !== "rejected" && <div className="diagnostic-retry-row"><div><b>Resume this mission</b><small>Completed specialist documents are saved. Only failed or unfinished roles will be called again.</small></div><button className="send-button" onClick={retryFailedWorkstreams} disabled={retryLoading}>{retryLoading ? <LoaderCircle className="spin" size={15} /> : <Zap size={15} />} {retryLoading ? "Queueing retry..." : "Retry unfinished workstreams"}</button></div>}
                    {providerDetails.length === 1 && !fallbackConfigured && <div className="diagnostic-note"><b>Single-provider setup:</b> All specialists share one AI provider quota. Add a second provider in Provider settings / the backend environment to enable automatic failover. Current local pacing: up to {providerDetails[0]?.max_requests_per_minute ?? 4} requests per minute, with a {providerDetails[0]?.min_request_interval_seconds ?? 15}-second minimum interval.</div>}
                  </div>
                </section>
              )}

              {plan.execution_status === "awaiting_approval" && (
                <div className="approval-card">
                  <div className="approval-copy"><div className="eyebrow"><LockKeyhole size={13} /> HUMAN APPROVAL REQUIRED</div><h3>Your team is ready. You are in control.</h3><p>Review the roles and workstreams. No specialist starts execution until you approve this plan.</p>{!providerConfigured && <div className="settings-help">Live execution is not enabled yet. Configure at least one provider endpoint in the backend hosting environment, then create a fresh plan. Demo plans cannot be executed.</div>}{plan.category === "research" && !webResearchConfigured && <div className="settings-help">Live web search is not configured. Add TAVILY_API_KEY to the backend hosting environment to collect source URLs and snippets. The team can still draft from model knowledge, but it must state that no live web search was performed.</div>}</div>
                  <div className="approval-actions"><button className="reject-button" onClick={() => decideApproval("reject")} disabled={approvalLoading}>Reject plan</button><button className="send-button" onClick={() => decideApproval("approve")} disabled={approvalLoading || providerConfigured !== true || plan.execution_mode !== "provider_planned"}>{approvalLoading ? <LoaderCircle className="spin" size={16} /> : <ShieldCheck size={16} />} Approve & start team</button></div>
                </div>
              )}

              {error && <div className="error-banner">{error}</div>}

              <div className="metric-row">
                <div className="metric-card"><span>TEAM SIZE</span><b>{plan.agents.length}</b><small>Specialists assembled</small></div>
                <div className="metric-card"><span>COMPLETED</span><b>{completedAgentCount}</b><small>Finished specialist roles</small></div>
                <div className="metric-card"><span>FAILED / BLOCKED</span><b>{failedAgentCount + blockedAgentCount}</b><small>{failedAgentCount} failed · {blockedAgentCount} provider blocked</small></div>
              </div>

              <section className="team-robot-stage">
                <div className="team-stage-heading"><div><span className="eyebrow"><Activity size={13} /> SPECIALIST FLOOR</span><b>Your bots, in their current work states</b></div><span>{completedAgentCount} completed · {blockedAgentCount} provider blocked · {failedAgentCount} failed · {runningAgentCount} working · {waitingAgentCount} waiting</span></div>
                <div className="team-robot-row">{plan.agents.map((agent, index) => { const displayStatus = visibleAgentStatus(agent, plan); return <div className="team-robot-item" key={agent.id}><Robot agent={{ ...agent, status: displayStatus }} index={index} /><b>{agent.name}</b><small>{displayStatus === "blocked_by_provider" ? "Blocked by provider" : displayStatus.replaceAll("_", " ")}</small></div>; })}</div>
              </section>

              <div className="workspace-columns">
                <section className="panel"><div className="panel-heading"><h3><Users size={17} /> Your specialist team</h3><span>{plan.agents.length} agents</span></div><div className="agent-list">{plan.agents.map((agent) => { const displayStatus = visibleAgentStatus(agent, plan); return <div className="agent-row" key={agent.id}><div className={"agent-avatar bot-" + robotVariant(agent)} style={{ backgroundImage: `url("${ROBOT_ATLAS_URL}")` }} role="img" aria-label={agent.name + " robot"} /><div className="agent-info"><b>{agent.name}</b><span>{agent.role} · {agent.personality}</span><small>{agent.task}</small></div><span className={"agent-status status-" + displayStatus}>{displayStatus === "blocked_by_provider" ? "provider blocked" : displayStatus.replaceAll("_", " ")}</span></div>; })}</div></section>
                <section className="panel"><div className="panel-heading"><h3><Workflow size={17} /> Workstreams</h3><span>{plan.subtasks.length} total</span></div><div className="subtask-list">{plan.subtasks.map((task, i) => { const displayStatus = visibleSubtaskStatus(task, plan); return <div className="subtask-row" key={task.id}><div className="step-number">{String(i + 1).padStart(2, "0")}</div><div><b>{task.title}</b><small>Owner: {task.owner}</small></div><span className={"planned-tag status-" + displayStatus}>{subtaskStatusLabel(displayStatus)}</span></div>; })}</div><div className={"qa-card qa-" + qaDisplayStatus}><ShieldCheck size={19} /><div><b>Independent QA · {qaStatusLabel(qaDisplayStatus)}</b><small>{qaDisplayStatus === "passed" ? "The reviewer approved the deliverable." : qaDisplayStatus === "rejected" ? "The independent reviewer rejected the draft after reviewing it." : qaDisplayStatus === "failed" ? "The reviewer started but could not complete a valid review." : qaDisplayStatus === "not_run" ? "No QA decision was made because prerequisite specialist execution stopped first." : "The independent reviewer will inspect the integrated work after the specialists report."}</small></div></div></section>
              </div>

              {plan.qa_feedback && (
                <section className="panel detail-panel"><div className="panel-heading"><h3><ShieldCheck size={17} /> Independent QA report</h3><span>{qaFeedback?.decision || plan.qa_status}</span></div><div className="detail-body"><b>{qaFeedback?.summary || "QA review report"}</b>{qaFeedback?.issues && qaFeedback.issues.length > 0 && <ul>{qaFeedback.issues.map((issue, i) => <li key={i}>{issue}</li>)}</ul>}{qaFeedback?.revision_instructions && <p><strong>Revision request:</strong> {qaFeedback.revision_instructions}</p>}</div></section>
              )}

              {plan.team_discussion && (
                <section className="panel detail-panel"><div className="panel-heading"><h3><MessageSquare size={17} /> Team deliberation</h3><span>Shared review</span></div><div className="detail-body output-content">{plan.team_discussion}</div></section>
              )}

              {plan.result_markdown && (
                <section className="panel detail-panel deliverable-panel"><div className="panel-heading"><h3><FileText size={17} /> Integrated deliverable</h3><div className="deliverable-actions"><span>{plan.execution_status === "completed" ? "QA approved" : "Draft · not approved"}</span><button className="secondary-button" onClick={downloadDeliverableMarkdown}>Download .md</button><button className="secondary-button" onClick={printDeliverableAsPdf}>Print / Save PDF</button></div></div><pre className="deliverable-content">{plan.result_markdown}</pre></section>
              )}

              <div className="workspace-columns lower-panels">
                <section className="panel"><div className="panel-heading"><h3><Radio size={17} /> Shared team memory</h3><span>{plan.shared_memory.length} entries</span></div><div className="feed-list">{plan.shared_memory.length === 0 ? <p className="empty-feed">No shared findings yet. Agent outputs will appear here after execution starts.</p> : plan.shared_memory.slice().reverse().slice(0, 8).map((item) => <div className="feed-item" key={item.id}><div className="feed-meta"><b>{item.source_agent}</b><span>{timestamp(item.timestamp)}</span></div><strong>{item.title}</strong><p>{item.content}</p></div>)}</div></section>
                <section className="panel"><div className="panel-heading"><h3><Activity size={17} /> Activity timeline</h3><span>{plan.events.length} events</span></div><div className="feed-list">{plan.events.length === 0 ? <p className="empty-feed">Task activity will appear here.</p> : plan.events.slice().reverse().slice(0, 12).map((item) => <div className="timeline-item" key={item.id}><span className="timeline-dot" /><div><div className="feed-meta"><b>{item.actor}</b><span>{timestamp(item.timestamp)}</span></div><strong>{item.kind.replaceAll("_", " ")}</strong><p>{item.message}</p></div></div>)}</div></section>
              </div>

              {plan.messages.length > 0 && <section className="panel detail-panel"><div className="panel-heading"><h3><MessageSquare size={17} /> Agent communication</h3><span>{plan.messages.length} messages</span></div><div className="feed-list">{plan.messages.slice().reverse().slice(0, 8).map((message) => <div className="feed-item" key={message.id}><div className="feed-meta"><b>{message.from_agent} → {message.to_agent}</b><span>{timestamp(message.timestamp)}</span></div><strong>{message.kind.replaceAll("_", " ")}</strong><p>{message.content}</p></div>)}</div></section>}
            </>}
          </div>
        )}

        {view === "settings" && (
          <div className="settings-view"><div className="eyebrow"><Settings2 size={14} /> CONFIGURATION</div><h1>Provider <span>settings</span></h1><p className="settings-intro">Provider credentials stay on the backend. This public browser interface never receives or stores API keys.</p><div className="settings-card"><div className="settings-card-head"><div className="provider-icon"><Zap size={20} /></div><div><b>Multi-provider router</b><small>Priority routing · local request budgets · bounded failover</small></div><span className={providerConfigured ? "connected-tag" : "pending-tag"}>{providerConfigured ? "Configured" : "Not configured"}</span></div><div className="settings-detail"><div><span>Configured endpoints</span><b>{providerConfigured === null ? "Backend not reachable" : providerDetails.length ? String(providerDetails.length) + " provider(s)" : "Demo planning mode"}</b></div><div><span>Fallback model</span><b>{fallbackConfigured ? "Configured" : "Not configured"}</b></div><div><span>Credential storage</span><b>Backend environment only</b></div><div><span>Live web research</span><b>{webResearchConfigured ? "Tavily configured" : "Not configured"}</b></div></div>{configurationError && <div className="error-banner">Provider configuration issue: {configurationError}</div>}<div className="settings-help">Configure multiple endpoints using AI_PROVIDERS_JSON in your Render backend environment. Each entry needs name, protocol, base_url, api_key, and model. Supported protocols: openai_compatible, gemini_generate_content, and anthropic_messages. Optional fields include priority, max_requests_per_hour, max_output_tokens, max_output_field, fallback_model, and api_version. The legacy AI_API_KEY, AI_PROVIDER_PROTOCOL, AI_API_BASE_URL, and AI_MODEL variables are also supported. Save the variables in Render, wait for redeployment, then refresh and create a fresh mission. Never paste API keys into chat or commit them to GitHub.</div><div className="provider-test-toolbar"><div><b>Test your configured endpoints</b><small>Each click sends one short test prompt per provider and consumes that provider's request quota.</small></div><button className="secondary-button" onClick={testProviderConnections} disabled={testingProviders || providerDetails.length === 0}>{testingProviders ? <LoaderCircle className="spin" size={15} /> : <Zap size={15} />} {testingProviders ? "Testing..." : "Test connections"}</button></div>{providerTestError && <div className="error-banner">{providerTestError}</div>}{providerTestedAt && <div className="test-timestamp">Last tested: {timestamp(providerTestedAt)}</div>}{providerDetails.length > 0 && <div className="provider-list">{providerDetails.map((provider) => { const result = providerTests.find((item) => item.name === provider.name); return <div className="provider-row" key={provider.name}><div><b>{provider.name}</b><small>{provider.model} · {provider.protocol.replaceAll("_", " ")}</small></div><span>{result ? (result.status.toUpperCase() + (result.latency_ms === null ? "" : " · " + result.latency_ms + " ms")) : provider.cooling_down ? ((provider.cooldown_info?.label || "Provider blocked") + " · " + formatWait(provider.cooldown_seconds || 0)) : (provider.requests_this_minute ?? 0) + " / " + (provider.max_requests_per_minute ?? 4) + " calls/min" + (provider.max_requests_per_hour ? " · " + provider.requests_this_hour + " / " + provider.max_requests_per_hour + " per hour" : "")}</span><i className={result ? "provider-test-" + result.status : provider.cooling_down ? "cooldown-indicator" : "ready-indicator"} /></div>; })}</div>}</div><div className="settings-card settings-security"><div className="settings-card-head"><div className="provider-icon"><ShieldCheck size={20} /></div><div><b>Task storage · V3</b><small>Durable history · API restart recovery</small></div><span className={durableStorageConfigured ? "connected-tag" : "pending-tag"}>{durableStorageConfigured ? "Persistent" : "Memory mode"}</span></div><div className="settings-detail"><div><span>Storage backend</span><b>{storageBackend === "supabase_postgresql" ? "SUPABASE POSTGRES" : storageBackend.toUpperCase()}</b></div><div><span>Restart persistence</span><b>{durableStorageConfigured ? "Enabled" : "Not configured"}</b></div><div><span>Queue backend</span><b>{taskQueueBackend === "redis_streams" ? "REDIS STREAMS" : "IN-PROCESS PREVIEW"}</b></div><div><span>Durable queue</span><b>{durableQueueConfigured ? "Enabled" : "Not configured"}</b></div><div><span>Configuration</span><b>DATABASE_URL + REDIS_URL</b></div></div><div className="settings-help">Set DATABASE_URL in the Render backend Environment using the Supabase Transaction Pooler URI (port 6543), and REDIS_URL using your Upstash Redis TLS URL. Task snapshots are stored in a private schema with row-level security; approved jobs enter a durable Redis Stream. Both variables must be configured to enable queue mode. Without them, the app uses the in-process preview runner.</div><div className="settings-card-head"><div className="provider-icon"><ShieldCheck size={20} /></div><div><b>Execution and privacy boundaries</b><small>V3 preview guardrails</small></div></div><ul><li>Human approval is required before specialist execution.</li><li>API keys are read only by the backend.</li><li>Only up to eight specialists run in parallel in this preview.</li><li>QA can reject the draft and request one bounded revision pass.</li><li>Task history persists when Supabase DATABASE_URL is configured; otherwise demo mode uses process memory.</li><li>With Supabase and Redis configured, interrupted approved jobs are reclaimed from the durable queue after the configured idle lease; otherwise interrupted jobs are marked failed.</li><li>Agents do not yet have website, filesystem, or terminal tools.</li></ul></div></div>
        )}
      </section>

      <footer className="app-footer"><span>WORKFORCE ENGINE <b>V3 AUTONOMOUS EXECUTION</b></span><span><CheckCircle2 size={13} /> {plan ? statusLabel(plan.execution_status) : "Ready for a mission"} <i /> {statusLabelText}</span></footer>
    </main>
  );
}
