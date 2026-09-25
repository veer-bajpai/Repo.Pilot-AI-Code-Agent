import { useEffect, useState, type FormEvent } from "react";
import {
  Check,
  ChevronRight,
  Code2,
  GitBranch,
  LogOut,
  Menu,
  Moon,
  Plus,
  Send,
  ShieldCheck,
  Sparkles,
  Sun,
  X,
} from "lucide-react";
import "./styles.css";

type User = {
  id: string;
  email: string;
  name: string;
  plan: string;
  email_verified: boolean;
};
type Demo = { id: string; name: string; task: string; description?: string };
type Session = {
  id: string;
  name: string;
  kind: string;
  status: string;
  error?: string;
  analysis?: {
    demo_id?: string;
    files?: number;
    languages?: { language: string }[];
  };
};
type Run = {
  id: string;
  status: string;
  summary?: string;
  error?: string;
  cost_usd?: number;
  tests_passed?: number;
  diff?: string;
};
type Event = { id: number; kind: string; data: Record<string, any> };

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const data =
    response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data?.detail || "Something went wrong");
  return data as T;
}

function Logo() {
  return (
    <div className="logo">
      <span className="logo-mark">
        <span />
      </span>
      <span>RepoPilot</span>
    </div>
  );
}

function AuthScreen({ onAuth }: { onAuth: (user: User) => void }) {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const data = await api<{ user: User }>(
        `/api/auth/${mode === "login" ? "login" : "signup"}`,
        { method: "POST", body: JSON.stringify({ email, password, name }) },
      );
      onAuth(data.user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to authenticate");
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="auth-shell">
      <section className="auth-card">
        <Logo />
        <p className="eyebrow">YOUR CODE, WITH A SECOND SET OF EYES</p>
        <h1>
          {mode === "login" ? "Welcome back." : "Start building with clarity."}
        </h1>
        <p className="auth-copy">
          RepoPilot turns repository work into a reviewable conversation. Every
          edit stays behind your approval.
        </p>
        <div className="oauth-row">
          <a className="oauth" href="/api/auth/oauth/google">
            <span className="google-g">G</span> Continue with Google
          </a>
          <a className="oauth" href="/api/auth/oauth/github">
            <GitBranch size={18} /> Continue with GitHub
          </a>
        </div>
        <div className="divider">
          <span>or use your email</span>
        </div>
        <form onSubmit={submit} className="auth-form">
          {mode === "signup" && (
            <label>
              Name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Your name"
                autoComplete="name"
              />
            </label>
          )}
          <label>
            Email
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
              autoComplete="email"
            />
          </label>
          <label>
            Password
            <input
              type="password"
              required
              minLength={12}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="At least 12 characters"
              autoComplete={
                mode === "login" ? "current-password" : "new-password"
              }
            />
          </label>
          {error && (
            <div className="form-error" role="alert">
              {error}
            </div>
          )}
          <button className="primary wide" disabled={busy}>
            {busy
              ? "Working…"
              : mode === "login"
                ? "Continue"
                : "Create account"}{" "}
            <ChevronRight size={17} />
          </button>
        </form>
        <div className="auth-footer">
          {mode === "login" ? (
            <>
              New to RepoPilot?{" "}
              <button onClick={() => setMode("signup")}>
                Create an account
              </button>
              <a href="/reset">Forgot password?</a>
            </>
          ) : (
            <>
              Already have an account?{" "}
              <button onClick={() => setMode("login")}>Sign in</button>
            </>
          )}
        </div>
      </section>
    </main>
  );
}

function App({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [demos, setDemos] = useState<Demo[]>([]);
  const [active, setActive] = useState<Session | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [repo, setRepo] = useState("");
  const [task, setTask] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [dark, setDark] = useState(localStorage.getItem("rp-theme") === "dark");
  const [mobile, setMobile] = useState(false);
  useEffect(() => {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    localStorage.setItem("rp-theme", dark ? "dark" : "light");
  }, [dark]);
  useEffect(() => {
    api<{ demos: Demo[] }>("/api/config")
      .then((c) => setDemos(c.demos))
      .catch(() => {});
    loadSessions();
  }, []);
  async function loadSessions() {
    const list = await api<Session[]>("/api/sessions");
    setSessions(list);
    if (!active && list[0]) setActive(list[0]);
  }
  async function add(source: { demo: string } | { repo_url: string }) {
    setBusy(true);
    setNotice("");
    try {
      const s = await api<Session>("/api/sessions", {
        method: "POST",
        body: JSON.stringify(source),
      });
      await loadSessions();
      setActive(s);
      setTask(
        s.kind === "demo"
          ? demos.find((d) => d.id === s.analysis?.demo_id)?.task || ""
          : "",
      );
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "Could not load repository");
    } finally {
      setBusy(false);
    }
  }
  async function startRun() {
    if (!active || task.trim().length < 5) return;
    setBusy(true);
    setNotice("");
    setEvents([]);
    try {
      const r = await api<Run>(`/api/sessions/${active.id}/runs`, {
        method: "POST",
        body: JSON.stringify({
          task,
          approval_mode: "ask",
          mode: active.kind === "demo" ? "simulated" : "live",
        }),
      });
      setRun(r);
      poll(r.id);
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "Could not start run");
    } finally {
      setBusy(false);
    }
  }
  async function poll(id: string) {
    const loop = async () => {
      try {
        const data = await api<{ run: Run; events: Event[] }>(
          `/api/runs/${id}/events?after=${events.at(-1)?.id || 0}`,
        );
        setRun(data.run);
        if (data.events.length) setEvents((prev) => [...prev, ...data.events]);
        if (
          ![
            "completed",
            "failed",
            "cancelled",
            "budget_exceeded",
            "interrupted",
          ].includes(data.run.status)
        )
          setTimeout(loop, 900);
      } catch (e) {
        setNotice(e instanceof Error ? e.message : "Run polling stopped");
      }
    };
    loop();
  }
  async function decide(id: string, approved: boolean) {
    await api(`/api/approvals/${id}`, {
      method: "POST",
      body: JSON.stringify({ approved, note: "" }),
    });
    if (run) poll(run.id);
  }
  const pending = events.find(
    (e) =>
      e.kind === "approval_requested" &&
      !events.some(
        (d) =>
          d.kind === "approval_decided" &&
          d.data.approval_id === e.data.approval_id,
      ),
  );
  return (
    <div className="app-shell">
      <aside className={mobile ? "sidebar open" : "sidebar"}>
        <div className="side-top">
          <Logo />
          <button
            className="icon-btn mobile-only"
            onClick={() => setMobile(false)}
            aria-label="Close menu"
          >
            <X size={18} />
          </button>
        </div>
        <button
          className="new-run"
          onClick={() => {
            setActive(null);
            setRun(null);
            setEvents([]);
            setMobile(false);
          }}
        >
          <Plus size={17} /> New run
        </button>
        <div className="side-label">WORKSPACES</div>
        <div className="history">
          {sessions.map((s) => (
            <button
              key={s.id}
              className={
                active?.id === s.id ? "history-item active" : "history-item"
              }
              onClick={() => {
                setActive(s);
                setMobile(false);
              }}
            >
              <Code2 size={15} />
              <span>{s.name}</span>
              <small>{s.status === "ready" ? "Ready" : "Loading"}</small>
            </button>
          ))}
        </div>
        <div className="side-bottom">
          <div className="account">
            <div className="avatar">
              {(user.name || user.email)[0].toUpperCase()}
            </div>
            <div>
              <strong>{user.name || "Your account"}</strong>
              <small>{user.plan} plan</small>
            </div>
          </div>
          <button
            className="icon-btn"
            onClick={() => setDark(!dark)}
            aria-label="Toggle theme"
          >
            {dark ? <Sun size={17} /> : <Moon size={17} />}
          </button>
          <button
            className="icon-btn"
            onClick={async () => {
              await api("/api/auth/logout", { method: "POST" });
              onLogout();
            }}
            aria-label="Sign out"
          >
            <LogOut size={17} />
          </button>
        </div>
      </aside>
      <main className="main-shell">
        <header className="main-header">
          <button
            className="icon-btn mobile-only"
            onClick={() => setMobile(true)}
            aria-label="Open menu"
          >
            <Menu size={19} />
          </button>
          <div>
            <span className="crumb">Workspace</span>
            <h2>{active?.name || "A quiet place to start"}</h2>
          </div>
          <div className="header-status">
            <span className="status-dot" /> Gemini connected
          </div>
        </header>
        {notice && (
          <div className="toast" role="alert">
            {notice}
            <button onClick={() => setNotice("")}>
              <X size={15} />
            </button>
          </div>
        )}
        <section className="conversation">
          {!active ? (
            <div className="empty-state">
              <div className="empty-mark">
                <Sparkles size={27} />
              </div>
              <p className="eyebrow">REPOPILOT WORKSPACE</p>
              <h1>
                Make the next change
                <br />
                <em>easy to review.</em>
              </h1>
              <p>
                Bring a public repository or choose a demo. RepoPilot will
                inspect it, propose a path, and wait before every consequential
                action.
              </p>
              <div className="quick-actions">
                {demos.slice(0, 3).map((d) => (
                  <button key={d.id} onClick={() => add({ demo: d.id })}>
                    <Sparkles size={15} />
                    {d.name}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <>
              <div className="welcome-message">
                <div className="message-icon">
                  <ShieldCheck size={17} />
                </div>
                <div>
                  <strong>Ready when you are.</strong>
                  <p>
                    {active.name} is loaded. Describe the change you want the
                    agent to investigate.
                  </p>
                </div>
              </div>
              {events.map((e) => (
                <div className="event-card" key={e.id}>
                  <div className="event-icon">
                    {e.kind === "approval_requested" ? (
                      <ShieldCheck size={16} />
                    ) : (
                      <Check size={16} />
                    )}
                  </div>
                  <div>
                    <strong>{e.kind.replaceAll("_", " ")}</strong>
                    <p>
                      {e.data.summary ||
                        e.data.task ||
                        e.data.tool ||
                        e.data.message ||
                        "Agent activity recorded."}
                    </p>
                    {e.kind === "approval_requested" && (
                      <pre>{e.data.preview}</pre>
                    )}
                  </div>
                </div>
              ))}
              {pending && (
                <div className="approval-card">
                  <div>
                    <span className="approval-kicker">
                      ACTION NEEDS YOUR EYES
                    </span>
                    <h3>{pending.data.tool}</h3>
                    <p>{pending.data.reason}</p>
                  </div>
                  <div className="approval-preview">
                    <pre>{pending.data.preview}</pre>
                  </div>
                  <div className="approval-actions">
                    <button
                      className="reject"
                      onClick={() => decide(pending.data.approval_id, false)}
                    >
                      <X size={16} /> Reject
                    </button>
                    <button
                      className="primary"
                      onClick={() => decide(pending.data.approval_id, true)}
                    >
                      <Check size={16} /> Approve
                    </button>
                  </div>
                </div>
              )}
              {run &&
                [
                  "completed",
                  "failed",
                  "cancelled",
                  "budget_exceeded",
                  "interrupted",
                ].includes(run.status) && (
                  <div
                    className={
                      run.status === "completed"
                        ? "result-card success"
                        : "result-card"
                    }
                  >
                    <div className="result-icon">
                      {run.status === "completed" ? (
                        <Check size={19} />
                      ) : (
                        <X size={19} />
                      )}
                    </div>
                    <div>
                      <strong>
                        {run.status === "completed"
                          ? "Run complete"
                          : "Run " + run.status}
                      </strong>
                      <p>{run.summary || run.error}</p>
                      <small>
                        {run.cost_usd
                          ? `$${run.cost_usd.toFixed(4)} estimated · `
                          : ""}
                        {run.tests_passed == null
                          ? "Tests not run"
                          : run.tests_passed
                            ? "Tests passed"
                            : "Tests failed"}
                      </small>
                    </div>
                  </div>
                )}
            </>
          )}
        </section>
        <footer className="composer-wrap">
          {!active ? (
            <div className="repo-entry">
              <input
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
                placeholder="Paste a public GitHub repository URL"
              />
              <button
                className="primary"
                disabled={!repo || busy}
                onClick={() => add({ repo_url: repo })}
              >
                <Send size={16} /> Load repository
              </button>
            </div>
          ) : (
            <div className="composer">
              <textarea
                value={task}
                onChange={(e) => setTask(e.target.value)}
                placeholder="Describe a bug or a small change…"
                rows={2}
              />
              <button
                className="primary send"
                disabled={
                  busy || task.trim().length < 5 || active.status !== "ready"
                }
                onClick={startRun}
                aria-label="Run agent"
              >
                <Send size={18} />
              </button>
            </div>
          )}
          <div className="composer-note">
            <span>Every edit is approval-gated.</span>
            <span>RepoPilot may send read code to Gemini.</span>
          </div>
        </footer>
      </main>
    </div>
  );
}

function Root() {
  const [user, setUser] = useState<User | null | undefined>(undefined);
  useEffect(() => {
    Promise.all([
      api<{ user: User | null }>("/api/auth/me"),
      api<{ auth_required: boolean }>("/api/config"),
    ])
      .then(([auth, config]) =>
        setUser(
          auth.user ||
            (!config.auth_required
              ? {
                  id: "guest",
                  email: "",
                  name: "Guest",
                  plan: "free",
                  email_verified: false,
                }
              : null),
        ),
      )
      .catch(() => setUser(null));
  }, []);
  if (user === undefined)
    return (
      <div className="loading-screen">
        <div className="spinner" />
      </div>
    );
  return user ? (
    <App user={user} onLogout={() => setUser(null)} />
  ) : (
    <AuthScreen onAuth={setUser} />
  );
}

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
