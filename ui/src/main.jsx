import React from 'react';
import { createRoot } from 'react-dom/client';
import { App as AntApp, ConfigProvider, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import 'antd/dist/reset.css';
import App from './App.jsx';
createRoot(document.getElementById('root')).render(
  <ConfigProvider locale={zhCN} theme={{
    algorithm: theme.darkAlgorithm,
    token: { colorPrimary: '#a394ff', colorInfo: '#a394ff', colorText: '#efedf7', colorTextSecondary: '#9594aa', colorBorder: '#2c2d3c', colorBgLayout: '#111219', colorBgContainer: '#1a1b25', colorBgElevated: '#232431', borderRadius: 12, controlHeight: 42, fontSize: 13, fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", sans-serif' },
    components: { Card: { paddingLG: 24, headerFontSize: 14 }, Button: { fontWeight: 600 }, Table: { headerBg: '#20212d', cellPaddingBlock: 16 } },
  }}><AntApp><App /></AntApp></ConfigProvider>,
);
