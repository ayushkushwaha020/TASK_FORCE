import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  Activity, ArrowRight, Bot, CheckCircle2, ChevronDown, CircleHelp,
  Code2, LayoutDashboard, LoaderCircle, MessageSquare,
  Plus, Search, Send, Settings2, ShieldCheck, Sparkles, Users,
  Workflow, Zap,
} from "lucide-react";

type Agent = {
  id: string;
  name: string;
  role: string;
  personality: string;
  status: string;
  task: string;
  emoji: string;
};
type Subtask = { id: string; title: string; owner: string; status: string };
type TaskPlan = {
  task_id: string;
  title: string;
  category: string;
  execution_mode: "demo" | "provider_planned";
  notice: string;
  agents: Agent[];
  subtasks: Subtask[];
  qa_status: string;
};

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");

const starterPrompts = [
  { icon: Code2, label: "Build an application", prompt: "Plan and build a full-stack task management application with authentication, a dashboard, and automated tests." },
  { icon: Search, label: "Research a topic", prompt: "Research the current state of multi-agent AI systems, compare leading approaches, verify sources, and prepare a structured report." },
  { icon: Users, label: "Plan an event", prompt: "Create a complete plan for a 200-person technology hackathon, including budget, venue, schedule, logistics, marketing, and risks." },
];

function Robot({ agent, index }: { agent?: Agent; index: number }) {
  const emoji = agent?.emoji || ["🤖", "🧑‍🚀", "🦾", "🤖"][index % 4];
  return <div className={`robot robot-${index % 4}`} title={agent ? `${agent.name} · ${agent.role}` : "Workforce assistant"}>
    <span className="robot-face">{emoji}</span>
    <span className="robot-shadow" />
  </div>;
}

export default function App() {
  const [view, setView] = useState<"chat" | "workspace" | "settings">("chat");
  const [prompt, setPrompt] = useState("");
  const [plan, setPlan] = useState<TaskPlan | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [providerConfigured, setProviderConfigured] = useState<boolean | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/api/config/status`)
      .then((r) => r.ok ? r.json() : Promise.reject(new Error("Backend unavailable")))
      .then((data) => setProviderConfigured(Boolean(data.provider_configured)))
      .catch(() => setProviderConfigured(null));
  }, []);

  async function submitTask(event?: FormEvent) {
    event?.preventDefault();
    if (!prompt.trim() || loading) return;
    setLoading(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE}/api/tasks/plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instruction: prompt.trim() }),
      });
      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail.detail || `Task planning failed (${response.status})`);
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

  const activeCount = plan?.agents.length ?? 0;
  const statusLabel = providerConfigured === true ? "Provider configured" : providerConfigured === false ? "Demo planning mode" : "Backend not connected";
  const taskSummary = useMemo(() => plan?.subtasks.length ? plan.subtasks.length + " planned workstreams" : "Ready for your first task", [plan]);

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark"><Bot size={23} /></div>
          <div><strong>workforce<span>.ai</span></strong><small>YOUR AI OPERATIONS TEAM</small></div>
        </div>
        <button className={`nav-item ${view === "chat" ? "selected" : ""}`} onClick={() => setView("chat")}><MessageSquare size={18} /> Command Center</button>
        <button className={`nav-item ${view === "workspace" ? "selected" : ""}`} onClick={() => setView("workspace")}><LayoutDashboard size={18} /> Workforce HQ</button>
        <button className={`nav-item ${view === "settings" ? "selected" : ""}`} onClick={() => setView("settings")}><Settings2 size={18} /> Provider settings</button>
        <div className="sidebar-bottom">
          <div className="sidebar-status"><span className={providerConfigured === null ? "dot amber" : "dot"} /> <span>{statusLabel}</span></div>
          <div className="user-chip"><div className="avatar">A</div><div><b>Workspace</b><small>Local session</small></div><ChevronDown size={15} /></div>
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
              <p>Give your workforce a goal. It will plan the work, assemble the right specialists, and prepare a verified path forward.</p>
              <div className="hero-pills"><span><Users size={14} /> Dynamic teams</span><span><ShieldCheck size={14} /> Human in control</span><span><Zap size={14} /> Parallel planning</span></div>
            </div>

            <div className="robot-stage">
              <div className="stage-glow" />
              <div className="stage-caption"><span className="live-dot" /> YOUR TEAM IS STANDING BY</div>
              <div className="robot-line">
                {Array.from({ length: 7 }, (_, i) => <Robot key={i} index={i} />)}
              </div>
              <div className="stage-floor" />
            </div>

            <div className="composer-wrap">
              <form className="composer" onSubmit={submitTask}>
                <div className="composer-top"><span><Sparkles size={15} /> Describe the outcome you want</span><span className="shortcut">ENTER TO PLAN</span></div>
                <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder="Build a product, research a topic, plan an event… give your team a mission." rows={3} />
                <div className="composer-bottom"><span className="composer-hint"><Plus size={16} /> Attachments coming in a later milestone</span><button className="send-button" type="submit" disabled={!prompt.trim() || loading}>{loading ? <LoaderCircle className="spin" size={17} /> : <Send size={17} />} {loading ? "Planning..." : "Assemble my team"}</button></div>
              </form>
              {error && <div className="error-banner">{error}</div>}
              <div className="starter-label">OR START WITH A MISSION</div>
              <div className="starter-grid">{starterPrompts.map((item) => <button key={item.label} className="starter-card" onClick={() => setPrompt(item.prompt)}><item.icon size={17} /><span>{item.label}</span><ArrowRight size={15} /></button>)}</div>
            </div>
          </div>
        )}

        {view === "workspace" && (
          <div className="workspace-view">
            <div className="workspace-heading"><div><div className="eyebrow"><Activity size={14} /> LIVE WORKSPACE</div><h1>Workforce <span>HQ</span></h1><p>See how your specialists are organised around the mission.</p></div><button className="secondary-button" onClick={() => { setPlan(null); setPrompt(""); setView("chat"); }}><Plus size={16} /> New mission</button></div>
            {!plan ? <div className="empty-workspace"><div className="empty-icon"><Workflow size={30} /></div><h2>No active mission yet</h2><p>Submit a task in Command Center and your team will appear here.</p><button className="send-button" onClick={() => setView("chat")}>Go to Command Center <ArrowRight size={16} /></button></div> : <>
              <div className="mission-card"><div><span className="card-kicker">CURRENT MISSION · {plan.category.toUpperCase()}</span><h2>{plan.title}</h2><p>{taskSummary} · QA: {plan.qa_status.replaceAll("_", " ")}</p></div><span className={`mode-tag ${plan.execution_mode}`}>{plan.execution_mode === "demo" ? "DEMO PLAN" : "PROVIDER-ASSISTED PLAN"}</span></div>
              <div className="notice-banner"><ShieldCheck size={17} /><span>{plan.notice}</span></div>
              <div className="metric-row"><div className="metric-card"><span>TEAM SIZE</span><b>{activeCount}</b><small>Specialist agents</small></div><div className="metric-card"><span>WORKSTREAMS</span><b>{plan.subtasks.length}</b><small>Planned subtasks</small></div><div className="metric-card"><span>QA GATE</span><b>01</b><small>Independent reviewer assigned</small></div></div>
              <div className="workspace-columns">
                <section className="panel"><div className="panel-heading"><h3><Users size={17} /> Your specialist team</h3><span>{activeCount} agents</span></div><div className="agent-list">{plan.agents.map((agent, i) => <div className="agent-row" key={agent.id}><div className="agent-avatar">{agent.emoji || "🤖"}</div><div className="agent-info"><b>{agent.name}</b><span>{agent.role} · {agent.personality}</span><small>{agent.task}</small></div><span className="agent-status">{agent.status.replaceAll("_", " ")}</span></div>)}</div></section>
                <section className="panel"><div className="panel-heading"><h3><Workflow size={17} /> Workstreams</h3><span>{plan.subtasks.length} planned</span></div><div className="subtask-list">{plan.subtasks.map((task, i) => <div className="subtask-row" key={task.id}><div className="step-number">{String(i + 1).padStart(2, "0")}</div><div><b>{task.title}</b><small>Owner: {task.owner}</small></div><span className="planned-tag">{task.status}</span></div>)}</div><div className="qa-card"><ShieldCheck size={19} /><div><b>Independent QA gate</b><small>Verification is pending until execution evidence exists.</small></div></div></section>
              </div>
            </>}
          </div>
        )}

        {view === "settings" && (
          <div className="settings-view"><div className="eyebrow"><Settings2 size={14} /> CONFIGURATION</div><h1>Provider <span>settings</span></h1><p className="settings-intro">Connect an AI provider through server-side environment variables. API keys are never entered into this browser UI.</p><div className="settings-card"><div className="settings-card-head"><div className="provider-icon"><Zap size={20} /></div><div><b>OpenAI-compatible provider</b><small>Foundation adapter · server-side configuration</small></div><span className={providerConfigured ? "connected-tag" : "pending-tag"}>{providerConfigured ? "Configured" : "Not configured"}</span></div><div className="settings-detail"><div><span>Provider status</span><b>{providerConfigured === null ? "Backend not reachable" : providerConfigured ? "API key detected on server" : "Demo planning mode"}</b></div><div><span>Credential storage</span><b>Backend environment only</b></div><div><span>Browser key storage</span><b>Never used</b></div></div><div className="settings-help">To enable provider-assisted planning, configure AI_API_KEY, AI_MODEL, and AI_API_BASE_URL in the backend hosting environment. Never paste secrets into chat or commit them to GitHub.</div></div></div>
        )}
      </section>

      <footer className="app-footer"><span>WORKFORCE ENGINE <b>V1 FOUNDATION</b></span><span><CheckCircle2 size={13} /> {plan ? "Task plan received" : "Ready for a mission"} <i /> {statusLabel}</span></footer>
    </main>
  );
}
