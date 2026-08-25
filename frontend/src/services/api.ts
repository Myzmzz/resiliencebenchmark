/** 后端 API 客户端。一期只有 GET；类型与后端 Pydantic 模型对齐。 */

import type { Application } from "../types/application";
import type { ModelsRegistry } from "../types/model";
import type { HarnessesRegistry } from "../types/harness";
import type { Episode } from "../types/episode";
import type { MCPToolsRegistry } from "../types/mcp";
import type { ObservabilityStack } from "../types/observability";

export interface RepoCheck {
  path: string;
  exists: boolean;
  factory_config_found: boolean;
}

export interface HealthResponse {
  service: string;
  version: string;
  repo: RepoCheck;
}

export async function fetchHealth(): Promise<HealthResponse> {
  const response = await fetch("/api/v1/meta/health");
  if (!response.ok) throw new Error(`health check failed: ${response.status}`);
  return (await response.json()) as HealthResponse;
}

export async function fetchApplications(): Promise<Application[]> {
  const response = await fetch("/api/v1/applications");
  if (!response.ok) throw new Error(`Failed to fetch applications: ${response.status}`);
  return (await response.json()) as Application[];
}

export async function fetchModels(): Promise<ModelsRegistry> {
  const response = await fetch("/api/v1/models");
  if (!response.ok) throw new Error(`Failed to fetch models: ${response.status}`);
  return (await response.json()) as ModelsRegistry;
}

export async function fetchHarnesses(): Promise<HarnessesRegistry> {
  const response = await fetch("/api/v1/harnesses");
  if (!response.ok) throw new Error(`Failed to fetch harnesses: ${response.status}`);
  return (await response.json()) as HarnessesRegistry;
}

export async function fetchEpisodes(): Promise<Episode[]> {
  const response = await fetch("/api/v1/episodes");
  if (!response.ok) throw new Error(`Failed to fetch episodes: ${response.status}`);
  return (await response.json()) as Episode[];
}

export async function fetchMCPTools(): Promise<MCPToolsRegistry> {
  const response = await fetch("/api/v1/mcp-tools");
  if (!response.ok) throw new Error(`Failed to fetch MCP tools: ${response.status}`);
  return (await response.json()) as MCPToolsRegistry;
}

export async function fetchObservability(): Promise<ObservabilityStack> {
  const response = await fetch("/api/v1/observability");
  if (!response.ok) throw new Error(`Failed to fetch observability: ${response.status}`);
  return (await response.json()) as ObservabilityStack;
}
