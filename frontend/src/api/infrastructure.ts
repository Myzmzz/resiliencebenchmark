import type { InfrastructureResource } from '../types/infrastructure';

export async function fetchInfrastructureResources(): Promise<InfrastructureResource[]> {
  const response = await fetch('/api/v1/infrastructure/resources');
  if (!response.ok) {
    throw new Error(`Failed to fetch infrastructure resources: ${response.statusText}`);
  }
  return response.json();
}
