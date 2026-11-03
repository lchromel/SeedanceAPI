import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type {
  Asset,
  Bootstrap,
  ImageGeneration,
  ReplacementRole,
} from "../types";
import { Button, Icon } from "./UI";

const roles: ReplacementRole[] = ["model", "clothing", "makeup", "location"];
const labels: Record<ReplacementRole, string> = {
  model: "New model",
  clothing: "Clothing",
  makeup: "Makeup",
  location: "Location",
};
const emptyRoles = (): Record<ReplacementRole, Asset | null> => ({
  model: null,
  clothing: null,
  makeup: null,
  location: null,
});
type Original = {
  asset: Asset;
  prompt: string;
  notes: string;
  preparedFor: string | null;
  error: string;
  jobId?: string;
};
const statusLabels: Record<ImageGeneration["state"], string> = {
  queued: "Queued",
  generating: "Generating…",
  downloading: "Saving…",
  ready: "Ready",
  error: "Generation failed",
  review: "Needs review",
  expired: "Image deleted",
};

export function ReplacementStudio({
  hidden,
  boot,
  jobs,
  onUpload,
  onJobs,
  onRefresh,
}: {
  hidden: boolean;
  boot: Bootstrap;
  jobs: ImageGeneration[];
  onUpload: (asset: Asset) => void;
  onJobs: (jobs: ImageGeneration[]) => void;
  onRefresh: () => Promise<unknown>;
}) {
  const [originals, setOriginals] = useState<Original[]>([]);
  const [replacements, setReplacements] = useState(emptyRoles);
  const [wishes, setWishes] = useState("");
  const [keepTattoos, setKeepTattoos] = useState(false);
  const [ratio, setRatio] = useState("original");
  const [resolution, setResolution] = useState("2K");
  const [busy, setBusy] = useState<"upload" | "prepare" | "submit" | null>(
    null,
  );
  const [preparingId, setPreparingId] = useState<string | null>(null);
  const [progress, setProgress] = useState("");
  const [error, setError] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [dragging, setDragging] = useState(false);
  const originalInput = useRef<HTMLInputElement>(null);
  const roleInputs = useRef<
    Partial<Record<ReplacementRole, HTMLInputElement | null>>
  >({});
  const requestKey = useRef<string | null>(null);
  const version = useRef(0);
  const running = useRef(false);
  const stop = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      stop.current = true;
    };
  }, []);
  const roleIds = Object.fromEntries(
    roles.map((role) => [role, replacements[role]?.id || null]),
  );
  const replacementOrder = [
    ...new Set(roles.map((role) => replacements[role]?.id).filter(Boolean)),
  ];
  const fingerprint = JSON.stringify([
    roleIds,
    wishes,
    ratio,
    resolution,
    keepTattoos,
  ]);
  const stamp = (row: Original) => JSON.stringify([fingerprint, row.notes]);
  const prepared = originals.filter(
    (row) => row.prompt.trim() && row.preparedFor === stamp(row),
  ).length;
  const locked = !!busy || submitted;
  const price = boot.images.pricing;
  const insufficient =
    boot.creditsEnabled &&
    price !== null &&
    originals.length * price > boot.wallet.available;
  function edit() {
    version.current += 1;
    requestKey.current = null;
    setProgress("");
  }
  function validateFiles(files: File[]) {
    if (
      files.some(
        (file) =>
          !["image/jpeg", "image/png", "image/webp"].includes(file.type) ||
          file.size > 30 * 1024 * 1024,
      )
    ) {
      throw new Error("Use JPG, PNG or WebP images up to 30 MB each.");
    }
  }
  async function upload(file: File) {
    const form = new FormData();
    form.append("kind", "image");
    form.append("file", file);
    const asset = await api<Asset>("assets", "POST", form);
    if (alive.current) onUpload(asset);
    return asset;
  }
  async function attachOriginals(input: FileList | File[]) {
    if (running.current || submitted) return;
    const files = Array.from(input);
    try {
      validateFiles(files);
      if (originals.length + files.length > boot.images.maxOriginals)
        throw new Error(
          `Add up to ${boot.images.maxOriginals} originals in one series.`,
        );
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    running.current = true;
    setBusy("upload");
    setError("");
    edit();
    const failures: string[] = [];
    try {
      for (const file of files) {
        if (!alive.current) break;
        try {
          const asset = await upload(file);
          if (alive.current)
            setOriginals((rows) => [
              ...rows,
              { asset, prompt: "", notes: "", preparedFor: null, error: "" },
            ]);
        } catch (e) {
          failures.push(`${file.name}: ${(e as Error).message}`);
        }
      }
      if (alive.current && failures.length) setError(failures.join(" "));
    } finally {
      running.current = false;
      if (alive.current) setBusy(null);
      if (originalInput.current) originalInput.current.value = "";
    }
  }
  async function attachRole(role: ReplacementRole, file: File) {
    if (running.current || submitted) return;
    try {
      validateFiles([file]);
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    running.current = true;
    setBusy("upload");
    setError("");
    edit();
    try {
      const asset = await upload(file);
      if (alive.current)
        setReplacements((current) => ({ ...current, [role]: asset }));
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      running.current = false;
      if (alive.current) setBusy(null);
      const input = roleInputs.current[role];
      if (input) input.value = "";
    }
  }
  async function prepare(rows: Original[]) {
    if (running.current || submitted) return;
    const currentVersion = version.current;
    running.current = true;
    stop.current = false;
    setBusy("prepare");
    setError("");
    let completed = 0;
    try {
      for (const [i, row] of rows.entries()) {
        if (
          stop.current ||
          !alive.current ||
          version.current !== currentVersion
        )
          break;
        setPreparingId(row.asset.id);
        setProgress(`Preparing prompt ${i + 1} of ${rows.length}…`);
        try {
          const result = await api<{ prompt: string }>(
            "images/replacements/prompt",
            "POST",
            {
              original: row.asset.id,
              replacements: roleIds,
              instructions: wishes,
              notes: row.notes,
              prompt: row.prompt,
              ratio,
              resolution,
              keepTattoos,
            },
          );
          if (!alive.current || version.current !== currentVersion) break;
          setOriginals((current) =>
            current.map((item) =>
              item.asset.id === row.asset.id
                ? {
                    ...item,
                    prompt: result.prompt,
                    preparedFor: stamp(row),
                    error: "",
                  }
                : item,
            ),
          );
          completed += 1;
        } catch (e) {
          if (alive.current)
            setOriginals((current) =>
              current.map((item) =>
                item.asset.id === row.asset.id
                  ? { ...item, error: (e as Error).message }
                  : item,
              ),
            );
        }
      }
      if (alive.current)
        setProgress(
          `${completed} individual prompt${completed === 1 ? "" : "s"} prepared. Review each before generating.`,
        );
    } finally {
      running.current = false;
      if (alive.current) {
        setBusy(null);
        setPreparingId(null);
      }
    }
  }
  async function generate() {
    if (
      running.current ||
      submitted ||
      !originals.length ||
      prepared !== originals.length
    )
      return;
    running.current = true;
    setBusy("submit");
    setError("");
    requestKey.current ||= crypto.randomUUID();
    try {
      const result = await api<ImageGeneration[]>(
        "images/replacements/generate",
        "POST",
        {
          originals: originals.map((row) => ({
            asset: row.asset.id,
            prompt: row.prompt,
            notes: row.notes,
          })),
          replacements: roleIds,
          instructions: wishes,
          ratio,
          resolution,
          keepTattoos,
          requestKey: requestKey.current,
        },
      );
      if (!alive.current) return;
      onJobs(result);
      setOriginals((rows) =>
        rows.map((row) => ({
          ...row,
          jobId: result.find((job) => job.originalId === row.asset.id)?.id,
        })),
      );
      setSubmitted(true);
      requestKey.current = null;
      setProgress(
        `${result.length} separate generation${result.length === 1 ? "" : "s"} queued.`,
      );
      await onRefresh();
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    } finally {
      running.current = false;
      if (alive.current) setBusy(null);
    }
  }
  function reset() {
    edit();
    setOriginals([]);
    setReplacements(emptyRoles());
    setWishes("");
    setKeepTattoos(false);
    setSubmitted(false);
    setError("");
  }
  const past = jobs.filter(
    (job) =>
      job.mode === "replacement" &&
      !originals.some((row) => row.jobId === job.id),
  );
  return (
    <div className="replacement-workbench" hidden={hidden}>
      <section
        className="replacement-originals"
        aria-label="Original photographs and individual prompts"
      >
        <header className="replacement-heading">
          <div>
            <small>PART 1</small>
            <h2>Original photographs</h2>
          </div>
          <span>
            {originals.length} / {boot.images.maxOriginals}
          </span>
        </header>
        <p className="replacement-help">
          One original, one individual prompt, one separate generation.
        </p>
        <input
          ref={originalInput}
          hidden
          type="file"
          multiple
          accept="image/jpeg,image/png,image/webp"
          onChange={(e) => {
            if (e.target.files) void attachOriginals(e.target.files);
          }}
        />
        {!submitted && (
          <button
            className={`image-dropzone ${dragging ? "is-dragging" : ""}`}
            disabled={!!busy}
            onClick={() => originalInput.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              void attachOriginals(e.dataTransfer.files);
            }}
          >
            <Icon name="Plus" />
            <span>
              {busy === "upload" ? "Uploading…" : "Add original photographs"}
              <small>
                Drop multiple photos or browse · JPG, PNG, WebP · 30 MB each
              </small>
            </span>
          </button>
        )}
        {error && (
          <div className="error-banner" role="alert">
            {error}
            <button onClick={() => setError("")}>Dismiss</button>
          </div>
        )}
        <div className="replacement-list">
          {originals.map((row, index) => {
            const job = jobs.find((item) => item.id === row.jobId);
            const ready = row.prompt.trim() && row.preparedFor === stamp(row);
            const sourceCleared =
              submitted && boot.images.storage === "temporary";
            return (
              <article
                className="replacement-original"
                key={row.asset.id}
                aria-busy={preparingId === row.asset.id}
              >
                <header>
                  <div>
                    <span>{String(index + 1).padStart(2, "0")}</span>
                    <h3>{row.asset.name}</h3>
                  </div>
                  {!submitted && (
                    <button
                      disabled={!!busy}
                      className="icon-button"
                      aria-label={`Remove original ${index + 1}`}
                      onClick={() => {
                        edit();
                        setOriginals(
                          originals.filter(
                            (item) => item.asset.id !== row.asset.id,
                          ),
                        );
                      }}
                    >
                      <Icon name="Close" size={18} />
                    </button>
                  )}
                  <small>
                    {job
                      ? statusLabels[job.state]
                      : preparingId === row.asset.id
                        ? "Writing prompt…"
                        : ready
                          ? "Prompt ready"
                          : row.prompt
                            ? "Update prompt"
                            : "Needs a prompt"}
                  </small>
                </header>
                <div className="replacement-original-body">
                  <div className="replacement-photo">
                    {job?.url ? (
                      <img src={job.url} alt={`Result for ${row.asset.name}`} />
                    ) : sourceCleared ? (
                      <Icon name="Image" size={32} />
                    ) : (
                      <img
                        src={row.asset.url}
                        alt={`Original ${index + 1}: ${row.asset.name}`}
                      />
                    )}
                    <small>
                      {job?.url
                        ? "Result"
                        : sourceCleared
                          ? "Original cleared after generation"
                          : "Image 1 · Original"}
                    </small>
                  </div>
                  <div className="replacement-draft">
                    <label className="script-field">
                      <span>Individual prompt</span>
                      <textarea
                        rows={5}
                        maxLength={6000}
                        value={row.prompt}
                        disabled={locked}
                        placeholder="Prepare with DeepSeek, or write the complete English prompt for this photograph."
                        onChange={(e) => {
                          edit();
                          setOriginals((rows) =>
                            rows.map((item) =>
                              item.asset.id === row.asset.id
                                ? {
                                    ...item,
                                    prompt: e.target.value,
                                    preparedFor: stamp(item),
                                    error: "",
                                  }
                                : item,
                            ),
                          );
                        }}
                      />
                    </label>
                    {!submitted && (
                      <details className="image-refinement">
                        <summary>
                          Wishes for this photo <span>Optional</span>
                        </summary>
                        <label className="script-field">
                          <span>Individual wishes</span>
                          <textarea
                            rows={2}
                            maxLength={2000}
                            disabled={!!busy}
                            value={row.notes}
                            placeholder="Only for this original: refine the expression, light or details."
                            onChange={(e) => {
                              edit();
                              setOriginals((rows) =>
                                rows.map((item) =>
                                  item.asset.id === row.asset.id
                                    ? { ...item, notes: e.target.value }
                                    : item,
                                ),
                              );
                            }}
                          />
                        </label>
                      </details>
                    )}
                    {row.error && (
                      <p className="error-message" role="alert">
                        {row.error}
                      </p>
                    )}
                    {job?.error && <p className="image-notice">{job.error}</p>}
                    <div className="replacement-row-actions">
                      {!submitted && (
                        <Button
                          disabled={!!busy || !boot.images.assistanceEnabled}
                          onClick={() => prepare([row])}
                        >
                          Improve this prompt
                        </Button>
                      )}
                      {job?.downloadUrl && (
                        <a className="button" href={job.downloadUrl} download>
                          <Icon name="Download" size={18} />
                          Download
                        </a>
                      )}
                      {job?.expiresAt && job.state === "ready" && (
                        <small>
                          Download before{" "}
                          {new Date(job.expiresAt).toLocaleTimeString([], {
                            hour: "2-digit",
                            minute: "2-digit",
                          })}
                        </small>
                      )}
                      {job?.simulation && <small>Local simulation</small>}
                    </div>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
        {!originals.length && (
          <div className="replacement-empty">
            <Icon name="Image" size={36} />
            <h3>Build a series from your originals</h3>
            <p>
              Each photograph keeps its own pose, framing and individual prompt.
            </p>
          </div>
        )}
        {!!past.length && (
          <section
            className="replacement-past"
            aria-label="Previous replacement results"
          >
            <h2>Recent results</h2>
            <div className="replacement-result-grid">
              {past.map((job) => (
                <article key={job.id}>
                  {job.url ? (
                    <img
                      src={job.url}
                      alt={job.originalName || "Replacement result"}
                    />
                  ) : (
                    <Icon name="Image" size={28} />
                  )}
                  <strong>{job.originalName || "Original photograph"}</strong>
                  <small>{statusLabels[job.state]}</small>
                  {job.downloadUrl && (
                    <a className="button" href={job.downloadUrl} download>
                      Download
                    </a>
                  )}
                  <details className="image-prompt-details">
                    <summary>Individual prompt</summary>
                    <p>{job.prompt}</p>
                  </details>
                </article>
              ))}
            </div>
          </section>
        )}
      </section>
      <aside
        className="image-composer replacement-settings"
        aria-label="Shared replacement references"
      >
        <div className="replacement-heading">
          <div>
            <small>PART 2</small>
            <h2>What changes?</h2>
          </div>
        </div>
        <p className="replacement-help">
          Shared across the series. Leave a reference empty to keep that aspect
          from each original.
        </p>
        <div className="replacement-slots">
          {roles.map((role) => (
            <div className="replacement-slot" key={role}>
              <input
                hidden
                type="file"
                accept="image/jpeg,image/png,image/webp"
                ref={(element) => {
                  roleInputs.current[role] = element;
                }}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) void attachRole(role, file);
                }}
              />
              <button
                className="replacement-slot-upload"
                disabled={locked}
                onClick={() => roleInputs.current[role]?.click()}
                aria-label={`Upload ${labels[role].toLowerCase()} reference`}
              >
                {replacements[role] && !submitted ? (
                  <img src={replacements[role]!.url} alt={labels[role]} />
                ) : (
                  <Icon
                    name={replacements[role] ? "Check" : "Plus"}
                    size={24}
                  />
                )}
                <strong>{labels[role]}</strong>
                {replacements[role] && !submitted && (
                  <small>
                    Image {replacementOrder.indexOf(replacements[role]!.id) + 2}
                  </small>
                )}
                <small>
                  {replacements[role]
                    ? submitted
                      ? "Used for this series"
                      : replacements[role]!.name
                    : "Keep from original"}
                </small>
              </button>
              {replacements[role] && !submitted && (
                <button
                  className="replacement-remove"
                  disabled={!!busy}
                  aria-label={`Remove ${labels[role].toLowerCase()} reference`}
                  onClick={() => {
                    edit();
                    setReplacements((current) => ({
                      ...current,
                      [role]: null,
                    }));
                  }}
                >
                  <Icon name="Close" size={16} />
                </button>
              )}
            </div>
          ))}
        </div>
        <label className="replacement-checkbox">
          <input
            type="checkbox"
            checked={keepTattoos}
            disabled={locked}
            onChange={(e) => {
              edit();
              setKeepTattoos(e.target.checked);
            }}
          />
          <span>
            <strong>Keep tattoos</strong>
            <small>
              {keepTattoos
                ? "Keep visible tattoos from the new model, or the original if no model is added."
                : "Remove tattoos from visible skin in every photo."}
            </small>
          </span>
        </label>
        <label className="script-field">
          <span>General wishes</span>
          <textarea
            rows={4}
            maxLength={2000}
            value={wishes}
            disabled={locked}
            placeholder="What else should change or improve? You can write in any language."
            onChange={(e) => {
              edit();
              setWishes(e.target.value);
            }}
          />
        </label>
        <div className="image-options">
          <label>
            <span>Aspect ratio</span>
            <select
              disabled={locked}
              value={ratio}
              onChange={(e) => {
                edit();
                setRatio(e.target.value);
              }}
            >
              <option value="original">Keep each original</option>
              {boot.images.ratios.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Resolution</span>
            <select
              disabled={locked}
              value={resolution}
              onChange={(e) => {
                edit();
                setResolution(e.target.value);
              }}
            >
              {boot.images.resolutions.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
        </div>
        {boot.images.storage === "temporary" && (
          <p className="image-notice">
            References are deleted after generation. Download each result within{" "}
            {Math.ceil(boot.images.downloadTtlSeconds / 60)} min.
          </p>
        )}
        {progress && (
          <p className="image-notice" role="status">
            {progress}
          </p>
        )}
        <footer className="generate-footer">
          {submitted ? (
            <Button onClick={reset} disabled={!!busy}>
              New series
            </Button>
          ) : (
            <>
              <div>
                <span>
                  {prepared} / {originals.length} prompts ready
                </span>
                <span>
                  {boot.creditsEnabled && price
                    ? `${originals.length * price} credits`
                    : `${originals.length} separate images`}
                </span>
              </div>
              <Button
                disabled={
                  !!busy || !originals.length || !boot.images.assistanceEnabled
                }
                onClick={() => prepare(originals)}
              >
                Prepare individual prompts
              </Button>
              {busy === "prepare" && (
                <button
                  className="text-button"
                  onClick={() => {
                    stop.current = true;
                  }}
                >
                  Stop after this photo
                </button>
              )}
              <Button
                primary
                onClick={generate}
                disabled={
                  !!busy ||
                  !originals.length ||
                  prepared !== originals.length ||
                  !boot.images.generationEnabled ||
                  insufficient
                }
              >
                {busy === "submit"
                  ? "Starting series…"
                  : `Generate ${originals.length || "the"} image${originals.length === 1 ? "" : "s"}`}
                <Icon name="ArrowUpRight" size={18} />
              </Button>
              {!boot.images.assistanceEnabled && (
                <small>
                  DeepSeek is not configured. You can write individual English
                  prompts manually.
                </small>
              )}
              {!boot.images.generationEnabled && (
                <small>Image generation needs administrator setup.</small>
              )}
              {insufficient && (
                <small>Not enough credits for this series.</small>
              )}
            </>
          )}
        </footer>
      </aside>
    </div>
  );
}
