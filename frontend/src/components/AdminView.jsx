import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  App as AntdApp, Button, Input, Modal, Popconfirm, Select, Switch, Table, Tabs, Tag, Typography,
} from 'antd'
import { ArrowLeftOutlined, LockOutlined, PlusOutlined } from '@ant-design/icons'
import { api } from '../api.js'

const { Text } = Typography

const EMPTY_FORM = {
  code: '', name: '', parent_code: null, kind: 'kb',
  book_id: '', reply: '', description: '', examples: '', gen_prompt: '',
}

/** 意图树管理：树形展示（仅叶子是意图节点）、统一编辑弹窗（全字段）、总开关 */
function IntentNodes() {
  const { message } = AntdApp.useApp()
  const [nodes, setNodes] = useState(null)
  const [treeEnabled, setTreeEnabled] = useState(true)
  const [editing, setEditing] = useState(null)     // 编辑弹窗：{code?, ...form}
  const [dragCode, setDragCode] = useState(null)
  const [overCode, setOverCode] = useState(null)
  const [saving, setSaving] = useState(false)
  const [adding, setAdding] = useState(false)

  const load = useCallback(() => {
    api.adminIntentNodes().then(setNodes).catch((e) => message.error(e.message))
    api.adminIntentTreeEnabled().then((d) => setTreeEnabled(d.enabled)).catch(() => {})
  }, [message])
  useEffect(() => { load() }, [load])

  // 平铺 → 树形（有子节点的作为分组行；分组不参与分类，仅叶子是意图节点）
  const treeData = useMemo(() => {
    const list = nodes || []
    const byParent = {}
    for (const n of list) {
      const p = n.parent_code || null
      ;(byParent[p] = byParent[p] || []).push(n)
    }
    const withChildren = new Set(list.filter((n) => n.parent_code).map((n) => n.parent_code))
    const build = (parent) =>
      (byParent[parent] || []).map((n) => {
        const children = withChildren.has(n.code) ? build(n.code) : undefined
        return children ? { ...n, children } : { ...n }
      })
    return build(null)
  }, [nodes])

  const groupOptions = useMemo(
    () => [
      { value: null, label: '（顶层分组）' },
      ...(nodes || [])
        .filter((n) => (nodes || []).some((x) => x.parent_code === n.code) || n.parent_code === null)
        .map((n) => ({ value: n.code, label: `${n.name} (${n.code})` })),
    ],
    [nodes],
  )

  const toggleTree = async (enabled) => {
    try {
      await api.adminSetIntentTreeEnabled(enabled)
      setTreeEnabled(enabled)
      message.success(enabled ? '意图树已开启：按节点路由' : '意图树已关闭：全部走全库检索')
    } catch (e) { message.error(e.message) }
  }

  const toggleNode = async (code, enabled) => {
    try { await api.adminUpdateIntentNode(code, { enabled: !enabled }); load() }
    catch (e) { message.error(e.message) }
  }

  const remove = async (code) => {
    try { await api.adminDelIntentNode(code); message.success('已删除'); load() }
    catch (e) { message.error(e.message) }
  }

  const openAdd = (parentCode = null, kind = 'kb') => {
    setEditing({ ...EMPTY_FORM, parent_code: parentCode, kind })
  }
  const openEdit = (node) => {
    setEditing({
      code: node.code, name: node.name, parent_code: node.parent_code,
      kind: node.kind, book_id: node.action?.book_id || '', reply: node.action?.reply || '',
      description: node.description || '', examples: node.examples || '',
      gen_prompt: node.gen_prompt || '',
    })
  }

  const save = async () => {
    const f = editing
    if (!f.name.trim()) { message.warning('名称必填'); return }
    const payload = {
      name: f.name.trim(), parent_code: f.parent_code || null,
      description: f.description, examples: f.examples, gen_prompt: f.gen_prompt,
      kind: f.kind,
      action: f.kind === 'system'
        ? { type: 'system', reply: f.reply || '我只能回答与戒色相关的问题。' }
        : f.kind === 'group'
          ? {}
          : { type: 'kb', book_id: f.book_id || null },
    }
    setSaving(true)
    try {
      if (f.code) {
        await api.adminUpdateIntentNode(f.code, payload)
        message.success('节点已更新，即时生效')
      } else {
        if (!f.code?.trim() && f.code !== null) { /* 新增需要 code */ }
        if (!f.code || !f.code.trim()) { message.warning('code 必填'); setSaving(false); return }
        await api.adminAddIntentNode({ ...payload, code: f.code.trim() })
        message.success('节点已添加')
      }
      setEditing(null)
      load()
    } catch (e) { message.error(e.message) } finally { setSaving(false) }
  }

  const renderAction = (r) => {
    if ((r.children?.length || 0) > 0) return `分组（${r.children.length} 个子节点）`
    if (r.kind === 'system') return '回复话术'
    return `检索: ${r.action?.book_id === 'jwly' ? '戒为良药' : r.action?.book_id === 'jzl' ? '戒者录' : '全部'}`
  }

  const columns = [
    { title: 'Code', dataIndex: 'code', width: 110 },
    { title: '名称', dataIndex: 'name', width: 170 },
    {
      title: '类型', dataIndex: 'kind', width: 100,
      render: (k, r) => (r.children?.length
        ? <Tag bordered={false}>分组</Tag>
        : k === 'system'
          ? <Tag color="orange" bordered={false}>系统应答</Tag>
          : <Tag color="geekblue" bordered={false}>知识库</Tag>),
    },
    { title: '动作', width: 140, render: renderAction },
    {
      title: '描述 / 示例', ellipsis: true,
      render: (_, r) => <Text type="secondary" style={{ fontSize: 12 }}>{r.description}</Text>,
    },
    {
      title: '启用', dataIndex: 'enabled', width: 70,
      render: (v, r) => <Switch size="small" checked={v} onClick={() => toggleNode(r.code, v)} />,
    },
    {
      title: '操作', width: 170,
      render: (_, r) => (
        <div style={{ display: 'flex', gap: 10 }}>
          <a onClick={() => openEdit(r)}>编辑</a>
          {(r.children?.length || 0) > 0 && (
            <a onClick={() => openAdd(r.code, 'kb')}>添加子节点</a>
          )}
          {(r.children?.length || 0) === 0 && (
            <Popconfirm title={`删除 ${r.code}？`} okText="删除" cancelText="取消" onConfirm={() => remove(r.code)}>
              <a style={{ color: '#e5484d' }}>删除</a>
            </Popconfirm>
          )}
        </div>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 14 }}>
        <Button icon={<PlusOutlined />} onClick={() => openAdd(null, 'kb')}>新建节点</Button>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginLeft: 'auto' }}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            {treeEnabled ? '意图路由：开启（按节点打分路由）' : '意图路由：关闭（全部走全库检索）'}
          </Text>
          <Switch checked={treeEnabled} onChange={toggleTree} />
        </div>
      </div>

      <Table
        rowKey="code" size="middle" loading={!nodes}
        dataSource={treeData} pagination={false} columns={columns}
        expandable={{ defaultExpandAllRows: true }}
        onRow={(r) => ({
          draggable: (r.children?.length || 0) === 0,
          onDragStart: () => setDragCode(r.code),
          onDragEnd: () => { setDragCode(null); setOverCode(null) },
          onDragOver: (e) => {
            if (!dragCode || dragCode === r.code) return
            e.preventDefault()
            setOverCode(r.code)
          },
          onDragLeave: () => setOverCode((cur) => (cur === r.code ? null : cur)),
          onDrop: async () => {
            const code = dragCode
            setDragCode(null); setOverCode(null)
            if (!code || code === r.code) return
            // 分组行 = 移入该分组（追加到末尾）；叶子行 = 插到它后面
            const intoGroup = (r.children?.length || 0) > 0 || r.kind === 'group'
            try {
              await api.adminMoveIntentNode(
                code,
                intoGroup ? r.code : (r.parent_code || null),
                intoGroup ? null : r.code,
              )
              message.success('已移动')
              load()
            } catch (e) { message.error(e.message) }
          },
          className: dragCode === r.code
            ? 'row-dragging'
            : overCode === r.code ? 'row-drag-over' : '',
        })}
      />

      <Modal
        title={editing?.code
          ? `编辑节点 · ${editing.code}`
          : `新建${editing?.kind === 'group' ? '分组目录' : editing?.kind === 'system' ? '系统应答节点' : '意图节点'}`}
        open={!!editing}
        onCancel={() => setEditing(null)}
        onOk={save}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        width={560}
      >
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <Input
            placeholder="code（新增后不可改）"
            value={editing?.code || ''}
            disabled={!!editing?.code}
            onChange={(e) => setEditing({ ...editing, code: e.target.value })}
          />
          <Input
            placeholder="节点名称"
            value={editing?.name || ''}
            onChange={(e) => setEditing({ ...editing, name: e.target.value })}
          />
          <Select
            placeholder="上级目录（分组）"
            value={editing?.parent_code ?? null}
            onChange={(v) => setEditing({ ...editing, parent_code: v })}
            options={groupOptions}
            style={{ width: '100%' }}
          />
          <Select
            value={editing?.kind || 'kb'}
            onChange={(v) => setEditing({ ...editing, kind: v })}
            options={[
              { value: 'kb', label: '知识库路由' },
              { value: 'system', label: '系统应答' },
              { value: 'group', label: '分组（目录）' },
            ]}
            style={{ width: '100%' }}
          />
        </div>
        {editing?.kind === 'system'
          ? (
            <Input.TextArea
              placeholder="固定回复话术：命中该意图时直接流式返回这段话"
              value={editing?.reply || ''}
              onChange={(e) => setEditing({ ...editing, reply: e.target.value })}
              autoSize={{ minRows: 2, maxRows: 6 }}
              style={{ marginTop: 10 }}
            />
          )
          : editing?.kind === 'kb' && (
            <Select
              placeholder="检索范围"
              value={editing?.book_id || ''}
              onChange={(v) => setEditing({ ...editing, book_id: v })}
              options={[
                { value: '', label: '检索范围: 全部书籍' },
                { value: 'jwly', label: '检索范围: 戒为良药' },
                { value: 'jzl', label: '检索范围: 戒者录' },
              ]}
              style={{ width: '100%', marginTop: 10 }}
            />
          )}
        {editing?.kind !== 'group' && (
        <Input
          placeholder="描述（供 LLM 打分理解该意图）"
          value={editing?.description || ''}
          onChange={(e) => setEditing({ ...editing, description: e.target.value })}
          style={{ marginTop: 10 }}
        />
        )}
        {editing?.kind !== 'group' && (
        <Input
          placeholder="示例问题，用 | 分隔（供 LLM 打分参考）"
          value={editing?.examples || ''}
          onChange={(e) => setEditing({ ...editing, examples: e.target.value })}
          style={{ marginTop: 10 }}
        />
        )}
        {editing?.kind !== 'group' && (
        <Input.TextArea
          placeholder="生成提示词注入（可选）：该意图命中时追加到回答的风格指引"
          value={editing?.gen_prompt || ''}
          onChange={(e) => setEditing({ ...editing, gen_prompt: e.target.value })}
          autoSize={{ minRows: 2, maxRows: 6 }}
          style={{ marginTop: 10 }}
        />
        )}
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
          {editing?.kind === 'group'
            ? '分组仅用于组织目录，不参与意图打分与生成。'
            : '保存后即时生效。仅叶子节点（无子节点）参与意图打分；分组仅用于组织目录。'}
        </Text>
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
        <div style={{ maxWidth: 960, margin: '0 auto', padding: '20px 24px' }}>
          <Tabs
            defaultActiveKey="tree"
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
