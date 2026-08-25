import type { ThemeConfig } from "antd";

/** 判分状态等语义色（设计文档：不同数据不同可视化，不用默认散装色）。 */
export const semanticColors = {
  pass: "#16A34A",
  failSafety: "#DC2626",
  failOther: "#EA580C",
  caseInvalid: "#6B7280",
  defect: "#DC2626",
  disturbance: "#F59E0B",
  fault: "#0EA5E9",
} as const;

export const appTheme: ThemeConfig = {
  token: {
    colorPrimary: "#1D4ED8",
    borderRadius: 6,
    fontSize: 14,
  },
  components: {
    Layout: {
      siderBg: "#0F172A",
      headerBg: "#FFFFFF",
    },
    Menu: {
      darkItemBg: "#0F172A",
      darkItemSelectedBg: "#1D4ED8",
    },
  },
};
