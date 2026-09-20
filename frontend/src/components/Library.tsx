import { useEffect, useRef, useState } from "react";
import type { Asset, Kind } from "../types";
import { api, time } from "../api";
import { Button, Icon, IconButton, Modal } from "./UI";
export function Library({
  kind,
  assets,
  selected,
  onClose,
  onSelect,
  onUpload,
  onSync,
  onUpdate,
  categories,
}: {
  kind: Kind;
  assets: Asset[];
  selected: string[];
  onClose: () => void;
  onSelect: (asset: Asset) => void;
  onUpload: (asset: Asset) => void;
  onSync: (assets: Asset[]) => void;
  onUpdate: (asset: Asset) => void;
  categories: Record<string, string>;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const needsAnalysis = kind === "clothing" || kind === "location";
  const [busy, setBusy] = useState(kind === "character");
  const [error, setError] = useState("");
  const [characters, setCharacters] = useState<Asset[]>([]);
  const syncCallback = useRef(onSync);
  syncCallback.current = onSync;
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    if (kind !== "character") return;
    let cancelled = false;
    setBusy(true);
    setError("");
    api<Asset[]>("characters/sync", "POST", {})
      .then((items) => {
        if (cancelled) return;
        setCharacters(items);
        syncCallback.current(items);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [kind, refresh]);
  const visible =
    kind === "character" ? characters : assets.filter((a) => a.kind === kind);
  async function upload(file?: File) {
    if (!file) return;
    setBusy(true);
    setError("");
    try {
      const data = new FormData();
      data.append("file", file);
      data.append("kind", kind);
      const asset = await api<Asset>("assets", "POST", data);
      onUpload(asset);
      if (asset.warning) setError(asset.warning);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }
  async function analyze(asset: Asset, selectedCategory: string) {
    setBusy(true);
    setError("");
    try {
      onUpdate(
        await api<Asset>(`assets/${asset.id}/analyze`, "POST", {
          category: selectedCategory,
        }),
      );
    } catch (e) {
      onUpdate({
        ...asset,
        category: selectedCategory,
        analysisStatus: "failed",
      });
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={`${kind.charAt(0).toUpperCase() + kind.slice(1)} library`}
      onClose={onClose}
    >
      <div className="library-toolbar">
        <span className="muted">
          {kind === "character"
            ? "BytePlus Assets"
            : kind === "motion"
              ? "Video references · 4–120 seconds"
              : "Your private image library"}
        </span>
        {kind === "character" ? (
          <Button onClick={() => setRefresh((n) => n + 1)} disabled={busy}>
            {busy ? "Syncing…" : "Refresh"}
          </Button>
        ) : (
          <Button onClick={() => input.current?.click()} disabled={busy}>
            {busy ? (needsAnalysis ? "Analyzing…" : "Uploading…") : "Upload"}
            <Icon name="Plus" size={18} />
          </Button>
        )}
        <input
          ref={input}
          hidden
          type="file"
          accept={
            kind === "motion"
              ? "video/mp4,video/quicktime"
              : "image/jpeg,image/png,image/webp"
          }
          onChange={(e) => upload(e.target.files?.[0])}
        />
      </div>
      {error && (
        <p role="alert" className="error-message">
          {error}
        </p>
      )}
      <div className="library-grid">
        {visible.map((a) => (
          <div className="library-entry" key={a.id}>
            <button
              className={`library-asset ${selected.includes(a.id) ? "chosen" : ""}`}
              disabled={
                busy ||
                (kind === "character" && (!!error || a.status !== "Active")) ||
                (needsAnalysis && a.analysisStatus !== "ready")
              }
              onClick={() => onSelect(a)}
            >
              {kind === "motion" ? (
                <video src={a.url} muted playsInline preload="metadata" />
              ) : (
                <AssetPreview key={`${a.id}-${refresh}`} url={a.url} />
              )}
              <span>{a.name}</span>
              {kind === "character" && a.status !== "Active" && (
                <small>{a.status}</small>
              )}
              {kind === "motion" && <small>{time(a.duration)} max</small>}
              {needsAnalysis && (
                <small>
                  {categories[a.category || ""] || "Not identified"}
                  {a.analysisStatus !== "ready" ? " · Needs analysis" : ""}
                </small>
              )}
              {selected.includes(a.id) && (
                <span className="selected-check">
                  <Icon name="Check" />
                </span>
              )}
            </button>
            {needsAnalysis && (
              <>
                <div className="asset-edit-trigger">
                  <IconButton
                    icon="Edit"
                    label={`Edit ${a.name}`}
                    onClick={() => setEditing(editing === a.id ? null : a.id)}
                  />
                </div>
                {editing === a.id && (
                  <div className="asset-details">
                    <AssetDetails
                      key={`${a.id}-${a.name}-${a.category}-${a.description}`}
                      asset={a}
                      categories={categories}
                      busy={busy}
                      onAnalyze={analyze}
                      onSave={async (changes) => {
                        setBusy(true);
                        setError("");
                        try {
                          onUpdate(
                            await api<Asset>(
                              `assets/${a.id}`,
                              "PATCH",
                              changes,
                            ),
                          );
                          setEditing(null);
                        } catch (e) {
                          setError((e as Error).message);
                        } finally {
                          setBusy(false);
                        }
                      }}
                    />
                  </div>
                )}
              </>
            )}
          </div>
        ))}
      </div>
      {!busy && !error && visible.length === 0 && (
        <div className="library-empty">
          <Icon name="Plus" size={32} />
          <p>No {kind} references yet</p>
          <small>
            {kind === "character"
              ? "Add a character in BytePlus Assets, then refresh."
              : "Upload a reference to get started."}
          </small>
        </div>
      )}
    </Modal>
  );
}

function AssetPreview({ url }: { url: string }) {
  const [failed, setFailed] = useState(false);
  return failed ? (
    <span className="muted">Preview unavailable</span>
  ) : (
    <img loading="lazy" decoding="async" src={url} alt="" onError={() => setFailed(true)} />
  );
}

function AssetDetails({
  asset,
  categories,
  busy,
  onAnalyze,
  onSave,
}: {
  asset: Asset;
  categories: Record<string, string>;
  busy: boolean;
  onAnalyze: (asset: Asset, category: string) => Promise<void>;
  onSave: (changes: {
    name: string;
    category: string;
    description: string;
  }) => Promise<void>;
}) {
  const [category, setCategory] = useState(asset.category || "");
  const [name, setName] = useState(asset.name);
  const [description, setDescription] = useState(asset.description || "");
  return (
    <div>
      <label>
        Name
        <input
          value={name}
          maxLength={120}
          disabled={busy}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <select
        aria-label={`Category for ${asset.name}`}
        value={category}
        disabled={busy}
        onChange={(e) => setCategory(e.target.value)}
      >
        <option value="">Choose category</option>
        {Object.entries(categories).map(([value, label]) => (
          <option value={value} key={value}>
            {label}
          </option>
        ))}
      </select>
      <label>
        Description
        <textarea
          value={description}
          maxLength={1200}
          disabled={busy}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>
      <Button
        disabled={busy || !category || !name.trim() || !description.trim()}
        onClick={() => onSave({ name, category, description })}
      >
        Save
      </Button>
      <Button disabled={busy} onClick={() => onAnalyze(asset, "")}>
        {busy ? "Analyzing…" : "Auto-detect again"}
      </Button>
    </div>
  );
}
