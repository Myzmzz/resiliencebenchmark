import { Empty, Tag, Typography } from "antd";

const milestoneColor: Record<string, string> = {
  M3: "blue",
  M4: "geekblue",
  M5: "purple",
  "Phase 2": "default",
};

export interface PlaceholderPageProps {
  title: string;
  milestone: "M3" | "M4" | "M5" | "Phase 2";
  description?: string;
}

/** 未实现页面的统一占位：保留完整导航形态，标注交付里程碑。 */
export default function PlaceholderPage({ title, milestone, description }: PlaceholderPageProps) {
  return (
    <div style={{ padding: 24 }}>
      <Typography.Title level={4} style={{ marginBottom: 4 }}>
        {title} <Tag color={milestoneColor[milestone]}>{milestone}</Tag>
      </Typography.Title>
      <Empty
        style={{ marginTop: 80 }}
        description={description ?? "该页面将在对应里程碑交付，导航与信息架构已冻结。"}
      />
    </div>
  );
}
