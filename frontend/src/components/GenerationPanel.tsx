import { useEffect, useRef, useState } from "react";
import type { Asset, Bootstrap, Config, Part, Project } from "../types";
import { api, time } from "../api";
import { Button, IconButton } from "./UI";
import { ChunkStatusList, GenerationProgressCard } from "./Player";
export function GenerationPanel({
  project,
  part,
  onPart,
  onChange,
  onGenerate,
  onEnhance,
  enhancing,
  onRetry,
  onLibrary,
  busy,
  pricing,
  assets,
}: {
  project: Project;
  part: Part;
  onPart: (p: Part) => void;
  onChange: (c: Config) => void;
  onGenerate: () => void;
  onEnhance: () => Promise<void>;
  enhancing: boolean;
  onRetry: (ids: string[]) => void;
  onLibrary: () => void;
  busy: boolean;
  pricing: Bootstrap["pricing"];
  assets: Asset[];
}) {
  const c = project.config;
  const [referenceText, setReferenceText] = useState("");
  const [referenceError, setReferenceError] = useState("");
  const configRef = useRef(c);
  configRef.current = c;
  const referenceKey = JSON.stringify([
    project.id,
    c.character,
    c.location,
    c.clothing,
    assets
      .filter((a) => [c.character, c.location, ...c.clothing].includes(a.id))
      .map((a) => [a.id, a.category, a.description, a.analysisStatus]),
  ]);
  useEffect(() => {
    let cancelled = false;
    setReferenceText("");
    setReferenceError("");
    api<{ references: string }>("prompt/references", "POST", configRef.current)
      .then((result) => {
        if (!cancelled) setReferenceText(result.references);
      })
      .catch((e: Error) => {
        if (!cancelled) setReferenceError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [referenceKey]);
  const run = project.runs.find((r) => r.part === part);
  const scroll = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (scroll.current) scroll.current.scrollTop = 0;
  }, [run?.id, part]);
  const active =
    run && ["generating", "assembling", "review"].includes(run.state);
  const seconds =
    part === "freeform"
      ? c.duration
      : c.motions.reduce((s, m) => s + m.duration, 0);
  return (
    <section className="generation">
      <div className="stage-tabs" role="tablist" aria-label="Generation part">
        {(["freeform", "motion"] as Part[]).map((p, i) => (
          <button
            key={p}
            role="tab"
            aria-selected={part === p}
            onClick={() => onPart(p)}
          >
            {String(i + 1).padStart(2, "0")}{" "}
            {p === "freeform" ? "Freeform" : "Motion"}
          </button>
        ))}
      </div>
      <div className="generation-card">
        <header className="part-heading">
          <span>{part === "freeform" ? "01" : "02"}</span>
          <h2>{part === "freeform" ? "Freeform" : "Motion"}</h2>
        </header>
        <div className="generation-scroll" ref={scroll}>
          {run && (
            <>
              <GenerationProgressCard run={run} />
              <ChunkStatusList run={run} onRetry={onRetry} busy={busy} />
              {run.revision !== project.revision && (
                <p className="muted">
                  Preview uses the settings saved when generation started.
                </p>
              )}
            </>
          )}
          <fieldset disabled={busy || !!active}>
            {part === "freeform" ? (
              <>
                <label className="field-label">Part duration</label>
                <div className="duration-control">
                  {[4, 30, 60, 90, 120].map((d) => (
                    <Button
                      key={d}
                      primary={c.duration === d}
                      onClick={() => onChange({ ...c, duration: d })}
                    >
                      {d < 60
                        ? `${d} s`
                        : d === 60
                          ? "1 min"
                          : d === 90
                            ? "1:30 min"
                            : "2 min"}
                    </Button>
                  ))}
                </div>
                <label className="script-field">
                  <textarea
                    aria-label="Action"
                    placeholder="What happens in the scene?"
                    value={c.action}
                    onChange={(e) => onChange({ ...c, action: e.target.value })}
                  />
                </label>
                <div className="prompt-actions">
                  <Button
                    onClick={onEnhance}
                    disabled={busy || !c.character || !c.action.trim()}
                  >
                    {enhancing ? "Improving…" : "Improve with DeepSeek"}
                  </Button>
                </div>
                <details className="full-prompt">
                  <summary>
                    Full prompt · reference instructions included
                  </summary>
                  {referenceError ? (
                    <p role="alert">{referenceError}</p>
                  ) : (
                    <pre>
                      {[referenceText, c.action].filter(Boolean).join("\n\n")}
                    </pre>
                  )}
                </details>
                <label className="script-field">
                  <textarea
                    aria-label="Dialogue"
                    placeholder="What does the character say?"
                    value={c.speech}
                    onChange={(e) => onChange({ ...c, speech: e.target.value })}
                  />
                </label>
              </>
            ) : (
              <>
                <div className="motion-heading">
                  <span>Motion sequence</span>
                  <Button onClick={onLibrary}>Choose presets</Button>
                </div>
                {c.motions.length === 0 ? (
                  <div className="empty-sequence">
                    Add a motion reference from your library.
                  </div>
                ) : (
                  c.motions.map((m, i) => {
                    const a = assets.find((a) => a.id === m.asset);
                    return (
                      <div className="motion-item" key={`${m.asset}-${i}`}>
                        <video
                          src={a?.url}
                          muted
                          playsInline
                          preload="metadata"
                        />
                        <div>
                          <strong>{a?.name || "Motion"}</strong>
                          <label>
                            Duration{" "}
                            <input
                              type="number"
                              min={4}
                              max={a?.duration || 120}
                              value={m.duration}
                              onChange={(e) =>
                                onChange({
                                  ...c,
                                  motions: c.motions.map((x, n) =>
                                    n === i
                                      ? {
                                          ...x,
                                          duration: Number(e.target.value),
                                        }
                                      : x,
                                  ),
                                })
                              }
                            />{" "}
                            s
                          </label>
                          <small>Up to {time(a?.duration || 0)}</small>
                        </div>
                        <IconButton
                          icon="Close"
                          label="Remove motion"
                          onClick={() =>
                            onChange({
                              ...c,
                              motions: c.motions.filter((_, n) => n !== i),
                            })
                          }
                        />
                      </div>
                    );
                  })
                )}
                <div className="motion-total">
                  <span>Part duration</span>
                  <strong>{time(seconds)}</strong>
                </div>
              </>
            )}
          </fieldset>
        </div>
        <footer className="generate-footer">
          <div>
            <span>
              {seconds
                ? `${time(seconds)} · ${Math.ceil(seconds / 30)} segments`
                : "Choose a motion preset"}
            </span>
            <span>
              {pricing === null
                ? "Pricing not configured"
                : `${seconds * pricing} credits`}
            </span>
          </div>
          <Button
            primary
            disabled={busy || !!active || !seconds || pricing === null}
            onClick={onGenerate}
          >
            {busy
              ? "Saving…"
              : active
                ? "Generation in progress"
                : run
                  ? "Generate a new version"
                  : `Generate ${part === "freeform" ? "freeform" : "motion"}`}
          </Button>
        </footer>
      </div>
    </section>
  );
}
