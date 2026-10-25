import { useCallback, useEffect, useState } from 'react'
import {
  App as AntdApp, Button, Input, Popconfirm, Switch, Table, Tabs, Tag, Typography,
} from 'antd'
import { ArrowLeftOutlined, LockOutlined, PlusOutlined } from '@ant-design/icons'
import { api } from '../api.js'

const { Title, Text } = Typography

/** 意图识别管理：规则列表（内置锁定）+ 添加 + 启停 + 删除 */
function IntentRules() {
  const { message } = AntdApp.useApp()
  const [rules, setRules] = useState(null)
  const [pattern, setPattern] = useState('')
  const [adding, setAdding] = useState(false)

  const load = useCallback(() => {
    api.adminIntentRules().then(setRules).catch((e) => message.error(e.message))
  }, [message])

  useEffect(() => { load() }, [load])

  const add = async () => {
    const p = pattern.trim()
    if (!p) return
    setAdding(true)
    try {
      await api.adminAddIntentRule(p)
      setPattern('')
      message.success('规则已添加')
      load()
    } catch (e) {
      message.error(e.message)
    } finally {
      setAdding(false)
    }
  }

  const toggle = async (id) => {
    try { await api.adminToggleIntentRule(id); load() }
    catch (e) { message.error(e.message) }
  }

  const remove = async (id) => {
    try { await api.adminDelIntentRule(id); message.success('已删除'); load() }
    catch (e) { message.error(e.message) }
  }

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 16 }}>
        <Input
          value={pattern}
          placeholder="输入关键词或正则，如：心态炸了 / 想哭"
          onChange={(e) => setPattern(e.target.value)}
          onPressEnter={add}
          style={{ maxWidth: 420 }}
        />
        <Button type="primary" icon={<PlusOutlined />} loading={adding} onClick={add}>
          添加规则
        </Button>
        <Text type="secondary" style={{ alignSelf: 'center', fontSize: 12 }}>
          命中规则的提问将注入《戒者录》个人叙事作支撑
        </Text>
      </div>
      <Table
        rowKey="id" size="middle" loading={!rules} dataSource={rules || []}
        pagination={{ pageSize: 10, showSizeChanger: false }}
        columns={[
          { title: 'ID', dataIndex: 'id', width: 60 },
          { title: '规则（正则）', dataIndex: 'pattern' },
          {
            title: '来源', dataIndex: 'builtin', width: 90,
            render: (b) => b ? <Tag bordered={false}>内置</Tag> : <Tag color="blue" bordered={false}>自定义</Tag>,
          },
          {
            title: '启用', dataIndex: 'enabled', width: 80,
            render: (v, r) => <Switch size="small" checked={v} onClick={() => toggle(r.id)} />,
          },
          {
            title: '操作', width: 90,
            render: (_, r) => r.builtin
              ? <Text type="secondary"><LockOutlined /> 锁定</Text>
              : (
                <Popconfirm title="删除该规则？" okText="删除" cancelText="取消" onConfirm={() => remove(r.id)}>
                  <a style={{ color: '#e5484d' }}>删除</a>
                </Popconfirm>
              ),
          },
        ]}
      />
    </div>
  )
}

/** 用户管理：列表 + 删除（内置管理员与本人不可删） */
function Users({ me }) {
  const { message } = AntdApp.useApp()
  const [users, setUsers] = useState(null)

  const load = useCallback(() => {
    api.adminUsers().then(setUsers).catch((e) => message.error(e.message))
  }, [message])

  useEffect(() => { load() }, [load])

  const remove = async (id) => {
    try { await api.adminDeleteUser(id); message.success('已删除'); load() }
    catch (e) { message.error(e.message) }
  }

  return (
    <Table
      rowKey="id" size="middle" loading={!users} dataSource={users || []}
      pagination={false}
      columns={[
        { title: 'ID', dataIndex: 'id', width: 60 },
        { title: '用户名', dataIndex: 'username' },
        { title: '邮箱', dataIndex: 'email', render: (v) => v || '-' },
        {
          title: '角色', dataIndex: 'role', width: 100,
          render: (r) => r === 'admin'
            ? <Tag color="red" bordered={false}>管理员</Tag>
            : <Tag bordered={false}>用户</Tag>,
        },
        {
          title: '操作', width: 90,
          render: (_, r) => r.role === 'admin'
            ? <Text type="secondary"><LockOutlined /> 内置</Text>
            : (
              <Popconfirm title={`删除用户 ${r.username}？其会话将一并删除`} okText="删除" cancelText="取消"
                onConfirm={() => remove(r.id)}>
                <a style={{ color: '#e5484d' }}>删除</a>
              </Popconfirm>
            ),
        },
      ]}
    />
  )
}

/** 管理后台（仅管理员可见入口） */
export default function AdminView({ me, onBack }) {
  return (
    <div className="admin-view">
      <header className="main-header">
        <Button icon={<ArrowLeftOutlined />} onClick={onBack}>返回</Button>
        <div className="main-title">管理后台</div>
        <div className="main-header-side" />
      </header>
      <div className="main-scroll">
        <div style={{ maxWidth: 860, margin: '0 auto', padding: '20px 24px' }}>
          <Tabs
            defaultActiveKey="intent"
            items={[
              { key: 'intent', label: '意图识别管理', children: <IntentRules /> },
              { key: 'users', label: '用户管理', children: <Users me={me} /> },
            ]}
          />
        </div>
      </div>
    </div>
  )
}
