import React from 'react'
import { createRoot } from 'react-dom/client'
import { ConfigProvider, App as AntdApp } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App.jsx'
import './styles.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#4D6BFE',
          borderRadius: 8,
          fontFamily: "-apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
        },
      }}
    >
      <AntdApp>
        <App />
      </AntdApp>
    </ConfigProvider>
  </React.StrictMode>,
)
