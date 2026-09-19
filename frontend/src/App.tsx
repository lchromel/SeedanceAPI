import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api";
import type { Asset, Bootstrap, Config, Kind, Part, Project } from "./types";
import { Button, Modal } from "./components/UI";
import { Sidebar } from "./components/Sidebar";
import { Player } from "./components/Player";
import { GenerationPanel } from "./components/GenerationPanel";
import { Library } from "./components/Library";

function Login({ onLogin }: { onLogin: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(e.currentTarget);
    try {
      await api("login", "POST", Object.fromEntries(form));
      onLogin();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-page">
      <div className="login-brand">
        Video Studio<span>Create something worth watching.</span>
      </div>
      <form className="login-card" onSubmit={submit}>
        <small>TEAM WORKSPACE</small>
        <h1>Welcome back</h1>
        <p>Sign in to your studio.</p>
        <label>
          Username
          <input name="username" autoComplete="username" required autoFocus />
        </label>
        <label>
          Password
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
          />
        </label>
        <label>
          Verification code
          <input
            name="code"
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="[0-9]{6}"
            maxLength={6}
            placeholder="6-digit authenticator code"
          />
        </label>
        {error && (
          <p role="alert" className="error-message">
            {error}
          </p>
        )}
        <Button primary disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
        <small>Access is managed by your team administrator.</small>
      </form>
    </main>
  );
}
export default function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [part, setPart] = useState<Part>("freeform");
  const [collapsed, setCollapsed] = useState(
    () => window.matchMedia("(max-width: 850px)").matches,
  );
  useEffect(() => {
    const media = window.matchMedia("(max-width: 850px)");
    const adapt = () => {
      if (media.matches) setCollapsed(true);
    };
    media.addEventListener("change", adapt);
    return () => media.removeEventListener("change", adapt);
  }, []);
  const [library, setLibrary] = useState<Kind | null>(null);
  const [account, setAccount] = useState(false);
  const [history, setHistory] = useState(false);
  const [rename, setRename] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const generationKey = useRef<string | null>(null);
  useEffect(() => {
    api<{ user: unknown }>("session")
      .then((s) => setAuthenticated(!!s.user))
      .catch((e) => setError(e.message));
  }, []);
  async function refresh() {
    const b = await api<Bootstrap>("bootstrap");
    setBoot(b);
    return b;
  }
  useEffect(() => {
    if (!authenticated) return;
    let alive = true;
    refresh()
      .then(async (b) => {
        const p = b.projects.length
          ? await api<Project>("projects/" + b.projects[0].id)
          : await api<Project>("projects", "POST", {});
        if (alive) setProject(p);
      })
      .catch((e) => setError(e.message));
    return () => {
      alive = false;
    };
  }, [authenticated]);
  useEffect(() => {
    if (!project) return;
    let alive = true;
    const id = project.id;
    const timer = setInterval(async () => {
      try {
        const next = await api<Project>("projects/" + id);
        if (alive) {
          setProject((p) => (p?.id === id ? { ...p, runs: next.runs } : p));
          await refresh();
        }
      } catch {
        /* Keep the last confirmed state through transient network failures. */
      }
    }, 5000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [project?.id]);
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);
  useEffect(() => {
    function close(e: KeyboardEvent) {
      if (e.key === "Escape") setAccount(false);
    }
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, []);
  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function change(config: Config) {
    if (!project || busy) return;
    setProject({ ...project, config });
    setDirty(true);
    generationKey.current = null;
  }
  async function save() {
    if (!project) throw new Error("No project");
    if (!dirty) return project;
    const p = await api<Project>("projects/" + project.id, "PATCH", {
      config: project.config,
      name: project.name,
      revision: project.revision,
    });
    setProject(p);
    setDirty(false);
    return p;
  }
  async function generate() {
    await perform(async () => {
      const p = await save();
      generationKey.current ||= crypto.randomUUID();
      await api(`projects/${p.id}/generate`, "POST", {
        part,
        requestKey: generationKey.current,
      });
      generationKey.current = null;
      setProject(await api<Project>("projects/" + p.id));
      await refresh();
    });
  }
  async function switchProject(id: string) {
    await perform(async () => {
      await save();
      setProject(await api<Project>("projects/" + id));
      setDirty(false);
      generationKey.current = null;
    });
  }
  async function addProject() {
    await perform(async () => {
      if (project) await save();
      setProject(await api<Project>("projects", "POST", {}));
      setDirty(false);
      generationKey.current = null;
      await refresh();
    });
  }
  function selectAsset(a: Asset) {
    if (!project) return;
    const c = project.config;
    if (a.kind === "clothing")
      change({
        ...c,
        clothing: c.clothing.includes(a.id)
          ? c.clothing.filter((id) => id !== a.id)
          : [...c.clothing, a.id],
      });
    else if (a.kind === "motion")
      change({
        ...c,
        motions: [
          ...c.motions,
          { asset: a.id, duration: Math.min(30, a.duration) },
        ],
      });
    else change({ ...c, [a.kind]: a.id });
    if (a.kind !== "clothing") setLibrary(null);
  }
  if (authenticated === false)
    return <Login onLogin={() => setAuthenticated(true)} />;
  if (!project || !boot)
    return (
      <main className="loading">
        <p>{error || "Opening your studio…"}</p>
        {error && <Button onClick={() => location.reload()}>Try again</Button>}
      </main>
    );
  const selected =
    library === "clothing"
      ? project.config.clothing
      : library === "motion"
        ? project.config.motions.map((m) => m.asset)
        : library && project.config[library]
          ? [project.config[library]!]
          : [];
  return (
    <div className={`studio ${collapsed ? "sidebar-collapsed" : ""}`}>
      <Sidebar
        collapsed={collapsed}
        onCollapse={() => setCollapsed(!collapsed)}
        config={project.config}
        assets={boot.assets}
        onLibrary={setLibrary}
        onChange={change}
        accountOpen={account}
        onAccount={() => setAccount(!account)}
        user={boot}
        onHistory={() => {
          setHistory(true);
          setAccount(false);
        }}
        onLogout={() =>
          perform(async () => {
            await save();
            await api("logout", "POST");
            setAuthenticated(false);
            setProject(null);
            setBoot(null);
            setAccount(false);
          })
        }
      />
      <main className="workspace">
        <header className="project-header">
          <div>
            <select
              aria-label="Project"
              value={project.id}
              disabled={busy}
              onChange={(e) => switchProject(e.target.value)}
            >
              {boot.projects.some((p) => p.id === project.id) ? (
                boot.projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))
              ) : (
                <option value={project.id}>{project.name}</option>
              )}
            </select>
            <button
              className="text-button"
              disabled={busy}
              onClick={() => setRename(true)}
            >
              Rename
            </button>
            <button
              className="text-button"
              onClick={addProject}
              disabled={busy}
            >
              New video
            </button>
          </div>
          <div>
            {boot.simulation && (
              <span className="simulation-badge">Local simulation</span>
            )}
            <span className="save-state">
              {dirty ? "Unsaved changes" : "Saved"}
            </span>
            <Button
              disabled={busy || !dirty}
              onClick={() =>
                perform(async () => {
                  await save();
                })
              }
            >
              Save
            </Button>
          </div>
        </header>
        {error && (
          <div className="error-banner" role="alert">
            {error}
            <button onClick={() => setError("")}>Dismiss</button>
          </div>
        )}
        <div className="editor">
          <Player
            project={project}
            busy={busy}
            onExport={() =>
              perform(async () => {
                await api(`projects/${project.id}/export`, "POST");
                setProject(await api<Project>("projects/" + project.id));
              })
            }
          />
          <GenerationPanel
            project={project}
            part={part}
            onPart={(p) => {
              setPart(p);
              generationKey.current = null;
            }}
            onChange={change}
            onGenerate={generate}
            onRetry={(ids) =>
              perform(async () => {
                for (const id of ids)
                  await api("chunks/" + id + "/retry", "POST");
                setProject(await api<Project>("projects/" + project.id));
                await refresh();
              })
            }
            onLibrary={() => setLibrary("motion")}
            busy={busy}
            pricing={boot.pricing}
            assets={boot.assets}
          />
        </div>
      </main>
      {library && (
        <Library
          kind={library}
          assets={boot.assets}
          selected={selected}
          onClose={() => setLibrary(null)}
          onSelect={selectAsset}
          onUpload={(a) => {
            setBoot({ ...boot, assets: [a, ...boot.assets] });
            selectAsset(a);
          }}
        />
      )}
      {rename && (
        <Modal title="Rename project" onClose={() => setRename(false)}>
          <form
            className="rename-form"
            onSubmit={(e) => {
              e.preventDefault();
              setRename(false);
            }}
          >
            <label>
              Project name
              <input
                autoFocus
                maxLength={120}
                required
                value={project.name}
                onChange={(e) => {
                  setProject({ ...project, name: e.target.value });
                  setDirty(true);
                }}
              />
            </label>
            <Button primary type="submit">
              Done
            </Button>
          </form>
        </Modal>
      )}
      {history && (
        <Modal title="Credit history" onClose={() => setHistory(false)}>
          <div className="history-summary">
            <strong>{boot.wallet.available.toLocaleString()}</strong>
            <span>available credits</span>
          </div>
          {boot.history.length ? (
            boot.history.map((e) => (
              <div className="history-row" key={e.id}>
                <div>
                  {
                    (
                      {
                        grant: "Credits added",
                        reserve: "Generation reserved",
                        settle: "Generation completed",
                        release: "Reservation released",
                      } as Record<string, string>
                    )[e.kind]
                  }
                  <small>{new Date(e.created).toLocaleString()}</small>
                </div>
                <span>
                  {e.kind === "grant" || e.kind === "release"
                    ? "+"
                    : e.kind === "settle"
                      ? "−"
                      : ""}
                  {e.amount}
                  {e.kind === "reserve" ? " held" : ""}
                </span>
              </div>
            ))
          ) : (
            <p className="muted">No credit activity yet.</p>
          )}
        </Modal>
      )}
    </div>
  );
}
