export type Kind = "character" | "clothing" | "location" | "motion";
export type StudioMode = "video" | "images";
export type Part = "freeform" | "motion";
export interface Asset {
  id: string;
  name: string;
  kind: Kind | "image";
  url: string;
  duration: number;
  category?: string;
  description?: string;
  analysisStatus?: string;
  warning?: string;
  source?: "byteplus" | "upload";
  status?: string;
}
export interface Config {
  character: string | null;
  clothing: string[];
  location: string | null;
  duration: number;
  action: string;
  speech: string;
  motions: { asset: string; duration: number }[];
}
export interface Chunk {
  id: string;
  start: number;
  duration: number;
  state: string;
  error: string;
  url: string | null;
}
export interface Run {
  id: string;
  part: Part | "full";
  state: string;
  revision: number;
  downloadUrl?: string | null;
  url: string | null;
  chunks: Chunk[];
}
export interface Project {
  id: string;
  name: string;
  revision: number;
  config: Config;
  runs: Run[];
}
export interface Bootstrap {
  images: {
    model: string;
    maxReferences: number;
    ratios: string[];
    resolutions: string[];
    pricing: number | null;
    assistanceEnabled: boolean;
    generationEnabled: boolean;
  };
  assetCategories: Partial<Record<Kind, Record<string, string>>>;
  user: { id: number; name: string };
  wallet: { available: number; held: number; spent: number };
  pricing: number | null;
  simulation: boolean;
  projects: { id: string; name: string }[];
  assets: Asset[];
  history: { id: number; kind: string; amount: number; created: string }[];
}
export interface ImageGeneration {
  id: string;
  state: "queued" | "generating" | "downloading" | "review" | "ready" | "error";
  prompt: string;
  references: string[];
  ratio: string;
  resolution: string;
  size: string;
  simulation: boolean;
  created: string;
  cost: number;
  error: string;
  url: string | null;
  downloadUrl: string | null;
}
