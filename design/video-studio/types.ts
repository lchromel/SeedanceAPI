/** Proposed UI contract, not an existing backend API. */
export type ChunkStatus = "queued" | "generating" | "ready" | "error";
export type PartKind = "freeform" | "motion";
export interface Asset { id: string; name: string; thumbnailUrl: string }
export interface SceneSelection {
  character: Asset | null;
  clothing: Asset[];
  location: Asset | null;
}
export interface MotionPreset extends Asset { maxDurationSeconds: number }
export interface MotionSelection {
  id: string;
  presetId: string;
  /** Must be positive and <= selected preset.maxDurationSeconds. */
  durationSeconds: number;
}
export interface GenerationChunk {
  id: string;
  part: PartKind;
  motionSelectionId?: string;
  startSeconds: number;
  durationSeconds: number;
  status: ChunkStatus;
  progress?: number;
  videoUrl?: string;
  errorMessage?: string;
}
export interface VideoProject {
  id: string;
  scene: SceneSelection;
  freeform: { durationSeconds: 30 | 60 | 90 | 120; action: string; speech: string };
  motions: MotionSelection[];
  chunks: GenerationChunk[];
}
