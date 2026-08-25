import { Badge, Tooltip } from "antd";
import { useEffect, useState } from "react";
import { fetchHealth } from "../services/api";

type Status =
  | { kind: "loading" }
  | { kind: "connected"; version: string }
  | { kind: "repo-invalid"; path: string }
  | { kind: "disconnected" };

/** Header 常驻的服务/仓库连通性指示。 */
export default function HealthIndicator() {
  const [status, setStatus] = useState<Status>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetchHealth()
      .then((health) => {
        if (cancelled) return;
        setStatus(
          health.repo.factory_config_found
            ? { kind: "connected", version: health.version }
            : { kind: "repo-invalid", path: health.repo.path },
        );
      })
      .catch(() => {
        if (!cancelled) setStatus({ kind: "disconnected" });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  switch (status.kind) {
    case "loading":
      return <Badge status="processing" text="连接中…" />;
    case "connected":
      return <Badge status="success" text="服务已连接" />;
    case "repo-invalid":
      return (
        <Tooltip title={`检查 BENCHMARK_REPO_PATH（当前指向 ${status.path}）`}>
          <Badge status="warning" text="仓库路径无效" />
        </Tooltip>
      );
    case "disconnected":
      return <Badge status="error" text="服务未连接" />;
  }
}
