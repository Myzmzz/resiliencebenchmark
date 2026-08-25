/**
 * 实验环境类型定义
 */

export type PodPhase = "Running" | "Pending" | "Succeeded" | "Failed" | "Unknown";
export type ComponentHealth = "运行正常" | "需要关注" | "异常";
export type ConnectionStatus = "连接正常" | "连接失败" | "未配置";

export interface PodInfo {
  name: string;
  namespace: string;
  node: string;
  phase: PodPhase;
  restarts: number;
  ready: string; // "1/1" 格式
  ip: string | null;
  created_at: string; // ISO 8601
}

export interface NodeInfo {
  name: string;
  pod_count: number;
  status: "Ready" | "NotReady";
}

export interface ComponentStatus {
  name: string;
  version: string;
  instances: string; // "2/2" 格式
  health: ComponentHealth;
}

export interface ClusterSummary {
  node_count: number;
  namespace_count: number;
  pod_count: number;
  abnormal_pod_count: number;
}

export interface ExperimentEnvironment {
  api_server: string;
  k8s_version: string;
  last_sync: string; // ISO 8601
  connection_status: ConnectionStatus;
  summary: ClusterSummary;
  nodes: NodeInfo[];
  pods: PodInfo[];
  components: ComponentStatus[];
}
