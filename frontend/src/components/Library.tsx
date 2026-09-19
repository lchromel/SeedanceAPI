import { useRef, useState } from "react";
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
}: {
  kind: Kind;
  assets: Asset[];
  selected: string[];
  onClose: () => void;
  onSelect: (asset: Asset) => void;
  onUpload: (asset: Asset) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
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
          {kind === "motion"
            ? "Video references · 4–120 seconds"
            : "Your private image library"}
        </span>
        <Button onClick={() => input.current?.click()} disabled={busy}>
          {busy ? "Uploading…" : "Upload"}
          <Icon name="Plus" size={18} />
        </Button>
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
        {assets
          .filter((a) => a.kind === kind)
          .map((a) => (
            <button
              key={a.id}
              className={`library-asset ${selected.includes(a.id) ? "chosen" : ""}`}
              onClick={() => onSelect(a)}
            >
              {kind === "motion" ? (
                <video src={a.url} muted playsInline preload="metadata" />
              ) : (
                <img src={a.url} alt="" />
              )}
              <span>{a.name}</span>
              {kind === "motion" && <small>{time(a.duration)} max</small>}
              {selected.includes(a.id) && (
                <span className="selected-check">
                  <Icon name="Check" />
                </span>
              )}
            </button>
          ))}
      </div>
      {!assets.some((a) => a.kind === kind) && (
        <div className="library-empty">
          <Icon name="Plus" size={32} />
          <p>No {kind} references yet</p>
          <small>Upload a reference to get started.</small>
        </div>
      )}
    </Modal>
  );
}
