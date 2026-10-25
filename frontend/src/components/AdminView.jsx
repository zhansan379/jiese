import { useCallback, useEffect, useState } from 'react'
import {
  App as AntdApp, Button, Input, Modal, Popconfirm, Select, Switch, Table, Tabs, Tag, Typography,
} from 'antd'
import { ArrowLeftOutlined, LockOutlined, PlusOutlined } from '@ant-design/icons'
import { api } from '../api.js'

const { Title, Text } = Typography

/** 意图树管理：叶子节点（description/examples 供 LLM 打分）+ 动作路由配置 */
function IntentNodes() {
  const { message } = AntdApp.useApp()
  const [nodes, setNodes] = useState(null)
  const [form, setForm] = useState({ code: '', name: '', kind: 'kb', book_id: '', reply: '', description: '', examples: '', gen_prompt: '' })
  const [adding, setAdding] = useState(false)

  const load = useCallback(() => {
    api.adminIntentNodes().then(setNodes).catch((e) => message.error(e.message))
  }, [message])
  useEffect(() => { load() }, [load])

  const add = async () => {
    if (!form.code.trim() || !form.name.trim()) { message.warning('code 与名称必填'); return }
    const node = {
      code: form.code.trim(), name: form.name.trim(), kind: form.kind,
      description: form.description, examples: form.examples, gen_prompt: form.gen_prompt,
      action: form.kind === 'system'
        ? { type: 'system', reply: form.reply || '我只能回答与戒色相关的问题。' }
        : { type: 'kb', book_id: form.book_id || null },
    }
    setAdding(true)
    try {
      await api.adminAddIntentNode(node)
      message.success('节点已添加')
      setForm({ code: '', name: '', kind: 'kb', book_id: '', reply: '', description: '', examples: '', gen_prompt: '' })
      load()
    } catch (e) { message.error(e.message) } finally { setAdding(false) }
  }

  const toggle = async (code, enabled) => {
    try { await api.adminUpdateIntentNode(code, { enabled: !enabled }); load() }
    catch (e) { message.error(e.message) }
  }

  const remove = async (code) => {
    try { await api.adminDelIntentNode(code); message.success('已删除'); load() }
    catch (e) { message.error(e.message) }
  }

  const [editCode, setEditCode] = useState(null)
  const [editReply, setEditReply] = useState('')
  const [saving, setSaving] = useState(false)

  const openEdit = (node) => {
    setEditCode(node.code)
    setEditReply(node.action?.reply || '')
  }
  const saveReply = async () => {
    setSaving(true)
    try {
      await api.adminUpdateIntentNode(editCode, { action: { type: 'system', reply: editReply } })
      message.success('话术已更新，即时生效')
      setEditCode(null)
      load()
    } catch (e) { message.error(e.message) } finally { setSaving(false) }
  }

  return (
    <div>
      <div style={{ display: 'grid', gridTemplateColumns: '120px 150px 130px 150px auto', gap: 8, marginBottom: 12, maxWidth: 940 }}>
        <Input placeholder="code 如 kb_new" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} />
        <Input placeholder="节点名称" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <Select
          value={form.kind}
          onChange={(v) => setForm({ ...form, kind: v })}
          options={[
            { value: 'kb', label: '知识库路由' },
            { value: 'system', label: '系统应答' },
          ]}
        />
        {form.kind === 'kb'
          ? (
            <Select
              value={form.book_id}
              onChange={(v) => setForm({ ...form, book_id: v })}
              options={[
                { value: '', label: '检索范围: 全部书籍' },
                { value: 'jwly', label: '检索范围: 戒为良药' },
                { value: 'jzl', label: '检索范围: 戒者录' },
              ]}
            />
          )
          : <Input placeholder="固定回复话术" value={form.reply} onChange={(e) => setForm({ ...form, reply: e.target.value })} />}
        <Button type="primary" icon={<PlusOutlined />} loading={adding} onClick={add}>添加节点</Button>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 8, maxWidth: 900 }}>
        <Input placeholder="描述（供 LLM 打分理解意图）" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
        <Input placeholder="示例问题，用 | 分隔" value={form.examples} onChange={(e) => setForm({ ...form, examples: e.target.value })} />
      </div>
      <Input.TextArea
        placeholder="生成提示词注入（可选）：该意图命中时追加到回答的风格指引，如“回答以戒友真实经历为主，多引用具体案例”"
        value={form.gen_prompt}
        onChange={(e) => setForm({ ...form, gen_prompt: e.target.value })}
        autoSize={{ minRows: 1, maxRows: 4 }}
        style={{ marginBottom: 14, maxWidth: 900 }}
      />
      <Table
        rowKey="code" size="middle" loading={!nodes} dataSource={nodes || []} pagination={false}
        columns={[
          { title: 'Code', dataIndex: 'code', width: 110 },
          { title: '名称', dataIndex: 'name', width: 150 },
          {
            title: '类型', dataIndex: 'kind', width: 100,
            render: (k) => k === 'system'
              ? <Tag color="orange" bordered={false}>系统应答</Tag>
              : <Tag color="geekblue" bordered={false}>知识库</Tag>,
          },
          {
            title: '动作', width: 130,
            render: (_, r) => r.kind === 'system'
              ? '回复话术'
              : `检索: ${r.action?.book_id === 'jwly' ? '戒为良药' : r.action?.book_id === 'jzl' ? '戒者录' : '全部'}`,
          },
          { title: '描述 / 示例', ellipsis: true, render: (_, r) => (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {r.description}
            </Text>
          ) },
          {
            title: '启用', dataIndex: 'enabled', width: 70,
            render: (v, r) => <Switch size="small" checked={v} onClick={() => toggle(r.code, v)} />,
          },
          {
            title: '操作', width: 150,
            render: (_, r) => (
              <div style={{ display: 'flex', gap: 10 }}>
                {r.kind === 'system' && (
                  <a onClick={() => openEdit(r)}>编辑话术</a>
                )}
                {r.builtin
                  ? <Text type="secondary"><LockOutlined /> 锁定</Text>
                  : (
                    <Popconfirm title="删除该节点？" okText="删除" cancelText="取消" onConfirm={() => remove(r.code)}>
                      <a style={{ color: '#e5484d' }}>删除</a>
                    </Popconfirm>
                  )}
              </div>
            ),
          },
        ]}
      />
      <Modal
        title={`编辑话术 · ${editCode || ''}`}
        open={!!editCode}
        onCancel={() => setEditCode(null)}
        onOk={saveReply}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
      >
        <Input.TextArea
          value={editReply}
          onChange={(e) => setEditReply(e.target.value)}
          autoSize={{ minRows: 4, maxRows: 8 }}
          placeholder="chitchat 命中时直接流式返回这段话"
        />
        <Text type="secondary" style={{ fontSize: 12 }}>保存后即时生效，无需重启。</Text>
      </Modal>
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
              { key: 'tree', label: '意图树', children: <IntentNodes /> },
              
              { key: 'users', label: '用户管理', children: <Users me={me} /> },
            ]}
          />
        </div>
      </div>
    </div>
  )
}
