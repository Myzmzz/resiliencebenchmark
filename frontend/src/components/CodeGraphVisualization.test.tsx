import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import CodeGraphVisualization from "./CodeGraphVisualization";

describe("CodeGraphVisualization", () => {
  it("展示查询结果中的节点和边", () => {
    render(
      <CodeGraphVisualization
        result={{
          index_overview: {
            file_count: 311,
            node_count: 8770,
            edge_count: 12667,
          },
          raw_result: {
            nodes: [
              { id: "function:a", kind: "function", qualifiedName: "checkout", filePath: "checkout.ts" },
              { id: "method:b", kind: "method", qualifiedName: "CartService::add", filePath: "cart.ts" },
            ],
            edges: [{ source: "function:a", target: "method:b", kind: "calls" }],
          },
        }}
      />,
    );

    expect(screen.getByLabelText("CodeGraph 可视化")).toBeInTheDocument();
    expect(screen.getByText("局部代码图")).toBeInTheDocument();
    expect(screen.getByText("2 个返回节点")).toBeInTheDocument();
    expect(screen.getByText("1 条可见边")).toBeInTheDocument();
    expect(screen.getByText("全量索引 8770 节点 / 12667 边 / 311 文件")).toBeInTheDocument();
    expect(screen.getByText("checkout")).toBeInTheDocument();
    expect(screen.getByText("CartService::add")).toBeInTheDocument();
  });

  it("展示 query 搜索结果数组", () => {
    render(
      <CodeGraphVisualization
        result={{
          raw_result: [
            {
              node: {
                id: "class:a",
                kind: "class",
                qualifiedName: "CheckoutService",
                filePath: "src/demo.grpc.pb.h",
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("1 个返回节点")).toBeInTheDocument();
    expect(screen.getByText("0 条可见边")).toBeInTheDocument();
    expect(screen.getByText("CheckoutService")).toBeInTheDocument();
  });
});
