import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Asset, Bootstrap, ImageGeneration } from "../types";
import { Button, Icon } from "./UI";

const stateLabels: Record<ImageGeneration["state"], string> = {
  queued: "Queued",
  generating: "Creating your image…",
  downloading: "Saving your image…",
  review: "Needs review",
  ready: "Ready",
  error: "Generation failed",
};
const pending = (job: ImageGeneration) =>
  ["queued", "generating", "downloading"].includes(job.state);

export function ImageStudio({
  hidden,
  boot,
  onRefresh,
  onUpload,
}: {
  hidden: boolean;
  boot: Bootstrap;
  onRefresh: () => Promise<unknown>;
  onUpload: (asset: Asset) => void;
}) {
  const settings = boot.images;
  const [prompt, setPrompt] = useState("");
  const [instructions, setInstructions] = useState("");
  const [references, setReferences] = useState<string[]>([]);
  const [ratio, setRatio] = useState("1:1");
  const [resolution, setResolution] = useState("2K");
  const [jobs, setJobs] = useState<ImageGeneration[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [enhancing, setEnhancing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [undo, setUndo] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const upload = useRef<HTMLInputElement>(null);
  const revision = useRef(0);
  const requestKey = useRef<string | null>(null);
  const uploadLock = useRef(false);
  const current = jobs.find((job) => job.id === selected) || jobs[0];
  const active = jobs.some(pending);
  const locked = uploading || submitting;

  function edit() {
    revision.current += 1;
    requestKey.current = null;
    setNotice("");
  }
  useEffect(() => {
    let alive = true;
    api<ImageGeneration[]>("images")
      .then((items) => {
        if (alive) setJobs(items);
      })
      .catch((e) => {
        if (alive) setError(e.message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    const timer = setInterval(async () => {
      try {
        const items = await api<ImageGeneration[]>("images");
        if (alive) {
          setJobs(items);
          await onRefresh();
        }
      } catch {
        /* A transient poll failure must not discard confirmed jobs. */
      }
    }, 5000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [active, onRefresh]);

  async function attach(files: FileList | File[]) {
    if (uploadLock.current || submitting) return;
    const items = Array.from(files);
    if (references.length + items.length > settings.maxReferences) {
      setError(`Attach up to ${settings.maxReferences} reference images.`);
      return;
    }
    if (
      items.some(
        (file) =>
          !["image/jpeg", "image/png", "image/webp"].includes(file.type) ||
          file.size > 30 * 1024 * 1024,
      )
    ) {
      setError("Use JPG, PNG or WebP images up to 30 MB each.");
      return;
    }
    uploadLock.current = true;
    setUploading(true);
    setError("");
    edit();
    const failures: string[] = [];
    try {
      for (const file of items) {
        try {
          const form = new FormData();
          form.append("file", file);
          form.append("kind", "image");
          const asset = await api<Asset>("assets", "POST", form);
          onUpload(asset);
          setReferences((ids) => [...ids, asset.id]);
        } catch (e) {
          failures.push(`${file.name}: ${(e as Error).message}`);
        }
      }
      if (failures.length) setError(failures.join(" "));
    } finally {
      uploadLock.current = false;
      setUploading(false);
      if (upload.current) upload.current.value = "";
    }
  }
  async function enhance() {
    const version = revision.current;
    const original = prompt;
    setEnhancing(true);
    setError("");
    setNotice("");
    try {
      const result = await api<{ prompt: string }>("images/enhance", "POST", {
        prompt,
        instructions,
        references,
        ratio,
        resolution,
      });
      if (revision.current !== version) {
        setNotice(
          "Your draft changed while DeepSeek was working. Improve again to use the latest edits.",
        );
        return;
      }
      edit();
      setUndo(original);
      setPrompt(result.prompt);
      setNotice("Full English prompt updated. Review it before generating.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setEnhancing(false);
    }
  }
  async function generate() {
    setSubmitting(true);
    setError("");
    requestKey.current ||= crypto.randomUUID();
    try {
      const job = await api<ImageGeneration>("images", "POST", {
        prompt,
        references,
        ratio,
        resolution,
        requestKey: requestKey.current,
      });
      setJobs((items) => [job, ...items.filter((item) => item.id !== job.id)]);
      setSelected(job.id);
      requestKey.current = null;
      await onRefresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }
  const price = settings.pricing;
  const insufficient = price !== null && boot.wallet.available < price;
  return (
    <main className="workspace image-workspace" hidden={hidden}>
      <header className="project-header">
        <div>
          <h1>Image studio</h1>
          <span className="image-model">Seedream 5 Pro</span>
        </div>
        {boot.simulation && (
          <span className="simulation-badge">Local simulation</span>
        )}
      </header>
      {error && (
        <div className="error-banner" role="alert">
          {error}
          <button onClick={() => setError("")}>Dismiss</button>
        </div>
      )}
      <div className="image-editor">
        <section className="image-results" aria-label="Generated images">
          <div
            className="image-preview"
            aria-busy={!!current && pending(current)}
          >
            {current?.url ? (
              <img src={current.url} alt={current.prompt} />
            ) : (
              <div className="image-empty">
                <span className="image-empty-icon">
                  <Icon name="Image" size={36} />
                </span>
                <h2>
                  {loading
                    ? "Opening your images…"
                    : current
                      ? stateLabels[current.state]
                      : "An idea, brought into focus"}
                </h2>
                <p>
                  {current
                    ? current.error ||
                      "You can keep working while your image is generated."
                    : "Describe your image, add a few references, and make it yours."}
                </p>
                {current && pending(current) && (
                  <span className="image-working" aria-hidden="true" />
                )}
              </div>
            )}
            {current?.simulation && (
              <span className="image-simulation simulation-badge">
                Simulation · placeholder image
              </span>
            )}
          </div>
          {current && (
            <div className="image-result-bar">
              <div>
                <strong>{stateLabels[current.state]}</strong>
                <small>
                  {current.ratio} · {current.resolution} · {current.size} px
                </small>
              </div>
              {current.downloadUrl && (
                <a className="button" href={current.downloadUrl} download>
                  <Icon name="Download" size={18} />
                  Download
                </a>
              )}
            </div>
          )}
          <div className="image-history-heading">
            <h2>Your images</h2>
            <span>{jobs.length}</span>
          </div>
          {jobs.length ? (
            <div className="image-history">
              {jobs.map((job) => (
                <button
                  key={job.id}
                  aria-pressed={current?.id === job.id}
                  className="image-history-item"
                  onClick={() => setSelected(job.id)}
                  title={job.prompt}
                  aria-label={`${stateLabels[job.state]}: ${job.prompt}`}
                >
                  {job.url ? (
                    <img src={job.url} alt="" />
                  ) : (
                    <Icon name="Image" />
                  )}
                  <span>
                    {job.simulation ? "Simulation" : stateLabels[job.state]}
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <p className="image-history-empty">
              Your generated images will appear here.
            </p>
          )}
          {current && (
            <details className="image-prompt-details">
              <summary>Generation prompt</summary>
              <p>{current.prompt}</p>
            </details>
          )}
        </section>
        <section
          className="image-composer"
          aria-label="Image generation settings"
        >
          <div className="image-composer-heading">
            <h2>Create an image</h2>
            <small>Seedream 5 Pro</small>
          </div>
          <fieldset disabled={locked} className="image-composer-fields">
            <section className="image-references" aria-label="Reference images">
              <div className="image-section-label">
                <span>References</span>
                <small>
                  {references.length} / {settings.maxReferences}
                </small>
              </div>
              <input
                ref={upload}
                hidden
                type="file"
                multiple
                tabIndex={-1}
                accept="image/jpeg,image/png,image/webp"
                aria-label="Upload reference images"
                onChange={(e) => {
                  if (e.target.files) void attach(e.target.files);
                }}
              />
              <button
                className={`image-dropzone ${dragging ? "is-dragging" : ""}`}
                type="button"
                disabled={references.length >= settings.maxReferences}
                onClick={() => upload.current?.click()}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragging(true);
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragging(false);
                  void attach(e.dataTransfer.files);
                }}
              >
                <Icon name="Plus" />
                <span>
                  {uploading ? "Uploading references…" : "Add reference images"}
                  <small>
                    Drop images or browse · JPG, PNG, WebP · 30 MB each
                  </small>
                </span>
              </button>
              {!!references.length && (
                <div className="image-reference-list">
                  {references.map((id, i) => {
                    const asset = boot.assets.find((item) => item.id === id);
                    return (
                      <div className="image-reference" key={id}>
                        {asset && <img src={asset.url} alt={asset.name} />}
                        <span>Image {i + 1}</span>
                        <button
                          aria-label={`Remove Image ${i + 1}`}
                          onClick={() => {
                            edit();
                            setUndo(null);
                            setReferences(references.filter((pk) => pk !== id));
                          }}
                        >
                          <Icon name="Close" size={14} />
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
              <small className="image-reference-help">
                Use “Image 1”, “Image 2” in your prompt to assign a role to each
                reference.
              </small>
            </section>
            <label className="script-field image-prompt">
              <span>Prompt</span>
              <textarea
                value={prompt}
                maxLength={6000}
                rows={7}
                placeholder="A portrait in soft window light, with the outfit from Image 1 and the setting from Image 2…"
                onChange={(e) => {
                  edit();
                  setUndo(null);
                  setPrompt(e.target.value);
                }}
              />
            </label>
            <details className="image-refinement">
              <summary>
                Notes for DeepSeek <span>Optional</span>
              </summary>
              <label className="script-field">
                <span>What would you like to refine?</span>
                <textarea
                  rows={3}
                  maxLength={2000}
                  value={instructions}
                  placeholder="Describe the mood, reference roles or changes. Any language is welcome."
                  onChange={(e) => {
                    edit();
                    setInstructions(e.target.value);
                  }}
                />
              </label>
            </details>
            <div className="image-enhance-actions">
              <Button
                disabled={
                  enhancing || !prompt.trim() || !settings.assistanceEnabled
                }
                onClick={enhance}
              >
                {enhancing ? "DeepSeek is refining…" : "Improve with DeepSeek"}
              </Button>
              {undo !== null && (
                <button
                  className="text-button"
                  onClick={() => {
                    edit();
                    setPrompt(undo);
                    setUndo(null);
                  }}
                >
                  Undo
                </button>
              )}
            </div>
            {!settings.assistanceEnabled && (
              <small>AI assistance is not configured.</small>
            )}
            {notice && (
              <p className="image-notice" role="status">
                {notice}
              </p>
            )}
            <div className="image-options">
              <label>
                <span>Aspect ratio</span>
                <select
                  value={ratio}
                  onChange={(e) => {
                    edit();
                    setRatio(e.target.value);
                  }}
                >
                  {settings.ratios.map((item) => (
                    <option key={item}>{item}</option>
                  ))}
                </select>
              </label>
              <label>
                <span>Resolution</span>
                <select
                  value={resolution}
                  onChange={(e) => {
                    edit();
                    setResolution(e.target.value);
                  }}
                >
                  {settings.resolutions.map((item) => (
                    <option key={item}>{item}</option>
                  ))}
                </select>
              </label>
            </div>
          </fieldset>
          <footer className="generate-footer image-generate-footer">
            <div>
              <span>1 image · {resolution}</span>
              <span>
                {price ? `${price} credits` : "Pricing not configured"}
              </span>
            </div>
            <Button
              primary
              disabled={
                locked ||
                enhancing ||
                !prompt.trim() ||
                !settings.generationEnabled ||
                insufficient
              }
              onClick={generate}
            >
              {submitting ? "Starting generation…" : "Generate image"}
              <Icon name="ArrowUpRight" size={18} />
            </Button>
            {!settings.generationEnabled && (
              <small>Image generation needs administrator setup.</small>
            )}
            {insufficient && <small>Not enough credits for this image.</small>}
          </footer>
        </section>
      </div>
    </main>
  );
}
