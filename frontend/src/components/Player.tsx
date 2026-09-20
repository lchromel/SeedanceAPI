import { useEffect, useRef, useState } from "react";
import type { Project, Run } from "../types";
import { segments } from "../segments";
import { time } from "../api";
import { Button, Icon } from "./UI";
export function Player({
  project,
  onExport,
  busy,
}: {
  project: Project;
  onExport: () => void;
  busy: boolean;
}) {
  const video = useRef<HTMLVideoElement>(null);
  const [selected, setSelected] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const chunks = project.runs
    .filter((r) => r.part !== "full")
    .flatMap((r) => r.chunks)
    .sort((a, b) => a.start - b.start);
  const chunk = chunks[selected];
  const timeline = segments(chunks);
  const seek = useRef<number | null>(null);
  const sources = useRef<Record<string, string>>({});
  const [, refreshSource] = useState(0);
  if (chunk?.url && !sources.current[chunk.id])
    sources.current[chunk.id] = chunk.url;
  function scrub(value: number) {
    const index = chunks.findIndex(
      (c) => value >= c.start && value < c.start + c.duration,
    );
    if (index < 0 || !chunks[index].url) return;
    seek.current = value - chunks[index].start;
    if (index === selected && video.current) {
      video.current.currentTime = seek.current;
      seek.current = null;
    } else setSelected(index);
  }
  const total =
    project.config.duration +
    project.config.motions.reduce((s, m) => s + m.duration, 0);
  const full = project.runs.find((r) => r.part === "full");
  useEffect(() => {
    setSelected(0);
    setElapsed(0);
  }, [project.id]);
  const next = () => {
    if (chunks[selected + 1]?.state === "ready") {
      setSelected(selected + 1);
      setElapsed(0);
    }
  };
  return (
    <section className="player-panel">
      <div className="video-stage">
        {chunk?.url ? (
          <video
            ref={video}
            key={chunk.id}
            src={sources.current[chunk.id]}
            controls
            playsInline
            onTimeUpdate={(e) => setElapsed(e.currentTarget.currentTime)}
            onEnded={next}
            onError={() => {
              if (chunk.url && sources.current[chunk.id] !== chunk.url) {
                sources.current[chunk.id] = chunk.url;
                refreshSource((x) => x + 1);
              }
            }}
            onLoadedData={() => {
              if (seek.current !== null && video.current) {
                video.current.currentTime = seek.current;
                seek.current = null;
              }
              if (selected > 0) video.current?.play().catch(() => {});
            }}
          />
        ) : (
          <div className="preview-empty">
            <Icon name="Play" size={40} />
            <p>
              {chunk
                ? chunk.state === "error"
                  ? "Clip failed"
                  : chunk.state === "review"
                    ? "Needs review"
                    : "Your clip is on its way"
                : "Your video starts here"}
            </p>
            <small>
              {chunk
                ? "Completed clips will appear here."
                : "Choose a look and create your first part."}
            </small>
          </div>
        )}
      </div>
      <div className="player-controls">
        <span>
          {time((chunk?.start || 0) + elapsed)}{" "}
          <span className="muted">/ {time(total)}</span>
        </span>
        <span className="muted">
          {timeline.filter((c) => c.state === "ready").length} /{" "}
          {timeline.length} segments
        </span>
      </div>
      <div className="timeline" aria-label="Video timeline">
        {timeline.length ? (
          timeline.map((c) => (
            <button
              key={c.start}
              className={`segment ${c.state} ${c.chunks.some((x) => x.id === chunk?.id) ? "selected" : ""}`}
              style={{ flex: c.duration }}
              aria-label={`${time(c.start)} to ${time(c.start + c.duration)}, ${c.state}`}
              title={`${time(c.start)} · ${c.state}`}
              onClick={() => scrub(c.start)}
            />
          ))
        ) : (
          <div className="timeline-empty" />
        )}
      </div>
      {full?.url ? (
        <a
          className="button export"
          href={full.downloadUrl || full.url}
          download="video.mp4"
        >
          Download video <Icon name="Download" />
        </a>
      ) : (
        <Button
          className="export"
          disabled={
            busy ||
            project.runs.filter((r) => r.part !== "full" && r.state === "ready")
              .length !== 2 ||
            full?.state === "assembling" ||
            full?.state === "generating"
          }
          onClick={onExport}
        >
          {full && full.state !== "error"
            ? "Assembling video…"
            : "Export full video"}
          <Icon name="Download" />
        </Button>
      )}
    </section>
  );
}
export function GenerationProgressCard({ run }: { run: Run }) {
  const groups = segments(run.chunks);
  const ready = groups.filter((c) => c.state === "ready");
  return (
    <div
      className={`progress-card ${run.state === "error" || run.state === "review" ? "error" : ""}`}
    >
      <small>
        {run.state === "ready"
          ? "Part ready"
          : run.state === "review"
            ? "Needs review"
            : run.state === "error"
              ? "Some clips failed"
              : run.state === "assembling"
                ? "Assembling"
                : "Generating"}
      </small>
      <strong>
        {ready.length} <span>of</span> {groups.length}
      </strong>
      <small>
        {time(
          run.chunks
            .filter((c) => c.state === "ready")
            .reduce((s, c) => s + c.duration, 0),
        )}{" "}
        of {time(run.chunks.reduce((s, c) => s + c.duration, 0))} ready
      </small>
    </div>
  );
}
export function ChunkStatusList({
  run,
  onRetry,
  busy,
}: {
  run: Run;
  onRetry: (ids: string[]) => void;
  busy: boolean;
}) {
  return (
    <div className="chunk-list">
      {segments(run.chunks).map((c) => (
        <div className="chunk-row" key={c.start}>
          <span>
            {time(c.start)} — {time(c.start + c.duration)}
          </span>
          {c.state === "error" ? (
            <button
              className="text-button"
              disabled={busy}
              onClick={() =>
                onRetry(
                  c.chunks.filter((x) => x.state === "error").map((x) => x.id),
                )
              }
            >
              Retry <Icon name="Chevron" size={16} />
            </button>
          ) : (
            <span className={c.state === "generating" ? "pulse" : ""}>
              {
                (
                  {
                    ready: "Ready",
                    queued: "Queued",
                    generating: "Generating…",
                    review: "Needs review",
                  } as Record<string, string>
                )[c.state]
              }
              {c.state === "ready" && <Icon name="Check" size={16} />}
            </span>
          )}
          {c.chunks.some((x) => x.error) && (
            <small>{c.chunks.find((x) => x.error)?.error}</small>
          )}
        </div>
      ))}
    </div>
  );
}
