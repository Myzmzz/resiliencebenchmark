export type ResourceType = 'kubernetes' | 'ssh_host' | 'registry' | 'model_gateway';
export type ResourceStatus = 'qualified' | 'partial' | 'pending' | 'error';

export interface InfrastructureResource {
  type: ResourceType;
  name: string;
  status: ResourceStatus;
  endpoint: string;
  metrics: Record<string, number>;
  last_qualified: string | null;
  details: unknown;
}
