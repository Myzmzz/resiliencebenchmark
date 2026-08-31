import { Layout, Menu, Typography } from "antd";
import type { MenuProps } from "antd";
import { Boxes, Bot, Library, Gauge, Database } from "lucide-react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import HealthIndicator from "../components/HealthIndicator";
import { menuSelectionForPath } from "./menuSelection";

const iconStyle = { width: 16, height: 16, verticalAlign: -3 };

const menuItems: MenuProps["items"] = [
  {
    key: "/infrastructure",
    icon: <Database style={iconStyle} />,
    label: "基础设施",
  },
  {
    key: "environments",
    icon: <Boxes style={iconStyle} />,
    label: "环境与资源",
    children: [
      { key: "/environments/infrastructure", label: "实验环境" },
      { key: "/environments/applications", label: "被测系统" },
      { key: "/environments/workloads", label: "负载与 SLO" },
      { key: "/environments/observability", label: "可观测性栈" },
      { key: "/environments/mcp", label: "MCP 服务" },
    ],
  },
  {
    key: "agents",
    icon: <Bot style={iconStyle} />,
    label: "智能体",
    children: [
      { key: "/agents/harnesses", label: "Harness 管理" },
      { key: "/agents/models", label: "模型管理" },
      { key: "/agents/units", label: "评测单元" },
      { key: "/agents/skills", label: "Skill 管理" },
    ],
  },
  {
    key: "episodes",
    icon: <Library style={iconStyle} />,
    label: "题库",
    children: [
      { key: "/episodes/defects", label: "缺陷库" },
      { key: "/episodes/disturbances", label: "扰动库" },
      { key: "/episodes/list", label: "Episode 管理" },
    ],
  },
  {
    key: "evaluation",
    icon: <Gauge style={iconStyle} />,
    label: "评测",
    children: [
      { key: "/evaluation/tasks", label: "评测任务" },
      { key: "/evaluation/stage2-console", label: "Stage2 控制台" },
      { key: "/evaluation/monitoring", label: "运行监控" },
      { key: "/evaluation/results", label: "结果分析" },
    ],
  },
];

export default function AppLayout() {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const openKeys = pathname.startsWith("/infrastructure") ? [] : [pathname.split("/")[1] ?? "environments"];
  const selectedKey = menuSelectionForPath(pathname);

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Sider width={220} theme="dark">
        <div style={{ padding: "20px 24px 12px" }}>
          <Typography.Text strong style={{ color: "#F8FAFC", fontSize: 15 }}>
            韧性测试 Benchmark
          </Typography.Text>
        </div>
        <Menu
          theme="dark"
          mode="inline"
          items={menuItems}
          selectedKeys={[selectedKey]}
          defaultOpenKeys={openKeys}
          onClick={({ key }) => navigate(key)}
        />
      </Layout.Sider>
      <Layout>
        <Layout.Header
          style={{ display: "flex", alignItems: "center", justifyContent: "flex-end",
                   borderBottom: "1px solid #E5E7EB", paddingInline: 24 }}
        >
          <HealthIndicator />
        </Layout.Header>
        <Layout.Content style={{ background: "#F8FAFC" }}>
          <Outlet />
        </Layout.Content>
      </Layout>
    </Layout>
  );
}
