/**
 * 实验环境 API 客户端
 */

import type { ExperimentEnvironment } from "../types/experiment";

export async function fetchExperimentEnvironment(): Promise<ExperimentEnvironment> {
  const response = await fetch("/api/v1/experiments/environment");
  if (!response.ok) throw new Error(`Failed to fetch experiment environment: ${response.status}`);
  return (await response.json()) as ExperimentEnvironment;
}
