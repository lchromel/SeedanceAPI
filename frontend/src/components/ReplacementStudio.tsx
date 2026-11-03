import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type {
  Asset,
  Bootstrap,
  ImageGeneration,
  EditWorkflow,
  IdentityLock,
  ReplacementRole,
} from "../types";
import { Button, Icon } from "./UI";
import { runPromptQueue } from "../promptQueue";

const allRoles: ReplacementRole[] = [
  "model",
  "clothing",
  "makeup",
  "hairstyle",
  "location",
];
const workflowRoles: Record<EditWorkflow, ReplacementRole[]> = {
  replacement: allRoles,
  makeup: ["makeup", "hairstyle"],
  wardrobe: ["clothing"],
};
const workflowCopy = {
  replacement: {
    title: "Original photographs",
    add: "Add original photographs",
    help: "One original, one individual prompt, one separate generation.",
    empty: "Build a series from your originals",
    detail:
      "Each photograph keeps its own pose, framing and individual prompt.",
  },
  makeup: {
    title: "Original portrait",
    add: "Add a portrait",
    help: "Change makeup and hair while keeping the same face.",
    empty: "Start with a clear portrait",
    detail:
      "Add a makeup reference, a hairstyle reference, or both. The original supplies identity, expression and framing.",
  },
  wardrobe: {
    title: "Person or body sheet",
    add: "Add a person or body sheet",
    help: "Dress the same person in the referenced outfit, across every existing view.",
    empty: "Start with a full-body photo or sheet",
    detail:
      "Upload a person or multi-view sheet, then add clothing references. Pose, body proportions and face stay consistent.",
  },
};
const labels: Record<ReplacementRole, string> = {
  model: "New model",
  clothing: "Clothing",
  makeup: "Makeup",
  hairstyle: "Hairstyle",
  location: "Location",
};
const emptyRoles = (): Record<ReplacementRole, Asset | null> => ({
  model: null,
  clothing: null,
  makeup: null,
  hairstyle: null,
  location: null,
});
type Original = {
  asset: Asset;
  prompt: string;
  notes: string;
  preparedFor: string | null;
  error: string;
  jobId?: string;
  locationDescription?: string;
  locationPreparation?: string;
  descriptionFor?: string;
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
  workflow,
  hidden,
  boot,
  jobs,
  onUpload,
  onJobs,
  onRefresh,
  identity,
  onIdentity,
  onFile,
  onRegenerate,
  retrying,
}: {
  workflow: EditWorkflow;
  hidden: boolean;
  boot: Bootstrap;
  jobs: ImageGeneration[];
  onUpload: (asset: Asset) => void;
  onJobs: (jobs: ImageGeneration[]) => void;
  onRefresh: () => Promise<unknown>;
  identity: IdentityLock;
  onIdentity: (value: IdentityLock) => void;
  onFile: (id: string, file: File) => void;
  onRegenerate: (job: ImageGeneration) => Promise<ImageGeneration>;
  retrying: string[];
}) {
  const roles = workflowRoles[workflow];
  const copy = workflowCopy[workflow];
  const maxOriginals =
    workflow === "replacement" ? boot.images.maxOriginals : 1;
  const [originals, setOriginals] = useState<Original[]>([]);
  const [replacements, setReplacements] = useState(emptyRoles);
  const [wishes, setWishes] = useState("");
  const [extras, setExtras] = useState<{ asset: Asset; label: string }[]>([]);
  const [parallelism, setParallelism] = useState(3);
  const [keepTattoos, setKeepTattoos] = useState(false);
  const [locationDescriptionOnly, setLocationDescriptionOnly] = useState(false);
  const [ratio, setRatio] = useState("original");
  const [resolution, setResolution] = useState("2K");
  const [busy, setBusy] = useState<"upload" | "prepare" | "submit" | null>(
    null,
  );
  const [preparingIds, setPreparingIds] = useState<string[]>([]);
  const [progress, setProgress] = useState("");
  const [error, setError] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [dragging, setDragging] = useState(false);
  const originalInput = useRef<HTMLInputElement>(null);
  const extraInput = useRef<HTMLInputElement>(null);
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
    ...new Set([
      ...roles.map((role) => replacements[role]?.id).filter(Boolean),
      ...extras.map((item) => item.asset.id),
    ]),
  ];
  const generationOrder = replacementOrder.filter(
    (id) => !locationDescriptionOnly || id !== replacements.location?.id,
  );
  const extraPayload = extras.map((item) => ({
    asset: item.asset.id,
    label: item.label,
  }));
  const fingerprint = JSON.stringify([
    roleIds,
    wishes,
    ratio,
    resolution,
    keepTattoos,
    extraPayload,
    identity,
    locationDescriptionOnly,
  ]);
  const stamp = (row: Original) => JSON.stringify([fingerprint, row.notes]);
  const prepared = originals.filter(
    (row) =>
      row.prompt.trim() &&
      row.preparedFor === stamp(row) &&
      (!locationDescriptionOnly ||
        (!!row.locationPreparation && row.descriptionFor === stamp(row))),
  ).length;
  const locked = !!busy || submitted;
  const validReferences =
    (!locationDescriptionOnly || !!replacements.location) &&
    extras.every((item) => item.label.trim()) &&
    (workflow !== "makeup" ||
      !!replacements.makeup ||
      !!replacements.hairstyle) &&
    (workflow !== "wardrobe" || !!replacements.clothing || !!extras.length);
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
    if (alive.current) {
      onUpload(asset);
      onFile(asset.id, file);
    }
    return asset;
  }
  async function attachOriginals(input: FileList | File[]) {
    if (running.current || submitted) return;
    const files = Array.from(input);
    try {
      validateFiles(files);
      if (originals.length + files.length > maxOriginals)
        throw new Error(
          `Add up to ${maxOriginals} original${maxOriginals === 1 ? "" : "s"} in this mode.`,
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
      if (
        !replacements[role] &&
        replacementOrder.length + 1 >= boot.images.maxReferences
      )
        throw new Error(
          `Use up to ${boot.images.maxReferences - 1} replacement and extra references combined.`,
        );
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
  async function attachExtras(input: FileList) {
    if (running.current || submitted) return;
    const files = Array.from(input);
    try {
      validateFiles(files);
      if (replacementOrder.length + files.length >= boot.images.maxReferences)
        throw new Error(
          `Use up to ${boot.images.maxReferences - 1} replacement and extra references combined.`,
        );
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    running.current = true;
    setBusy("upload");
    setError("");
    edit();
    try {
      for (const file of files) {
        try {
          const asset = await upload(file);
          if (alive.current)
            setExtras((items) => [...items, { asset, label: "" }]);
        } catch (e) {
          if (alive.current) setError(`${file.name}: ${(e as Error).message}`);
        }
      }
    } finally {
      running.current = false;
      if (alive.current) setBusy(null);
      if (extraInput.current) extraInput.current.value = "";
    }
  }
  async function prepare(rows: Original[]) {
    if (running.current || submitted || !validReferences) return;
    const currentVersion = version.current;
    running.current = true;
    stop.current = false;
    setBusy("prepare");
    setError("");
    let completed = 0,
      succeeded = 0,
      failed = 0;
    const shouldStop = () =>
      stop.current || !alive.current || version.current !== currentVersion;
    try {
      await runPromptQueue(
        rows,
        parallelism,
        async (row) => {
          setPreparingIds((ids) => [...ids, row.asset.id]);
          setOriginals((current) =>
            current.map((item) =>
              item.asset.id === row.asset.id ? { ...item, error: "" } : item,
            ),
          );
          try {
            const result = await api<{
              prompt: string;
              locationDescription?: string;
              locationPreparation?: string;
            }>("images/replacements/prompt", "POST", {
              original: row.asset.id,
              replacements: roleIds,
              instructions: wishes,
              notes: row.notes,
              prompt: row.prompt,
              ratio,
              resolution,
              keepTattoos,
              locationDescriptionOnly,
              workflow,
              extras: extraPayload,
              identity,
            });
            if (!alive.current || version.current !== currentVersion) return;
            setOriginals((current) =>
              current.map((item) =>
                item.asset.id === row.asset.id
                  ? {
                      ...item,
                      prompt: result.prompt,
                      preparedFor: stamp(row),
                      locationDescription: result.locationDescription,
                      locationPreparation: result.locationPreparation,
                      descriptionFor: result.locationPreparation
                        ? stamp(row)
                        : undefined,
                      error: "",
                    }
                  : item,
              ),
            );
            succeeded += 1;
          } catch (e) {
            failed += 1;
            if (alive.current)
              setOriginals((current) =>
                current.map((item) =>
                  item.asset.id === row.asset.id
                    ? {
                        ...item,
                        error: (e as Error).message,
                        preparedFor: null,
                      }
                    : item,
                ),
              );
          } finally {
            completed += 1;
            if (alive.current) {
              setPreparingIds((ids) => ids.filter((id) => id !== row.asset.id));
              setProgress(
                `${completed} / ${rows.length} finished · ${succeeded} ready · ${failed} failed`,
              );
            }
          }
        },
        shouldStop,
      );
      if (alive.current)
        setProgress(
          `${succeeded} prompts ready, ${failed} failed${completed < rows.length ? `, ${rows.length - completed} not started` : ""}. Review each before generating.`,
        );
    } finally {
      running.current = false;
      if (alive.current) {
        setBusy(null);
        setPreparingIds([]);
      }
    }
  }
  async function generate() {
    if (
      running.current ||
      submitted ||
      !originals.length ||
      prepared !== originals.length ||
      !validReferences
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
            locationDescription: row.locationDescription || "",
            locationPreparation: row.locationPreparation || "",
          })),
          replacements: roleIds,
          instructions: wishes,
          ratio,
          resolution,
          keepTattoos,
          locationDescriptionOnly,
          workflow,
          extras: extraPayload,
          identity,
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
    setExtras([]);
    setWishes("");
    setKeepTattoos(false);
    setLocationDescriptionOnly(false);
    setSubmitted(false);
    setError("");
  }
  async function regenerate(job: ImageGeneration, rowId?: string) {
    setError("");
    try {
      const created = await onRegenerate(job);
      if (alive.current && rowId)
        setOriginals((rows) =>
          rows.map((row) =>
            row.asset.id === rowId ? { ...row, jobId: created.id } : row,
          ),
        );
    } catch (e) {
      if (alive.current) setError((e as Error).message);
    }
  }
  const past = jobs.filter(
    (job) =>
      job.mode === workflow && !originals.some((row) => row.jobId === job.id),
  );
  return (
    <div className="replacement-workbench" hidden={hidden}>
      <section
        className="replacement-originals"
        aria-label={`${copy.title} and individual prompts`}
      >
        <header className="replacement-heading">
          <div>
            <small>PART 1</small>
            <h2>{copy.title}</h2>
          </div>
          <span>
            {originals.length} / {maxOriginals}
          </span>
        </header>
        <p className="replacement-help">{copy.help}</p>
        <input
          ref={originalInput}
          hidden
          type="file"
          multiple={workflow === "replacement"}
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
              {busy === "upload" ? "Uploading…" : copy.add}
              <small>
                {workflow === "replacement"
                  ? "Drop multiple photos or browse"
                  : "Drop one image or browse"}{" "}
                · JPG, PNG, WebP · 30 MB each
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
            const ready =
              row.prompt.trim() &&
              row.preparedFor === stamp(row) &&
              (!locationDescriptionOnly || row.descriptionFor === stamp(row));
            const sourceCleared =
              submitted && boot.images.storage === "temporary";
            return (
              <article
                className="replacement-original"
                key={row.asset.id}
                aria-busy={preparingIds.includes(row.asset.id)}
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
                      : preparingIds.includes(row.asset.id)
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
                                    preparedFor:
                                      !locationDescriptionOnly ||
                                      item.descriptionFor === stamp(item)
                                        ? stamp(item)
                                        : null,
                                    error: "",
                                  }
                                : item,
                            ),
                          );
                        }}
                      />
                    </label>
                    {locationDescriptionOnly && row.locationDescription && (
                      <details className="image-prompt-details">
                        <summary>Observed location details</summary>
                        <p>{row.locationDescription}</p>
                        <small>
                          The individual prompt adapts these observations to
                          this photo. The location image is excluded from
                          generation.
                        </small>
                      </details>
                    )}
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
                          disabled={
                            !!busy ||
                            !validReferences ||
                            !boot.images.assistanceEnabled
                          }
                          onClick={() => prepare([row])}
                        >
                          {row.error
                            ? "Retry writing prompt"
                            : "Improve this prompt"}
                        </Button>
                      )}
                      {job?.downloadUrl && (
                        <a className="button" href={job.downloadUrl} download>
                          <Icon name="Download" size={18} />
                          Download
                        </a>
                      )}
                      {job &&
                        ["ready", "error", "expired"].includes(job.state) && (
                          <Button
                            disabled={
                              retrying.includes(job.id) ||
                              !boot.images.generationEnabled
                            }
                            onClick={() => void regenerate(job, row.asset.id)}
                          >
                            {retrying.includes(job.id)
                              ? "Starting…"
                              : "Regenerate this image"}
                          </Button>
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
            <h3>{copy.empty}</h3>
            <p>{copy.detail}</p>
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
                  {["ready", "error", "expired"].includes(job.state) && (
                    <Button
                      disabled={
                        retrying.includes(job.id) ||
                        !boot.images.generationEnabled
                      }
                      onClick={() => void regenerate(job)}
                    >
                      {retrying.includes(job.id)
                        ? "Starting…"
                        : "Regenerate image"}
                    </Button>
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
            <h2>
              {workflow === "makeup"
                ? "Makeup & hairstyle"
                : workflow === "wardrobe"
                  ? "The outfit"
                  : "What changes?"}
            </h2>
          </div>
        </div>
        <p className="replacement-help">
          {workflow === "makeup"
            ? "The portrait supplies identity. Empty makeup or hair slots preserve the original."
            : workflow === "wardrobe"
              ? "Use a main clothing reference and add more garments below. The original supplies the person and pose."
              : "Shared across the series. Leave a reference empty to keep that aspect from each original."}
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
                    {role === "location" && locationDescriptionOnly
                      ? "Description only"
                      : `Image ${generationOrder.indexOf(replacements[role]!.id) + 2}`}
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
                    if (role === "location") setLocationDescriptionOnly(false);
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
        {workflow === "replacement" && (
          <label className="replacement-checkbox">
            <input
              type="checkbox"
              checked={locationDescriptionOnly}
              disabled={locked || !replacements.location}
              onChange={(e) => {
                edit();
                setLocationDescriptionOnly(e.target.checked);
              }}
            />
            <span>
              <strong>Use location for description only</strong>
              <small>
                {replacements.location
                  ? "Describe the location in detail for each photo's composition, then generate using the text without the location image. Prepare prompts after changing this."
                  : "Add a location reference first. Its description can guide the scene without sending the image to generation."}
              </small>
            </span>
          </label>
        )}
        <section className="replacement-extras" aria-label="Extra references">
          <div className="image-section-label">
            <span>
              {workflow === "wardrobe"
                ? "More clothing references"
                : "Extra references"}
            </span>
            <small>
              {replacementOrder.length + 1} / {boot.images.maxReferences}{" "}
              {locationDescriptionOnly
                ? "images analyzed"
                : "images per original"}
            </small>
          </div>
          <p className="replacement-help">
            {workflow === "makeup"
              ? "Describe the cosmetic or hair detail to use from each extra reference."
              : "Give each reference a role, such as shoes, earrings or a fabric detail."}
          </p>
          <input
            ref={extraInput}
            hidden
            type="file"
            multiple
            accept="image/jpeg,image/png,image/webp"
            onChange={(e) => {
              if (e.target.files) void attachExtras(e.target.files);
            }}
          />
          {extras.map((item, index) => (
            <div className="replacement-extra" key={item.asset.id}>
              {!submitted && <img src={item.asset.url} alt={item.asset.name} />}
              <label className="script-field">
                <span>
                  Image {generationOrder.indexOf(item.asset.id) + 2} · Reference
                  role
                </span>
                <input
                  value={item.label}
                  disabled={locked}
                  maxLength={160}
                  placeholder="Describe what to use from this image"
                  onChange={(e) => {
                    edit();
                    setExtras((items) =>
                      items.map((extra, i) =>
                        i === index
                          ? { ...extra, label: e.target.value }
                          : extra,
                      ),
                    );
                  }}
                />
              </label>
              {!submitted && (
                <button
                  className="icon-button"
                  disabled={!!busy}
                  aria-label={`Remove extra reference ${index + 1}`}
                  onClick={() => {
                    edit();
                    setExtras((items) => items.filter((_, i) => i !== index));
                  }}
                >
                  <Icon name="Close" size={16} />
                </button>
              )}
            </div>
          ))}
          {!submitted && (
            <Button
              disabled={
                !!busy ||
                replacementOrder.length >= boot.images.maxReferences - 1
              }
              onClick={() => extraInput.current?.click()}
            >
              <Icon name="Plus" size={16} />
              Add extra references
            </Button>
          )}
          {extras.some((item) => !item.label.trim()) && (
            <small className="image-notice">
              Describe each extra reference before preparing or generating.
            </small>
          )}
        </section>
        <fieldset className="replacement-identity" disabled={locked}>
          <legend>Fixed face details</legend>
          <p className="replacement-help">
            Shared across editing modes and saved in this browser for your
            account.
          </p>
          <label className="script-field">
            <span>Eye color</span>
            <select
              value={identity.eyeColor}
              onChange={(e) => {
                edit();
                onIdentity({ ...identity, eyeColor: e.target.value });
              }}
            >
              {["blue", "green", "brown", "hazel", "gray", "amber"].map(
                (value) => (
                  <option key={value} value={value}>
                    {value[0].toUpperCase() + value.slice(1)}
                  </option>
                ),
              )}
            </select>
          </label>
          <label className="script-field">
            <span>Other fixed features · English</span>
            <textarea
              rows={2}
              maxLength={600}
              value={identity.details}
              placeholder="For example: freckles across the nose, a small mole under the left eye."
              onChange={(e) => {
                edit();
                onIdentity({ ...identity, details: e.target.value });
              }}
            />
          </label>
        </fieldset>
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
        {workflow === "replacement" && (
          <label className="script-field replacement-parallelism">
            <span>Prompts at the same time</span>
            <select
              disabled={locked}
              value={parallelism}
              onChange={(e) => setParallelism(Number(e.target.value))}
            >
              {[1, 2, 3, 4].map((value) => (
                <option key={value} value={value}>
                  {value}
                  {value === 3 ? " · Default" : ""}
                </option>
              ))}
            </select>
          </label>
        )}
        {boot.images.storage === "temporary" && (
          <p className="image-notice">
            References are deleted after generation. Download each result within{" "}
            {Math.ceil(boot.images.downloadTtlSeconds / 60)} min. Regeneration
            can reuse files from this open tab. After reloading, attach them
            again.
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
              {workflow === "replacement" ? "New series" : "New draft"}
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
                  !!busy ||
                  !originals.length ||
                  !validReferences ||
                  !boot.images.assistanceEnabled
                }
                onClick={() => prepare(originals)}
              >
                Prepare individual prompts
              </Button>
              {originals.some((row) => row.error) && (
                <Button
                  disabled={
                    !!busy || !validReferences || !boot.images.assistanceEnabled
                  }
                  onClick={() => prepare(originals.filter((row) => row.error))}
                >
                  Retry failed prompts
                </Button>
              )}
              {busy === "prepare" && (
                <button
                  className="text-button"
                  onClick={() => {
                    stop.current = true;
                  }}
                >
                  Stop queued prompts
                </button>
              )}
              <Button
                primary
                onClick={generate}
                disabled={
                  !!busy ||
                  !originals.length ||
                  prepared !== originals.length ||
                  !validReferences ||
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
                  {locationDescriptionOnly
                    ? "DeepSeek is not configured. Location descriptions require prompt preparation before generating."
                    : "DeepSeek is not configured. You can write individual English prompts manually."}
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
