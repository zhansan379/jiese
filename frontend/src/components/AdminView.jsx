import { useCallback, useEffect, useMemo, useState } from 'react'
import { Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  App as AntdApp, Button, Drawer, Grid, Input, InputNumber, Modal, Popconfirm, Select, Switch,
  Table, Tabs, Tag, Tooltip, Typography,
} from 'antd'
import { ArrowLeftOutlined, LockOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { api } from '../api.js'

const { Text } = Typography
const { useBreakpoint: useBp } = Grid

/** 移动端判定：<768px（与全局侧栏抽屉断点一致） */
function useIsMobile() {
  const screens = useBp()
  return !screens.md
}

const fmtMs = (ms) => (ms == null ? '-' : ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`)
const fmtTime = (s) => (s ? new Date(s).toLocaleString('zh-CN', { hour12: false }) : '-')

const EMPTY_FORM = {
  code: '', name: '', parent_code: null, kind: 'kb',
  book_id: '', reply: '', description: '', examples: '', gen_prompt: '',
}

/** 意图树管理：树形展示（仅叶子是意图节点）、统一编辑弹窗（全字段）、总开关 */
function IntentNodes() {
  const { message } = AntdApp.useApp()
  const isMobile = useIsMobile()
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
    setEditing({ ...EMPTY_FORM, parent_code: parentCode, kind, _isEdit: false })
  }
  const openEdit = (node) => {
    setEditing({
      code: node.code, name: node.name, parent_code: node.parent_code,
      kind: node.kind, book_id: node.action?.book_id || '', reply: node.action?.reply || '',
      description: node.description || '', examples: node.examples || '',
      gen_prompt: node.gen_prompt || '', _isEdit: true,
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
      if (f._isEdit) {
        await api.adminUpdateIntentNode(f.code, payload)
        message.success('节点已更新，即时生效')
      } else {
        if (!f.code?.trim()) { message.warning('code 必填'); setSaving(false); return }
        await api.adminAddIntentNode({ ...payload, code: f.code.trim() })
        message.success('节点已添加')
      }
      setEditing(null)
      load()
    } catch (e) { message.error(e.message) } finally { setSaving(false) }
  }

  const renderAction = (r) => {
    if (r.kind === 'group') return '目录'
    if ((r.children?.length || 0) > 0) return `分组（${r.children.length} 个子节点）`
    if (r.kind === 'system') return '回复话术'
    return `检索: ${r.action?.book_id === 'jwly' ? '戒为良药' : r.action?.book_id === 'jzl' ? '戒者录' : '全部'}`
  }

  const columns = [
    { title: 'Code', dataIndex: 'code', width: 110 },
    { title: '名称', dataIndex: 'name', width: 170 },
    {
      title: '类型', dataIndex: 'kind', width: 100,
      render: (k) => k === 'group'
        ? <Tag bordered={false}>分组</Tag>
        : k === 'system'
          ? <Tag color="orange" bordered={false}>系统应答</Tag>
          : <Tag color="geekblue" bordered={false}>知识库</Tag>,
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
  ].filter((c) => !(isMobile && ['Code', '描述 / 示例'].includes(c.title)))

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 14, flexWrap: 'wrap' }}>
        <Button icon={<PlusOutlined />} onClick={() => openAdd(null, 'kb')}>新建节点</Button>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginLeft: isMobile ? 0 : 'auto' }}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            {treeEnabled ? '意图路由：开启' : '意图路由：关闭（全库检索）'}
          </Text>
          <Switch checked={treeEnabled} onChange={toggleTree} />
        </div>
      </div>

      <Table
        rowKey="code" size="middle" loading={!nodes}
        dataSource={treeData} pagination={false} columns={columns}
        expandable={{ defaultExpandAllRows: true }}
        scroll={isMobile ? { x: 560 } : undefined}
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
        width={isMobile ? '94%' : 560}
      >
        <div style={{ display: 'grid', gridTemplateColumns: isMobile ? '1fr' : '1fr 1fr', gap: 10 }}>
          <Input
            placeholder="code（新增后不可改）"
            value={editing?.code || ''}
            disabled={!!editing?._isEdit}
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
        {editing?.kind === 'kb' && (
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
function Users() {
  const { message } = AntdApp.useApp()
  const isMobile = useIsMobile()
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
      ].filter((c) => !(isMobile && ['ID', '邮箱'].includes(c.title)))}
    />
  )
}

/** 查询词映射（关键词映射）：口语/缩写 → 标准术语，检索前归一化（参考 ragent QueryTermMapping） */
const MATCH_TYPE_OPTIONS = [
  { value: 1, label: '精确（子串替换）' },
  { value: 2, label: '前缀匹配' },
  { value: 3, label: '正则匹配' },
  { value: 4, label: '整词匹配' },
]
const MATCH_TYPE_TEXT = { 1: '精确', 2: '前缀', 3: '正则', 4: '整词' }
const EMPTY_MAPPING_FORM = {
  source_term: '', target_term: '', match_type: 1, priority: 100, enabled: true, remark: '',
}

function TermMappings() {
  const { message } = AntdApp.useApp()
  const isMobile = useIsMobile()
  const [data, setData] = useState(null)          // {total, mappings}
  const [query, setQuery] = useState({ page: 1, keyword: '' })
  const [editing, setEditing] = useState(null)    // 编辑弹窗：{_isEdit, id?, ...form}
  const [saving, setSaving] = useState(false)
  const [testText, setTestText] = useState('')
  const [testResult, setTestResult] = useState(null)

  const load = useCallback(() => {
    api.adminTermMappings({ page: query.page, size: 20, keyword: query.keyword })
      .then(setData).catch((e) => message.error(e.message))
  }, [query, message])
  useEffect(() => { load() }, [load])

  const toggle = async (id, enabled) => {
    try { await api.adminUpdateTermMapping(id, { enabled: !enabled }); load() }
    catch (e) { message.error(e.message) }
  }

  const remove = async (id) => {
    try { await api.adminDelTermMapping(id); message.success('已删除'); load() }
    catch (e) { message.error(e.message) }
  }

  const save = async () => {
    const f = editing
    if (!f.source_term.trim() || !f.target_term.trim()) { message.warning('源词与目标词必填'); return }
    setSaving(true)
    const payload = {
      source_term: f.source_term.trim(), target_term: f.target_term.trim(),
      match_type: f.match_type, priority: f.priority ?? 100,
      enabled: f.enabled, remark: f.remark,
    }
    try {
      if (f._isEdit) {
        await api.adminUpdateTermMapping(f.id, payload)
        message.success('已更新，即时生效')
      } else {
        await api.adminAddTermMapping(payload)
        message.success('已添加，即时生效')
      }
      setEditing(null)
      load()
    } catch (e) { message.error(e.message) } finally { setSaving(false) }
  }

  const runTest = async () => {
    if (!testText.trim()) return
    try { setTestResult(await api.adminTermMappingPreview(testText.trim())) }
    catch (e) { message.error(e.message) }
  }

  const columns = [
    {
      title: '源词 → 目标词', ellipsis: true,
      render: (_, r) => (
        <span>
          <Text code>{r.source_term}</Text>
          <Text type="secondary"> → </Text>
          <Text code>{r.target_term}</Text>
        </span>
      ),
    },
    {
      title: '匹配类型', dataIndex: 'match_type', width: 90,
      render: (v) => <Tag bordered={false}>{MATCH_TYPE_TEXT[v] || v}</Tag>,
    },
    { title: '优先级', dataIndex: 'priority', width: 80 },
    {
      title: '启用', dataIndex: 'enabled', width: 70,
      render: (v, r) => <Switch size="small" checked={v} onClick={() => toggle(r.id, v)} />,
    },
    { title: '备注', dataIndex: 'remark', ellipsis: true, render: (v) => v || '-' },
    { title: '更新时间', dataIndex: 'updated_at', width: 160, render: fmtTime },
    {
      title: '操作', width: 120,
      render: (_, r) => (
        <div style={{ display: 'flex', gap: 10 }}>
          <a onClick={() => setEditing({ ...r, _isEdit: true })}>编辑</a>
          <Popconfirm title={`删除「${r.source_term}」？`} okText="删除" cancelText="取消"
            onConfirm={() => remove(r.id)}>
            <a style={{ color: '#e5484d' }}>删除</a>
          </Popconfirm>
        </div>
      ),
    },
  ].filter((c) => !(isMobile && ['优先级', '备注', '更新时间'].includes(c.title)))

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <Button icon={<PlusOutlined />} onClick={() => setEditing({ ...EMPTY_MAPPING_FORM, _isEdit: false })}>
          新建映射
        </Button>
        <Input.Search
          placeholder="搜索源词 / 目标词 / 备注"
          allowClear style={{ width: isMobile ? '100%' : 240 }}
          onSearch={(kw) => setQuery((q) => ({ ...q, page: 1, keyword: kw }))}
        />
        <Button icon={<ReloadOutlined />} onClick={load} />
        {!isMobile && (
          <Text type="secondary" style={{ fontSize: 13, alignSelf: 'center' }}>
            检索前把口语/缩写归一化为标准术语（如 yy → 意淫），小优先级先应用、长源词优先
          </Text>
        )}
      </div>

      <div style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <Input
          placeholder="试一试：输入一句话看归一化结果"
          value={testText} onChange={(e) => setTestText(e.target.value)}
          onPressEnter={runTest} style={{ width: isMobile ? '100%' : 320 }}
        />
        <Button onClick={runTest}>试算</Button>
        {testResult && (
          <Text style={{ alignSelf: 'center', fontSize: 13, width: isMobile ? '100%' : 'auto' }}>
            结果：<Text code>{testResult.result}</Text>
            {(testResult.hits || []).map((h, i) => (
              <Tag key={i} color="blue" bordered={false} style={{ marginLeft: 4 }}>
                {h.source} → {h.target}（{MATCH_TYPE_TEXT[h.match_type]}）
              </Tag>
            ))}
          </Text>
        )}
      </div>

      <Table
        rowKey="id" size="middle" loading={!data}
        dataSource={data?.mappings || []} columns={columns}
        scroll={isMobile ? { x: 560 } : undefined}
        pagination={{
          current: query.page, pageSize: 20, total: data?.total || 0,
          showSizeChanger: false, showTotal: (t) => `共 ${t} 条`,
          onChange: (p) => setQuery((q) => ({ ...q, page: p })),
        }}
      />

      <Modal
        title={editing?._isEdit ? `编辑映射 · ${editing.source_term}` : '新建查询词映射'}
        open={!!editing}
        onCancel={() => setEditing(null)}
        onOk={save}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        width={isMobile ? '94%' : 520}
      >
        <div style={{ display: 'grid', gridTemplateColumns: isMobile ? '1fr' : '1fr 1fr', gap: 10 }}>
          <Input
            placeholder="源词（口语/缩写，如 yy）"
            value={editing?.source_term || ''}
            onChange={(e) => setEditing({ ...editing, source_term: e.target.value })}
          />
          <Input
            placeholder="目标词（标准术语，如意淫）"
            value={editing?.target_term || ''}
            onChange={(e) => setEditing({ ...editing, target_term: e.target.value })}
          />
          <Select
            value={editing?.match_type ?? 1}
            onChange={(v) => setEditing({ ...editing, match_type: v })}
            options={MATCH_TYPE_OPTIONS}
            style={{ width: '100%' }}
          />
          <InputNumber
            min={0} max={9999} style={{ width: '100%' }}
            value={editing?.priority ?? 100}
            onChange={(v) => setEditing({ ...editing, priority: v })}
            addonBefore="优先级"
          />
          <Input
            placeholder="备注（可选）"
            value={editing?.remark || ''}
            onChange={(e) => setEditing({ ...editing, remark: e.target.value })}
            style={isMobile ? undefined : { gridColumn: 'span 2' }}
          />
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12 }}>
          <Switch
            size="small" checked={editing?.enabled ?? true}
            onChange={(v) => setEditing({ ...editing, enabled: v })}
          />
          <Text type="secondary" style={{ fontSize: 13 }}>启用该规则（停用后不再参与归一化）</Text>
        </div>
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
          保存后即时生效。正则类型请填合法正则；同一源词 + 匹配类型不可重复。
        </Text>
      </Modal>
    </div>
  )
}

/** 链路追踪：Run 分页 + 节点详情（参考 ragent RagTraceController / admin-trace） */
const TRACE_STATUS = {
  RUNNING: { color: 'processing', text: '进行中' },
  SUCCESS: { color: 'success', text: '成功' },
  ERROR: { color: 'error', text: '失败' },
  CANCELLED: { color: 'default', text: '取消' },
}
const NODE_TYPE_COLOR = {
  REWRITE: 'geekblue', INTENT: 'purple', RETRIEVE: 'cyan', LLM: 'orange', USER_TTFT: 'green',
}

function Traces() {
  const { message } = AntdApp.useApp()
  const isMobile = useIsMobile()
  const [data, setData] = useState(null)          // {total, runs}
  const [query, setQuery] = useState({ page: 1, status: '', keyword: '' })
  const [detail, setDetail] = useState(null)      // {run, nodes} | 'loading'
  const [expanded, setExpanded] = useState(null)  // 展开节点 extra_data 的 node_id

  const load = useCallback(() => {
    api.adminTraceRuns({ page: query.page, size: 20, status: query.status, keyword: query.keyword })
      .then(setData).catch((e) => message.error(e.message))
  }, [query, message])
  useEffect(() => { load() }, [load])

  const openDetail = async (traceId) => {
    setDetail('loading')
    setExpanded(null)
    try { setDetail(await api.adminTraceDetail(traceId)) }
    catch (e) { setDetail(null); message.error(e.message) }
  }

  const maxDuration = useMemo(() => {
    const nodes = detail?.nodes || []
    return Math.max(1, ...nodes.map((n) => n.duration_ms || 0))
  }, [detail])

  const columns = [
    { title: '时间', dataIndex: 'start_time', width: 160, render: fmtTime },
    {
      title: '问题', dataIndex: 'question', ellipsis: true,
      render: (v, r) => v
        ? <Tooltip title={v}><span>{v}</span></Tooltip>
        : <Text type="secondary">{r.entry_method}</Text>,
    },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (v) => {
        const m = TRACE_STATUS[v] || { color: 'default', text: v }
        return <Tag color={m.color} bordered={false}>{m.text}</Tag>
      },
    },
    { title: '总耗时', dataIndex: 'duration_ms', width: 90, render: fmtMs },
    { title: '首包', dataIndex: 'ttft_ms', width: 90, render: fmtMs },
    {
      title: 'Trace', dataIndex: 'trace_id', width: 100,
      render: (v) => <Text code style={{ fontSize: 12 }}>{v.slice(0, 8)}</Text>,
    },
    { title: '操作', width: 70, render: (_, r) => <a onClick={() => openDetail(r.trace_id)}>详情</a> },
  ].filter((c) => !(isMobile && ['Trace', '首包'].includes(c.title)))

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <Select
          placeholder="状态" allowClear style={{ width: 120 }}
          value={query.status || undefined}
          onChange={(v) => setQuery((q) => ({ ...q, page: 1, status: v || '' }))}
          options={Object.entries(TRACE_STATUS).map(([value, m]) => ({ value, label: m.text }))}
        />
        <Input.Search
          placeholder="搜索问题 / Trace / 会话 ID"
          allowClear style={{ width: isMobile ? '100%' : 260 }}
          onSearch={(kw) => setQuery((q) => ({ ...q, page: 1, keyword: kw }))}
        />
        <Button icon={<ReloadOutlined />} onClick={load} />
        {!isMobile && (
          <Text type="secondary" style={{ fontSize: 13, alignSelf: 'center' }}>
            每次问答记录一条链路：归一化 → 改写 → 意图 → 检索 → 生成的各节点耗时与输入输出
          </Text>
        )}
      </div>

      <Table
        rowKey="trace_id" size="middle" loading={!data}
        dataSource={data?.runs || []} columns={columns}
        scroll={isMobile ? { x: 560 } : undefined}
        pagination={{
          current: query.page, pageSize: 20, total: data?.total || 0,
          showSizeChanger: false, showTotal: (t) => `共 ${t} 条`,
          onChange: (p) => setQuery((q) => ({ ...q, page: p })),
        }}
      />

      <Drawer
        title="链路详情"
        open={!!detail && detail !== 'loading'}
        onClose={() => setDetail(null)}
        width={isMobile ? '94%' : 560}
        destroyOnClose
      >
        {detail && detail !== 'loading' && (() => {
          const { run, nodes } = detail
          return (
            <div>
              <div style={{ marginBottom: 12 }}>
                <Tag color={(TRACE_STATUS[run.status] || {}).color} bordered={false}>
                  {(TRACE_STATUS[run.status] || {}).text || run.status}
                </Tag>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {fmtTime(run.start_time)} · 总耗时 {fmtMs(run.duration_ms)} · 首包 {fmtMs(run.ttft_ms)}
                </Text>
              </div>
              <Text code style={{ fontSize: 12, display: 'block', marginBottom: 10 }}>
                {run.trace_id}
              </Text>
              {run.question && (
                <div style={{ marginBottom: 10 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>问题</Text>
                  <div>{run.question}</div>
                </div>
              )}
              {run.session_id && (
                <div style={{ marginBottom: 10 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>会话</Text>
                  <div><Text code style={{ fontSize: 12 }}>{run.session_id}</Text></div>
                </div>
              )}
              {run.error_message && (
                <div style={{
                  marginBottom: 12, padding: '8px 10px', borderRadius: 6,
                  background: '#fff2f0', color: '#e5484d', fontSize: 12,
                }}>
                  {run.error_message}
                </div>
              )}
              <Text strong style={{ fontSize: 13 }}>节点（{nodes.length}）</Text>
              <div style={{ marginTop: 10 }}>
                {nodes.map((n) => {
                  const st = TRACE_STATUS[n.status] || { color: 'default', text: n.status }
                  const extras = n.extra_data && Object.keys(n.extra_data).length > 0 ? n.extra_data : null
                  return (
                    <div key={n.node_id} style={{ marginBottom: 10 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                        <Tag color={NODE_TYPE_COLOR[n.node_type] || 'default'} bordered={false} style={{ marginRight: 0 }}>
                          {n.node_type}
                        </Tag>
                        <Text strong style={{ fontSize: 13 }}>{n.node_name}</Text>
                        <Tag color={st.color} bordered={false} style={{ fontSize: 11, lineHeight: '16px' }}>
                          {st.text}
                        </Tag>
                        <Text type="secondary" style={{ fontSize: 12, marginLeft: 'auto' }}>
                          {fmtMs(n.duration_ms)}
                        </Text>
                      </div>
                      <div style={{
                        height: 4, background: '#f0f0f0', borderRadius: 2,
                        marginTop: 6, overflow: 'hidden',
                      }}>
                        <div style={{
                          width: `${Math.max(2, ((n.duration_ms || 0) / maxDuration) * 100)}%`,
                          height: '100%',
                          background: n.status === 'ERROR' ? '#e5484d' : '#6c8cff',
                          borderRadius: 2,
                        }} />
                      </div>
                      {n.error_message && (
                        <div style={{ color: '#e5484d', fontSize: 12, marginTop: 4 }}>{n.error_message}</div>
                      )}
                      {extras && (
                        <div style={{ marginTop: 4 }}>
                          <a style={{ fontSize: 12 }} onClick={() => setExpanded(expanded === n.node_id ? null : n.node_id)}>
                            {expanded === n.node_id ? '收起输入输出' : '查看输入输出'}
                          </a>
                          {expanded === n.node_id && (
                            <pre style={{
                              fontSize: 11, background: '#fafafa', borderRadius: 6,
                              padding: 8, marginTop: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                            }}>
                              {JSON.stringify(extras, null, 2)}
                            </pre>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )
        })()}
      </Drawer>
    </div>
  )
}

/** 术语与关系（实体标注 + 受限关系）：词条表格（增删改）+ 关系边表格（证据数 + 跳原文） */
const TERM_TYPES = ['症状', '方法', '因素', '概念']
const TERM_TYPE_COLOR = { 症状: 'red', 方法: 'green', 因素: 'orange', 概念: 'blue' }
const EDGE_REL_COLOR = { 对治: 'green', 导致: 'orange' }

/** para_ids 兼容数组/JSON 字符串（后端历史版本返回 TEXT），解析失败按空处理 */
function parseParaIds(v) {
  if (Array.isArray(v)) return v.filter(Boolean)
  if (typeof v === 'string' && v.trim()) {
    try {
      const a = JSON.parse(v)
      return Array.isArray(a) ? a.filter(Boolean) : []
    } catch { return [] }
  }
  return []
}

/** 证据跳原文：from=admin + back=来源路径，阅读页返回按钮回到对应管理 Tab */
function chunkLinkParams(c, back = '/admin/tree') {
  const params = new URLSearchParams({ from: 'admin', back })
  const pids = parseParaIds(c.para_ids)
  if (pids.length) {
    params.set('para', pids[0])
    params.set('hl', pids.join(','))
  }
  return `/reader/${c.doc_id}?${params}`
}

function Terms() {
  const { message } = AntdApp.useApp()
  const isMobile = useIsMobile()
  const [data, setData] = useState(null)
  const [query, setQuery] = useState({ page: 1, keyword: '', type: '' })
  const [editing, setEditing] = useState(null)   // {_isEdit, term?, type, aliases, enabled}
  const [saving, setSaving] = useState(false)

  const load = useCallback(() => {
    api.adminTerms({ page: query.page, keyword: query.keyword, type: query.type })
      .then(setData).catch((e) => message.error(e.message))
  }, [query, message])
  useEffect(() => { load() }, [load])

  const toggle = async (r) => {
    try { await api.adminUpdateTerm(r.term, { enabled: !r.enabled }); load() }
    catch (e) { message.error(e.message) }
  }

  const remove = async (term) => {
    try { await api.adminDelTerm(term); message.success('已删除'); load() }
    catch (e) { message.error(e.message) }
  }

  const save = async () => {
    const f = editing
    if (!f.term?.trim()) { message.warning('术语必填'); return }
    const aliases = (f.aliases || '').split(/[;；,，]/).map((s) => s.trim()).filter(Boolean)
    setSaving(true)
    try {
      if (f._isEdit) {
        await api.adminUpdateTerm(f.term, { type: f.type, aliases, enabled: f.enabled })
        message.success('已更新，即时生效')
      } else {
        await api.adminAddTerm({ term: f.term.trim(), type: f.type, aliases, enabled: f.enabled })
        message.success('已添加，即时生效')
      }
      setEditing(null)
      load()
    } catch (e) { message.error(e.message) } finally { setSaving(false) }
  }

  const columns = [
    { title: '术语', dataIndex: 'term', width: 150, render: (v) => <Text code>{v}</Text> },
    {
      title: '类型', dataIndex: 'type', width: 90,
      render: (t) => <Tag color={TERM_TYPE_COLOR[t] || 'default'} bordered={false}>{t}</Tag>,
    },
    {
      title: '同义词', ellipsis: true,
      render: (_, r) => (r.aliases || []).length
        ? (r.aliases || []).map((a) => <Tag key={a} bordered={false} style={{ marginRight: 4 }}>{a}</Tag>)
        : <Text type="secondary">-</Text>,
    },
    {
      title: '启用', dataIndex: 'enabled', width: 70,
      render: (v, r) => <Switch size="small" checked={v} onClick={() => toggle(r)} />,
    },
    {
      title: '操作', width: 130,
      render: (_, r) => (
        <div style={{ display: 'flex', gap: 10 }}>
          <a onClick={() => setEditing({
            _isEdit: true, term: r.term, type: r.type,
            aliases: (r.aliases || []).join(';'), enabled: r.enabled,
          })}>编辑</a>
          <Popconfirm title={`删除「${r.term}」？其关系边保留`} okText="删除" cancelText="取消"
            onConfirm={() => remove(r.term)}>
            <a style={{ color: '#e5484d' }}>删除</a>
          </Popconfirm>
        </div>
      ),
    },
  ].filter((c) => !(isMobile && c.title === '同义词'))

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <Button icon={<PlusOutlined />} onClick={() => setEditing({ term: '', type: '症状', aliases: '', enabled: true })}>
          新建术语
        </Button>
        <Select
          placeholder="类型" allowClear style={{ width: 100 }}
          value={query.type || undefined}
          onChange={(v) => setQuery((q) => ({ ...q, page: 1, type: v || '' }))}
          options={TERM_TYPES.map((t) => ({ value: t, label: t }))}
        />
        <Input.Search
          placeholder="搜索术语" allowClear style={{ width: isMobile ? '100%' : 220 }}
          onSearch={(kw) => setQuery((q) => ({ ...q, page: 1, keyword: kw }))}
        />
        {!isMobile && (
          <Text type="secondary" style={{ fontSize: 13, alignSelf: 'center' }}>
            检索加权与关系挖掘依赖术语类型（症状/方法/因素）；词条增删改即时生效
          </Text>
        )}
      </div>
      <Table
        rowKey="term" size="middle" loading={!data}
        dataSource={data?.terms || []} columns={columns}
        scroll={isMobile ? { x: 480 } : undefined}
        pagination={{
          current: query.page, pageSize: 20, total: data?.total || 0,
          showSizeChanger: false, showTotal: (t) => `共 ${t} 条`,
          onChange: (p) => setQuery((q) => ({ ...q, page: p })),
        }}
      />
      <Modal
        title={editing?._isEdit ? `编辑术语 · ${editing.term}` : '新建术语'}
        open={!!editing}
        onCancel={() => setEditing(null)}
        onOk={save} okText="保存" cancelText="取消" confirmLoading={saving}
        width={isMobile ? '94%' : 480}
      >
        <Input
          placeholder="术语（如 固肾功）" value={editing?.term || ''}
          disabled={!!editing?._isEdit}
          onChange={(e) => setEditing({ ...editing, term: e.target.value })}
        />
        <div style={{ display: 'grid', gridTemplateColumns: isMobile ? '1fr' : '1fr 1fr', gap: 10, marginTop: 10 }}>
          <Select
            value={editing?.type || '症状'}
            onChange={(v) => setEditing({ ...editing, type: v })}
            options={TERM_TYPES.map((t) => ({ value: t, label: t }))}
            style={{ width: '100%' }}
          />
          <Input
            placeholder="同义词，用 ; 分隔（如 梦遗;滑精）"
            value={editing?.aliases || ''}
            onChange={(e) => setEditing({ ...editing, aliases: e.target.value })}
          />
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12 }}>
          <Switch size="small" checked={editing?.enabled ?? true}
            onChange={(v) => setEditing({ ...editing, enabled: v })} />
          <Text type="secondary" style={{ fontSize: 13 }}>启用（停用后不参与匹配与加权）</Text>
        </div>
      </Modal>
    </div>
  )
}

function Edges() {
  const { message } = AntdApp.useApp()
  const isMobile = useIsMobile()
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [query, setQuery] = useState({ page: 1, rel: '', keyword: '' })
  const [evidence, setEvidence] = useState({})   // edgeId → [{chunk...}]
  const [loadingEv, setLoadingEv] = useState(null)

  const load = useCallback(() => {
    api.adminTermEdges({ page: query.page, rel: query.rel, keyword: query.keyword })
      .then(setData).catch((e) => message.error(e.message))
  }, [query, message])
  useEffect(() => { load() }, [load])

  const openEvidence = async (id) => {
    if (evidence[id]) return
    setLoadingEv(id)
    try {
      const rows = await api.adminTermEdgeEvidence(id)
      setEvidence((e) => ({ ...e, [id]: rows }))
    } catch (err) { message.error(err.message) }
    finally { setLoadingEv(null) }
  }

  const remove = async (id) => {
    try { await api.adminDelTermEdge(id); message.success('已删除噪声边'); load() }
    catch (e) { message.error(e.message) }
  }

  const columns = [
    {
      title: '关系', ellipsis: true,
      render: (_, r) => (
        <span>
          <Text code>{r.src_term}</Text>
          <Text type="secondary"> —</Text>
          <Tag color={EDGE_REL_COLOR[r.rel] || 'default'} bordered={false} style={{ margin: '0 2px' }}>{r.rel}</Tag>
          <Text type="secondary">→ </Text>
          <Text code>{r.dst_term}</Text>
        </span>
      ),
    },
    {
      title: '证据数', dataIndex: 'support', width: 90,
      render: (v) => <Tag bordered={false} color={v >= 2 ? 'blue' : 'default'}>{v} chunks</Tag>,
    },
    {
      title: '操作', width: 110,
      render: (_, r) => (
        <Popconfirm title="删除该关系边？" okText="删除" cancelText="取消" onConfirm={() => remove(r.id)}>
          <a style={{ color: '#e5484d' }}>删除</a>
        </Popconfirm>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <Select
          placeholder="关系" allowClear style={{ width: 100 }}
          value={query.rel || undefined}
          onChange={(v) => setQuery((q) => ({ ...q, page: 1, rel: v || '' }))}
          options={Object.keys(EDGE_REL_COLOR).map((r) => ({ value: r, label: r }))}
        />
        <Input.Search
          placeholder="搜索源词 / 目标词" allowClear style={{ width: isMobile ? '100%' : 220 }}
          onSearch={(kw) => setQuery((q) => ({ ...q, page: 1, keyword: kw }))}
        />
        {!isMobile && (
          <Text type="secondary" style={{ fontSize: 13, alignSelf: 'center' }}>
            方法—对治→问题、因素—导致→症状；运行时仅使用证据 ≥2 的边（由 scripts/mine_edges.py 挖掘）
          </Text>
        )}
      </div>
      <Table
        rowKey="id" size="middle" loading={!data}
        dataSource={data?.edges || []} columns={columns}
        scroll={isMobile ? { x: 480 } : undefined}
        expandable={{
          onExpand: (expanded, r) => expanded && openEvidence(r.id),
          expandedRowRender: (r) => (
            <div style={{ padding: '4px 0' }}>
              {loadingEv === r.id && !evidence[r.id] && <Text type="secondary">加载证据中…</Text>}
              {(evidence[r.id] || []).map((c) => (
                <div key={c.id} style={{ marginBottom: 8 }}>
                  <a
                    onClick={() => navigate(chunkLinkParams(c, '/admin/entities?sub=edges'))}
                    style={{ fontSize: 12 }}
                  >
                    {c.breadcrumb}
                  </a>
                  <div style={{ fontSize: 12, color: '#666', marginTop: 2 }}>{c.text}…</div>
                </div>
              ))}
              {evidence[r.id] && !evidence[r.id].length && (
                <Text type="secondary">证据 chunk 已不存在</Text>
              )}
            </div>
          ),
        }}
        pagination={{
          current: query.page, pageSize: 20, total: data?.total || 0,
          showSizeChanger: false, showTotal: (t) => `共 ${t} 条`,
          onChange: (p) => setQuery((q) => ({ ...q, page: p })),
        }}
      />
    </div>
  )
}

function TermsRelations() {
  const isMobile = useIsMobile()
  const [searchParams, setSearchParams] = useSearchParams()
  const sub = searchParams.get('sub') === 'edges' ? 'edges' : 'terms'
  return (
    <Tabs
      activeKey={sub}
      onChange={(k) => {
        const next = new URLSearchParams(searchParams)
        if (k === 'terms') next.delete('sub')
        else next.set('sub', k)
        setSearchParams(next, { replace: true })
      }}
      size={isMobile ? 'small' : 'middle'}
      items={[
        { key: 'terms', label: '术语表', children: <Terms /> },
        { key: 'edges', label: '关系边', children: <Edges /> },
      ]}
    />
  )
}

/** 经验共识（Claim 共识层）：按主题分类浏览，停用劣质簇、展开查看证据 chunk */
const TOPIC_COLORS = {
  念头与断念: 'purple', 警惕与破戒: 'red', 症状与对治: 'orange', 养生与恢复: 'green',
  学习与觉悟: 'geekblue', 情绪与心态: 'cyan', 行善与修德: 'gold', 方法与功课: 'blue',
  动机与观念: 'magenta', 生活习惯: 'volcano', 中医与医药: 'lime', 其他: 'default',
}

function Claims() {
  const { message } = AntdApp.useApp()
  const isMobile = useIsMobile()
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  // 默认只看运行期可用（≥3）：避免几千条单发断言淹没视图；可清空筛选看全量
  const [query, setQuery] = useState({ page: 1, keyword: '', minSupport: 3, topic: '' })
  const [topics, setTopics] = useState([])
  const [evidence, setEvidence] = useState({})
  const [loadingEv, setLoadingEv] = useState(null)

  const load = useCallback(() => {
    api.adminClaims({
      page: query.page, keyword: query.keyword,
      minSupport: query.minSupport, topic: query.topic,
    }).then(setData).catch((e) => message.error(e.message))
  }, [query, message])
  useEffect(() => { load() }, [load])
  useEffect(() => { api.adminClaimTopics().then(setTopics).catch(() => {}) }, [])

  const toggle = async (r) => {
    try { await api.adminUpdateClaim(r.id, { enabled: !r.enabled }); load() }
    catch (e) { message.error(e.message) }
  }

  const openEvidence = async (id) => {
    if (evidence[id]) return
    setLoadingEv(id)
    try {
      const rows = await api.adminClaimEvidence(id)
      setEvidence((e) => ({ ...e, [id]: rows }))
    } catch (err) { message.error(err.message) }
    finally { setLoadingEv(null) }
  }

  const fmtBooks = (books) => {
    const names = { jwly: '戒为良药', jzl: '戒者录' }
    return Object.entries(books || {})
      .map(([b, n]) => `${names[b] || b} ${n}处`)
      .join('，') || '-'
  }

  const columns = [
    {
      title: '主题', dataIndex: 'topic', width: 110,
      render: (t) => <Tag bordered={false} color={TOPIC_COLORS[t] || 'default'}>{t || '其他'}</Tag>,
    },
    { title: '规范表述', dataIndex: 'text', ellipsis: !isMobile },
    {
      title: '支持', dataIndex: 'support_authors', width: isMobile ? 130 : 110,
      render: (v, r) => <Tag bordered={false} color="geekblue">{v} 位作者 · {r.support_chunks}处</Tag>,
    },
    { title: '书籍分布', dataIndex: 'books', width: 170, render: fmtBooks },
    {
      title: '启用', dataIndex: 'enabled', width: 70,
      render: (v, r) => <Switch size="small" checked={v} onClick={() => toggle(r)} />,
    },
  ].filter((c) => !(isMobile && ['书籍分布', '主题'].includes(c.title)))

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <Select
          placeholder="主题" allowClear
          style={{ width: isMobile ? '100%' : 190 }}
          value={query.topic || undefined}
          onChange={(v) => setQuery((q) => ({ ...q, page: 1, topic: v || '' }))}
          options={topics.map((t) => ({
            value: t.topic,
            label: `${t.topic}（${t.count} / 运行期 ${t.runtime_count}）`,
          }))}
        />
        <Input.Search
          placeholder="搜索共识表述" allowClear style={{ width: isMobile ? '100%' : 240 }}
          onSearch={(kw) => setQuery((q) => ({ ...q, page: 1, keyword: kw }))}
        />
        <Select
          placeholder="最低支持度" allowClear style={{ width: 140 }}
          value={query.minSupport || undefined}
          onChange={(v) => setQuery((q) => ({ ...q, page: 1, minSupport: v || 0 }))}
          options={[3, 5, 10, 20].map((n) => ({ value: n, label: `≥ ${n} chunks` }))}
        />
        {!isMobile && (
          <Text type="secondary" style={{ fontSize: 13, alignSelf: 'center' }}>
            默认仅看运行期可用（≥3）；主题为第二级分类，可由 build_claims.py 重新生成
          </Text>
        )}
      </div>
      <Table
        rowKey="id" size="middle" loading={!data}
        dataSource={data?.claims || []} columns={columns}
        scroll={isMobile ? { x: 560 } : undefined}
        expandable={{
          onExpand: (expanded, r) => expanded && openEvidence(r.id),
          expandedRowRender: (r) => (
            <div style={{ padding: '4px 0' }}>
              {loadingEv === r.id && !evidence[r.id] && <Text type="secondary">加载证据中…</Text>}
              {(evidence[r.id] || []).map((c) => (
                <div key={c.id} style={{ marginBottom: 8 }}>
                  <a
                    onClick={() => navigate(chunkLinkParams(c, '/admin/claims'))}
                    style={{ fontSize: 12 }}
                  >
                    {c.breadcrumb}{c.author ? `｜${c.author}` : ''}
                  </a>
                  <div style={{ fontSize: 12, color: '#666', marginTop: 2 }}>{c.text}…</div>
                </div>
              ))}
              {evidence[r.id] && !evidence[r.id].length && (
                <Text type="secondary">证据 chunk 已不存在</Text>
              )}
            </div>
          ),
        }}
        pagination={{
          current: query.page, pageSize: 20, total: data?.total || 0,
          showSizeChanger: false, showTotal: (t) => `共 ${t} 条`,
          onChange: (p) => setQuery((q) => ({ ...q, page: p })),
        }}
      />
    </div>
  )
}

/** 管理后台（仅管理员可见入口），tab 路由化：/admin/tree|terms|traces|users|entities|claims */
const ADMIN_TABS = ['tree', 'terms', 'traces', 'users', 'entities', 'claims']

export default function AdminView({ me, authReady, onBack }) {
  const isMobile = useIsMobile()
  const { tab } = useParams()
  const navigate = useNavigate()
  const activeTab = ADMIN_TABS.includes(tab) ? tab : 'tree'
  // 刷新后 user 异步恢复，恢复完成前不判定权限，避免把管理员误弹回首页
  if (!authReady) return null
  if (me?.role !== 'admin') return <Navigate to="/" replace />
  return (
    <div className="admin-view">
      <header className="main-header">
        <Button icon={<ArrowLeftOutlined />} onClick={onBack}>返回</Button>
        <div className="main-title">管理后台</div>
        <div className="main-header-side" />
      </header>
      <div className="main-scroll">
        <div style={{ maxWidth: 960, margin: '0 auto', padding: isMobile ? '12px 12px' : '20px 24px' }}>
          <Tabs
            activeKey={activeTab}
            onChange={(k) => navigate(`/admin/${k}`)}
            items={[
              { key: 'tree', label: '意图树', children: <IntentNodes /> },
              { key: 'terms', label: '查询词映射', children: <TermMappings /> },
              { key: 'entities', label: '术语与关系', children: <TermsRelations /> },
              { key: 'claims', label: '经验共识', children: <Claims /> },
              { key: 'traces', label: '链路追踪', children: <Traces /> },
              { key: 'users', label: '用户管理', children: <Users /> },
            ]}
          />
        </div>
      </div>
    </div>
  )
}
