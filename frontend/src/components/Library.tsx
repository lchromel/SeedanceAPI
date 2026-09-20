import { useEffect, useRef, useState } from "react";
import type { Asset, Kind } from "../types";
import { api, time } from "../api";
import { Button, Icon, Modal } from "./UI";
export function Library({
  kind,
  assets,
  selected,
  onClose,
  onSelect,
  onUpload,
  onSync,
}: {
  kind: Kind;
  assets: Asset[];
  selected: string[];
  onClose: () => void;
  onSelect: (asset: Asset) => void;
  onUpload: (asset: Asset) => void;
  onSync: (assets: Asset[]) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
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
      onUpload(await api<Asset>("assets", "POST", data));
    } catch (e) {
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
            {busy ? "Uploading…" : "Upload"}
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
          <button
            key={a.id}
            className={`library-asset ${selected.includes(a.id) ? "chosen" : ""}`}
            disabled={
              kind === "character" && (busy || !!error || a.status !== "Active")
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
            {selected.includes(a.id) && (
              <span className="selected-check">
                <Icon name="Check" />
              </span>
            )}
          </button>
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
    <img src={url} alt="" onError={() => setFailed(true)} />
  );
}
