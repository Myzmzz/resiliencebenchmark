import { useMemo } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Tag, Typography } from "antd";

const { Text } = Typography;

interface GraphNode {
  id: string;
  kind?: string;
  name?: string;
  qualifiedName?: string;
  filePath?: string;
  language?: string;
  startLine?: number;
  endLine?: number;
}

interface GraphEdge {
  source: string;
  target: string;
  kind?: string;
}

interface IndexOverview {
  file_count?: number;
  node_count?: number;
  edge_count?: number;
}

interface CodeGraphNodeData extends Record<string, unknown> {
  title: string;
  kind: string;
  filePath: string;
  language: string;
  lineRange: string;
}

type CodeGraphFlowNode = Node<CodeGraphNodeData, "codegraph">;

function object(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function text(value: unknown, limit: number): string {
  const rendered = String(value ?? "");
  return rendered.length > limit ? `${rendered.slice(0, limit - 1)}...` : rendered;
}

function numeric(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function nodeFrom(value: unknown, index: number): GraphNode | undefined {
  const container = object(value);
  const candidate = object(container?.node) ?? container;
  if (!candidate) return undefined;
  const name = String(candidate.qualifiedName ?? candidate.name ?? "");
  const filePath = String(candidate.filePath ?? "");
  const kind = String(candidate.kind ?? "symbol");
  if (!name && !filePath) return undefined;
  return {
    id: String(candidate.id ?? `${kind}:${name}:${filePath}:${candidate.startLine ?? index}`),
    kind,
    name: String(candidate.name ?? name),
    qualifiedName: name || undefined,
    filePath: filePath || undefined,
    language: candidate.language ? String(candidate.language) : undefined,
    startLine: numeric(candidate.startLine),
    endLine: numeric(candidate.endLine),
  };
}

function nodesFromRaw(raw: unknown): GraphNode[] {
  const source = object(raw);
  let values: unknown[] = [];
  if (Array.isArray(raw)) values = raw;
  else if (Array.isArray(source?.nodes)) values = source.nodes;
  else if (Array.isArray(source?.affected)) values = source.affected;
  else if (Array.isArray(source?.callers)) values = source.callers;
  else if (Array.isArray(source?.callees)) values = source.callees;
  else if (Array.isArray(source?.entryPoints)) values = source.entryPoints;

  const seen = new Set<string>();
  return values.flatMap((value, index) => {
    const node = nodeFrom(value, index);
    if (!node || seen.has(node.id)) return [];
    seen.add(node.id);
    return [node];
  });
}

function edgesFromRaw(raw: unknown): GraphEdge[] {
  const source = object(raw);
  if (!Array.isArray(source?.edges)) return [];
  return source.edges.flatMap((value) => {
    const edge = object(value);
    if (!edge || typeof edge.source !== "string" || typeof edge.target !== "string") return [];
    return [{
      source: edge.source,
      target: edge.target,
      kind: edge.kind ? String(edge.kind) : undefined,
    }];
  });
}

function edgeColor(kind?: string): string {
  if (kind === "calls") return "#2563eb";
  if (kind === "contains") return "#64748b";
  if (kind === "extends") return "#7c3aed";
  if (kind === "instantiates") return "#b45309";
  return "#475569";
}

function CodeGraphNodeView({ data }: NodeProps<CodeGraphFlowNode>) {
  return (
    <div className="codegraph-flow-node">
      <Handle type="target" position={Position.Top} />
      <div className="codegraph-flow-kind">{data.kind}</div>
      <div className="codegraph-flow-title">{text(data.title, 34)}</div>
      <div className="codegraph-flow-path">{text(data.filePath, 38)}</div>
      <div className="codegraph-flow-meta">
        <span>{data.language || "unknown"}</span>
        <span>{data.lineRange}</span>
      </div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { codegraph: CodeGraphNodeView };

function toFlowNodes(nodes: GraphNode[]): CodeGraphFlowNode[] {
  const columns = Math.max(2, Math.ceil(Math.sqrt(nodes.length * 1.7)));
  const cellWidth = 260;
  const cellHeight = 126;
  return nodes.map((node, index) => {
    const start = node.startLine;
    const end = node.endLine;
    return {
      id: node.id,
      type: "codegraph",
      position: {
        x: (index % columns) * cellWidth,
        y: Math.floor(index / columns) * cellHeight,
      },
      data: {
        title: node.qualifiedName ?? node.name ?? node.id,
        kind: node.kind ?? "symbol",
        filePath: node.filePath ?? "",
        language: node.language ?? "",
        lineRange: start && end ? `${start}-${end}` : start ? `L${start}` : "",
      },
    };
  });
}

function toFlowEdges(edges: GraphEdge[], nodes: GraphNode[]): Edge[] {
  const known = new Set(nodes.map((node) => node.id));
  return edges.flatMap((edge, index) => {
    if (!known.has(edge.source) || !known.has(edge.target)) return [];
    return [{
      id: `${edge.source}-${edge.target}-${index}`,
      source: edge.source,
      target: edge.target,
      label: edge.kind ?? "relation",
      type: "smoothstep",
      markerEnd: { type: MarkerType.ArrowClosed, color: edgeColor(edge.kind) },
      style: { stroke: edgeColor(edge.kind), strokeWidth: 1.7 },
      labelStyle: { fill: "#334155", fontSize: 11, fontWeight: 600 },
      labelBgStyle: { fill: "#ffffff", fillOpacity: 0.82 },
    }];
  });
}

/** Interactive React Flow view for the local CodeGraph query subgraph. */
export default function CodeGraphVisualization({ result }: { result: unknown }) {
  const { nodes, edges, raw, overview } = useMemo(() => {
    const resultObject = object(result);
    const rawResult = resultObject?.raw_result ?? result;
    const graphNodes = nodesFromRaw(rawResult);
    return {
      nodes: graphNodes,
      edges: edgesFromRaw(rawResult),
      raw: object(rawResult),
      overview: object(resultObject?.index_overview) as IndexOverview | undefined,
    };
  }, [result]);

  const flowNodes = useMemo(() => toFlowNodes(nodes), [nodes]);
  const flowEdges = useMemo(() => toFlowEdges(edges, nodes), [edges, nodes]);
  if (!flowNodes.length) return null;

  const rawNodeCount = numeric(raw?.nodeCount) ?? numeric(object(raw?.stats)?.nodeCount);
  const rawEdgeCount = numeric(raw?.edgeCount) ?? numeric(object(raw?.stats)?.edgeCount);
  const rawFileCount = numeric(object(raw?.stats)?.fileCount);

  return (
    <section className="codegraph-visualization" aria-label="CodeGraph 可视化">
      <div className="codegraph-visualization-header">
        <div>
          <strong>局部代码图</strong>
          <Text type="secondary">当前 CodeGraph 查询返回的节点、关系与源码位置</Text>
        </div>
        <div className="codegraph-visualization-tags">
          <Tag color="blue">{nodes.length} 个返回节点</Tag>
          <Tag color="purple">{flowEdges.length} 条可见边</Tag>
          {(rawNodeCount ?? rawEdgeCount ?? rawFileCount) !== undefined && (
            <Tag color="cyan">
              本次结果 {rawNodeCount ?? nodes.length} 节点 / {rawEdgeCount ?? edges.length} 边
              {rawFileCount !== undefined ? ` / ${rawFileCount} 文件` : ""}
            </Tag>
          )}
          {overview && (
            <Tag color="geekblue">
              全量索引 {overview.node_count ?? 0} 节点 / {overview.edge_count ?? 0} 边 / {overview.file_count ?? 0} 文件
            </Tag>
          )}
        </div>
      </div>
      <div className="codegraph-canvas">
        <ReactFlow
          nodes={flowNodes}
          edges={flowEdges}
          nodeTypes={nodeTypes}
          fitView
          minZoom={0.18}
          maxZoom={1.8}
          nodesDraggable
          nodesConnectable={false}
          elementsSelectable
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={24} size={1.2} color="#dbe4f0" />
          <Controls showInteractive={false} />
          <MiniMap pannable zoomable nodeColor="#60a5fa" maskColor="rgba(248, 251, 255, 0.72)" />
        </ReactFlow>
      </div>
    </section>
  );
}
