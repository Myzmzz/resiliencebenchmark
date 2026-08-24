import { useState, useEffect } from "react";
import { Table, Button, Drawer, Tag, Alert, Spin, Descriptions, List, Typography, Space } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined, ExperimentOutlined, ClockCircleOutlined } from "@ant-design/icons";
import { fetchEpisodes } from "../services/api";
import type { Episode } from "../types/episode";

const { Title, Paragraph } = Typography;

export default function EpisodesPage() {
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedEpisode, setSelectedEpisode] = useState<Episode | null>(null);

  const loadEpisodes = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchEpisodes();
      setEpisodes(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch episodes");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadEpisodes();
  }, []);

  const handleRowClick = (episode: Episode) => {
    setSelectedEpisode(episode);
    setDrawerOpen(true);
  };

  const getStatusTag = (status: string) => {
    const statusMap: Record<string, { color: string; text: string }> = {
      example: { color: "blue", text: "示例" },
      draft: { color: "default", text: "草稿" },
      ready: { color: "green", text: "就绪" },
      error: { color: "red", text: "错误" },
    };
    const config = statusMap[status] || { color: "default", text: status };
    return <Tag color={config.color}>{config.text}</Tag>;
  };

  const columns: ColumnsType<Episode> = [
    {
      title: "Episode ID",
      dataIndex: "episode_id",
      key: "episode_id",
      width: 220,
      render: (text: string, record: Episode) => (
        <a onClick={() => handleRowClick(record)}>{text}</a>
      ),
    },
    {
      title: "标题",
      dataIndex: "title",
      key: "title",
      width: 350,
      ellipsis: true,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 100,
      render: (status: string) => getStatusTag(status),
    },
    {
      title: "应用",
      key: "application",
      width: 150,
      render: (_: any, record: Episode) => record.application.name,
    },
    {
      title: "服务数",
      key: "services",
      width: 100,
      render: (_: any, record: Episode) => record.application.candidate_services.length,
    },
    {
      title: "预算",
      key: "budget",
      width: 200,
      render: (_: any, record: Episode) => (
        <Space size="small">
          <Tag icon={<ExperimentOutlined />}>{record.budget.max_experiments} 次实验</Tag>
          <Tag icon={<ClockCircleOutlined />}>{record.budget.max_duration_minutes} 分钟</Tag>
        </Space>
      ),
    },
  ];

  if (error) {
    return (
      <div style={{ padding: 24 }}>
        <Alert message="加载失败" description={error} type="error" showIcon />
        <Button icon={<ReloadOutlined />} onClick={loadEpisodes} style={{ marginTop: 16 }}>
          重试
        </Button>
      </div>
    );
  }

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <Title level={3} style={{ margin: 0 }}>评测单元</Title>
        <Button icon={<ReloadOutlined />} onClick={loadEpisodes} loading={loading}>
          刷新
        </Button>
      </div>

      {loading ? (
        <div style={{ textAlign: "center", padding: 48 }}>
          <Spin size="large" />
        </div>
      ) : episodes.length === 0 ? (
        <Alert
          message="暂无评测单元"
          description="未找到任何 Episode 配置文件"
          type="info"
          showIcon
        />
      ) : (
        <Table
          columns={columns}
          dataSource={episodes}
          rowKey="episode_id"
          pagination={{ pageSize: 10 }}
        />
      )}

      <Drawer
        title={selectedEpisode?.title}
        placement="right"
        width={720}
        onClose={() => setDrawerOpen(false)}
        open={drawerOpen}
      >
        {selectedEpisode && <EpisodeDetails episode={selectedEpisode} />}
      </Drawer>
    </div>
  );
}

function EpisodeDetails({ episode }: { episode: Episode }) {
  return (
    <div>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="Episode ID">{episode.episode_id}</Descriptions.Item>
        <Descriptions.Item label="标题">{episode.title}</Descriptions.Item>
        <Descriptions.Item label="状态">
          <Tag color={episode.status === "example" ? "blue" : "default"}>{episode.status}</Tag>
        </Descriptions.Item>
        <Descriptions.Item label="Schema 版本">{episode.schema_version}</Descriptions.Item>
      </Descriptions>

      <Title level={5} style={{ marginTop: 24 }}>智能体目标</Title>
      <Paragraph>{episode.agent_goal}</Paragraph>

      <Title level={5} style={{ marginTop: 24 }}>应用配置</Title>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="应用名称">{episode.application.name}</Descriptions.Item>
        <Descriptions.Item label="命名空间">{episode.application.namespace}</Descriptions.Item>
        <Descriptions.Item label="发布引用">{episode.application.release_ref}</Descriptions.Item>
        <Descriptions.Item label="候选服务">
          <Space wrap>
            {episode.application.candidate_services.map((service) => (
              <Tag key={service} color="cyan">{service}</Tag>
            ))}
          </Space>
        </Descriptions.Item>
      </Descriptions>

      <Title level={5} style={{ marginTop: 24 }}>环境快照</Title>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="快照 ID">{episode.environment_snapshot.snapshot_id}</Descriptions.Item>
      </Descriptions>
      {episode.environment_snapshot.health_prerequisites && episode.environment_snapshot.health_prerequisites.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 16 }}>健康先决条件</Title>
          <List
            size="small"
            bordered
            dataSource={episode.environment_snapshot.health_prerequisites}
            renderItem={(item) => <List.Item>{item}</List.Item>}
          />
        </>
      )}
      {episode.environment_snapshot.reset_contract && episode.environment_snapshot.reset_contract.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 16 }}>重置契约</Title>
          <List
            size="small"
            bordered
            dataSource={episode.environment_snapshot.reset_contract}
            renderItem={(item) => <List.Item>{item}</List.Item>}
          />
        </>
      )}

      <Title level={5} style={{ marginTop: 24 }}>工作负载</Title>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="配置文件">{episode.workload.profile}</Descriptions.Item>
      </Descriptions>
      <Title level={5} style={{ marginTop: 16 }}>SLO</Title>
      <List
        size="small"
        bordered
        dataSource={episode.workload.slo}
        renderItem={(item) => <List.Item>{item}</List.Item>}
      />

      <Title level={5} style={{ marginTop: 24 }}>可观测性</Title>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="指标">
          {episode.observability.metrics.join(", ")}
        </Descriptions.Item>
        <Descriptions.Item label="追踪">
          {episode.observability.traces.join(", ")}
        </Descriptions.Item>
        <Descriptions.Item label="日志">
          {episode.observability.logs.join(", ")}
        </Descriptions.Item>
        <Descriptions.Item label="Kubernetes">
          {episode.observability.kubernetes.join(", ")}
        </Descriptions.Item>
      </Descriptions>

      <Title level={5} style={{ marginTop: 24 }}>源代码访问</Title>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="模式">{episode.source_access.mode}</Descriptions.Item>
        <Descriptions.Item label="允许路径">
          <Space wrap>
            {episode.source_access.allowed_paths.map((path) => (
              <Tag key={path} color="green"><code>{path}</code></Tag>
            ))}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="禁止路径">
          <Space wrap>
            {episode.source_access.forbidden_paths.map((path) => (
              <Tag key={path} color="red"><code>{path}</code></Tag>
            ))}
          </Space>
        </Descriptions.Item>
      </Descriptions>

      <Title level={5} style={{ marginTop: 24 }}>动作空间</Title>
      <Descriptions column={1} bordered size="small">
        <Descriptions.Item label="允许的触发器类">
          <Space wrap>
            {episode.action_space.allowed_trigger_classes.map((cls) => (
              <Tag key={cls} color="blue">{cls}</Tag>
            ))}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label="允许的目标范围">
          {episode.action_space.allowed_target_scope.join(", ")}
        </Descriptions.Item>
      </Descriptions>
      {episode.action_space.forbidden_actions.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 16 }}>禁止的动作</Title>
          <List
            size="small"
            bordered
            dataSource={episode.action_space.forbidden_actions}
            renderItem={(item) => <List.Item>{item}</List.Item>}
          />
        </>
      )}

      <Title level={5} style={{ marginTop: 24 }}>预算配置</Title>
      <Descriptions column={2} bordered size="small">
        <Descriptions.Item label="最大实验次数">{episode.budget.max_experiments}</Descriptions.Item>
        <Descriptions.Item label="最大时长">{episode.budget.max_duration_minutes} 分钟</Descriptions.Item>
        <Descriptions.Item label="最大并发故障">{episode.budget.max_concurrent_faults}</Descriptions.Item>
      </Descriptions>

      {episode.safety_constraints.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>安全约束</Title>
          <List
            size="small"
            bordered
            dataSource={episode.safety_constraints}
            renderItem={(item) => <List.Item>{item}</List.Item>}
          />
        </>
      )}

      {episode.expected_agent_output.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>期望的智能体输出</Title>
          <List
            size="small"
            bordered
            dataSource={episode.expected_agent_output}
            renderItem={(item) => <List.Item>{item}</List.Item>}
          />
        </>
      )}

      {episode.leakage_controls.length > 0 && (
        <>
          <Title level={5} style={{ marginTop: 24 }}>泄漏控制</Title>
          <List
            size="small"
            bordered
            dataSource={episode.leakage_controls}
            renderItem={(item) => <List.Item>{item}</List.Item>}
          />
        </>
      )}
    </div>
  );
}
