import type { MatrixInspection, MatrixListItem, MatrixTrialDetail } from "./matrixTypes";

const ROOT = import.meta.env.VITE_STAGE2_API_ROOT || "/api/v1";

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${ROOT}${path}`, {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json() as { detail?: unknown };
      detail = typeof body.detail === "string" ? `: ${body.detail}` : "";
    } catch {
      detail = "";
    }
    throw new Error(`Stage2 matrix API failed: ${response.status}${detail}`);
  }
  return await response.json() as T;
}

export async function listMatrices(signal?: AbortSignal): Promise<MatrixListItem[]> {
  const value = await request<{ matrices: MatrixListItem[] }>("/matrices", signal);
  return value.matrices;
}

export function getMatrix(matrixId: string, signal?: AbortSignal): Promise<MatrixInspection> {
  return request(`/matrices/${encodeURIComponent(matrixId)}`, signal);
}

export function getMatrixTrial(matrixId: string, trialId: string, signal?: AbortSignal): Promise<MatrixTrialDetail> {
  return request(`/matrices/${encodeURIComponent(matrixId)}/trials/${encodeURIComponent(trialId)}`, signal);
}

export function matrixArtifactUrl(matrixId: string, artifactPath: string): string {
  const encodedPath = artifactPath.split("/").map(encodeURIComponent).join("/");
  return `${ROOT}/matrices/${encodeURIComponent(matrixId)}/artifacts/${encodedPath}`;
}
