import type { InfrastructureResource } from '../types/infrastructure';

const API_BASE = 'http://localhost:8000';

export async function fetchInfrastructureResources(): Promise<InfrastructureResource[]> {
  const response = await fetch(`${API_BASE}/api/v1/infrastructure/resources`);
  if (!response.ok) {
    throw new Error(`Failed to fetch infrastructure resources: ${response.statusText}`);
  }
  return response.json();
}
