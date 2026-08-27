import { useState, useEffect } from "react";
import { Table, Badge, Button, Drawer, Tag, Alert, Spin, Descriptions, List, Typography, Space } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined, PlusOutlined } from "@ant-design/icons";
import { fetchApplications } from "../services/api";
import type { Application, ReadinessGap } from "../types/application";
import { useNavigate } from "react-router-dom";


export default function ApplicationsDetailPage() {
  const navigate = useNavigate();

  return (
    <div></div>
  );
}