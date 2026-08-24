import { Navigate, createBrowserRouter } from "react-router-dom";
import type { RouteObject } from "react-router-dom";
import AppLayout from "./layout/AppLayout";
import PlaceholderPage from "./pages/PlaceholderPage";
import InfrastructurePage from "./pages/InfrastructurePage";
import ApplicationsPage from "./pages/ApplicationsPage";
import ModelsPage from "./pages/ModelsPage";
import HarnessesPage from "./pages/HarnessesPage";
import EpisodesPage from "./pages/EpisodesPage";
import MCPToolsPage from "./pages/MCPToolsPage";
import WorkloadsPage from "./pages/WorkloadsPage";
import ObservabilityPage from "./pages/ObservabilityPage";
import EvaluationTasksPage from "./features/evaluation/pages/EvaluationTasksPage";
import EvaluationTaskCreatePage from "./features/evaluation/pages/EvaluationTaskCreatePage";
import EvaluationMonitoringPage from "./features/evaluation/pages/EvaluationMonitoringPage";
import EvaluationTaskMonitorPage from "./features/evaluation/pages/EvaluationTaskMonitorPage";
import EvaluationUnitDetailPage from "./features/evaluation/pages/EvaluationUnitDetailPage";
import EvaluationResultsPage from "./features/evaluation/pages/EvaluationResultsPage";
import EvaluationResultDetailPage from "./features/evaluation/pages/EvaluationResultDetailPage";
import "./features/evaluation/evaluation.css";

/** 子路由集中定义，测试用 createMemoryRouter 复用。 */
export const routeChildren: RouteObject[] = [
  { index: true, element: <Navigate to="/infrastructure" replace /> },
  // 基础设施资源（M2）
  { path: "infrastructure", element: <InfrastructurePage /> },
  // 环境与资源（M3）
  { path: "environments/infrastructure", element: <PlaceholderPage title="实验环境" milestone="M3" /> },
  { path: "environments/applications", element: <ApplicationsPage /> },
  { path: "environments/workloads", element: <WorkloadsPage /> },
  { path: "environments/observability", element: <ObservabilityPage /> },
  { path: "environments/mcp", element: <MCPToolsPage /> },
  // 智能体（M3；Skill 二期）
  { path: "agents/harnesses", element: <HarnessesPage /> },
  { path: "agents/models", element: <ModelsPage /> },
  { path: "agents/units", element: <EpisodesPage /> },
  { path: "agents/skills", element: <PlaceholderPage title="Skill 管理" milestone="Phase 2" /> },
  // 题库（M4）
  { path: "episodes/defects", element: <PlaceholderPage title="缺陷库" milestone="M4" /> },
  { path: "episodes/disturbances", element: <PlaceholderPage title="扰动库" milestone="M4" /> },
  { path: "episodes/list", element: <PlaceholderPage title="Episode 管理" milestone="M4" /> },
  // 评测
  { path: "evaluation/tasks", element: <EvaluationTasksPage /> },
  { path: "evaluation/tasks/new", element: <EvaluationTaskCreatePage /> },
  { path: "evaluation/monitoring", element: <EvaluationMonitoringPage /> },
  { path: "evaluation/monitoring/:taskId", element: <EvaluationTaskMonitorPage /> },
  { path: "evaluation/monitoring/:taskId/units/:unitId", element: <EvaluationUnitDetailPage /> },
  { path: "evaluation/results", element: <EvaluationResultsPage /> },
  { path: "evaluation/results/:taskId", element: <EvaluationResultDetailPage /> },
];

export const router = createBrowserRouter([
  { path: "/", element: <AppLayout />, children: routeChildren },
]);
