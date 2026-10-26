import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert, App as AntdApp, Button, Card, Empty, Grid, Input, Modal, Pagination, Select,
  Skeleton, Tag, Tabs, Typography,
} from 'antd'
import { BulbOutlined, ClusterOutlined, PartitionOutlined } from '@ant-design/icons'
import { api } from '../api.js'

// ECharts 体量较大，单独分包：只有进入知识图谱时才加载（不影响问答首屏）
const GraphChart = lazy(() => import('./GraphChart.jsx'))

const { Text, Paragraph } = Typography
const { useBreakpoint: useBp } = Grid

/** 移动端判定：<768px（与全局侧栏抽屉断点一致） */
function useIsMobile() {
  const screens = useBp()
  return !screens.md
}

const TERM_TYPE_COLOR = { 症状: 'red', 方法: 'green', 因素: 'orange', 概念: 'blue' }
const EDGE_REL_COLOR = { 对治: 'green', 导致: 'orange' }
const BOOK_NAMES = { jwly: '戒为良药', jzl: '戒者录' }

const TOPIC_COLORS = {
  念头与断念: 'purple', 警惕与破戒: 'red', 症状与对治: 'orange', 养生与恢复: 'green',
  学习与觉悟: 'geekblue', 情绪与心态: 'cyan', 行善与修德: 'gold', 方法与功课: 'blue',
  动机与观念: 'magenta', 生活习惯: 'volcano', 中医与医药: 'lime', 其他: 'default',
}

/** 证据段落兼容数组 / JSON 字符串（后端历史数据为 TEXT） */
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

/** 证据跳原文：from=explore，阅读页返回按钮回到知识探索对应页签 */
function evidenceLink(c, back = '/explore/graph') {
  const params = new URLSearchParams({ from: 'explore', back })
  const pids = parseParaIds(c.para_ids)
  if (pids.length) {
    params.set('para', pids[0])
    params.set('hl', pids.join(','))
  }
  return `/reader/${c.doc_id}?${params}`
}

const fmtBooks = (books) => Object.entries(books || {})
  .map(([b, n]) => `${BOOK_NAMES[b] || b} ${n}处`)
  .join('，')

/** 证据列表：章节面包屑可点回原文（默认只展示前 8 处，避免长列表刷屏） */
function EvidenceList({ rows, loading, back, onOpen }) {
  const [showAll, setShowAll] = useState(false)
  if (loading) return <Text type="secondary">加载原文证据…</Text>
  if (!rows?.length) return <Text type="secondary">原文证据已不可用</Text>
  const limit = 8
  const shown = showAll ? rows : rows.slice(0, limit)
  return (
    <div className="explore-evidence">
      {shown.map((c) => (
        <div key={c.id} className="explore-evidence-item">
          <a onClick={() => onOpen(evidenceLink(c, back))}>
            {c.breadcrumb || c.chapter_title || '原文'}{c.author ? `｜${c.author}` : ''}
          </a>
          <div className="explore-evidence-text">{c.text}…</div>
        </div>
      ))}
      {rows.length > limit && (
        <Button type="link" size="small" style={{ paddingLeft: 0 }} onClick={() => setShowAll((v) => !v)}>
          {showAll ? '收起' : `显示全部 ${rows.length} 处`}
        </Button>
      )}
    </div>
  )
}

/** 知识图谱：术语（节点）+ 受限关系（边），点开看原文证据 */
function GraphPanel({ overview }) {
  const { message } = AntdApp.useApp()
  const isMobile = useIsMobile()
  const navigate = useNavigate()
  const [termData, setTermData] = useState(null)
  const [edges, setEdges] = useState(null)
  const [graph, setGraph] = useState(null)
  const [query, setQuery] = useState({ page: 1, keyword: '', rel: '' })
  const [text, setText] = useState('')
  const [openEdge, setOpenEdge] = useState(null)
  const [evidence, setEvidence] = useState({})
  const [loadingEv, setLoadingEv] = useState(null)
  // 图上点连线 → 弹窗看原文出处
  const [detail, setDetail] = useState(null)   // {edge, rows, loading}

  const loadTerms = useCallback(() => {
    api.exploreTerms({ size: 100, keyword: query.keyword })
      .then(setTermData).catch((e) => message.error(e.message))
  }, [query.keyword, message])

  const loadEdges = useCallback(() => {
    api.exploreEdges({ page: query.page, keyword: query.keyword, rel: query.rel, minSupport: 2 })
      .then(setEdges).catch((e) => message.error(e.message))
  }, [query, message])

  // 图表数据：只取证据最强的 Top 60 条边，避免力导向图过密
  const loadGraph = useCallback(() => {
    api.exploreGraph({ keyword: query.keyword, rel: query.rel, limit: 60 })
      .then(setGraph).catch(() => setGraph({ nodes: [], links: [] }))
  }, [query.keyword, query.rel])

  useEffect(() => { loadTerms() }, [loadTerms])
  useEffect(() => { loadEdges() }, [loadEdges])
  useEffect(() => { loadGraph() }, [loadGraph])

  const toggleEdge = async (id) => {
    if (openEdge === id) { setOpenEdge(null); return }
    setOpenEdge(id)
    if (evidence[id]) return
    setLoadingEv(id)
    try {
      const rows = await api.exploreEdgeEvidence(id)
      setEvidence((e) => ({ ...e, [id]: rows }))
    } catch (err) { message.error(err.message) }
    finally { setLoadingEv(null) }
  }

  /** 点图上的连线：弹窗展示该关系的原文出处（与列表共享证据缓存） */
  const openDetail = async (edge) => {
    setDetail({ edge, rows: evidence[edge.id], loading: !evidence[edge.id] })
    if (evidence[edge.id]) return
    try {
      const rows = await api.exploreEdgeEvidence(edge.id)
      setEvidence((e) => ({ ...e, [edge.id]: rows }))
      setDetail((d) => (d?.edge.id === edge.id ? { ...d, rows, loading: false } : d))
    } catch (err) {
      message.error(err.message)
      setDetail((d) => (d?.edge.id === edge.id ? { ...d, rows: [], loading: false } : d))
    }
  }

  // 点击概念标签：把它作为关键词过滤关系边，便于顺着一个概念看它连向哪里
  const pickTerm = (term) => {
    const next = query.keyword === term ? '' : term
    setText(next)
    setQuery((q) => ({ ...q, page: 1, keyword: next }))
  }

  return (
    <div>
      <Alert
        type="info" showIcon icon={<PartitionOutlined />} className="explore-intro"
        message="知识图谱来自全书的术语标注与证据挖掘"
        description="「方法 —对治→ 问题」「因素 —导致→ 症状」两类关系只有被至少 2 处原文支持才会出现在这里。上方图谱可拖拽/滚轮缩放，点击概念聚焦、点击连线看原文；下方按关系列出明细，展开即可读原文出处。"
      />

      <div className="explore-filter">
        <Input.Search
          placeholder="搜索概念或关系，如 固肾功 / 梦遗"
          allowClear
          value={text}
          style={{ width: isMobile ? '100%' : 280 }}
          onChange={(e) => {
            const v = e.target.value
            setText(v)
            if (!v) setQuery((q) => ({ ...q, page: 1, keyword: '' }))
          }}
          onSearch={(v) => setQuery((q) => ({ ...q, page: 1, keyword: v.trim() }))}
        />
        <Select
          placeholder="关系类型" allowClear
          style={{ width: 130 }}
          value={query.rel || undefined}
          onChange={(v) => setQuery((q) => ({ ...q, page: 1, rel: v || '' }))}
          options={Object.keys(EDGE_REL_COLOR).map((r) => ({ value: r, label: r }))}
        />
        <Text type="secondary" className="explore-filter-hint">
          {overview ? `全库 ${overview.terms.total} 个概念 · ${overview.edges.total} 条关系` : ' '}
        </Text>
      </div>

      <div className="explore-graph-card">
        <div className="explore-graph-head">
          <span className="explore-section-title">关系图谱总览</span>
          <Text type="secondary" className="explore-graph-tip">
            {graph ? `证据最强的 ${graph.links.length} 条关系` : '加载中…'}
            {isMobile
              ? ' · 拖动平移 · 双指缩放 · 点节点聚焦'
              : ' · 拖拽/滚轮缩放，点概念聚焦，点连线看原文'}
          </Text>
        </div>
        {!graph && <Skeleton active paragraph={{ rows: 6 }} />}
        {graph && graph.links.length > 0 && (
          <Suspense fallback={<Skeleton active paragraph={{ rows: 6 }} />}>
            <GraphChart
              nodes={graph.nodes}
              links={graph.links}
              selected={query.keyword}
              height={isMobile ? 340 : 460}
              onSelectNode={pickTerm}
              onSelectEdge={openDetail}
            />
          </Suspense>
        )}
        {graph && graph.links.length === 0 && (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可绘制的关联，换个概念或类型试试" />
        )}
      </div>

      <div className="explore-section-title">概念标签</div>
      <div className="explore-chip-row">
        {!termData && <Skeleton.Button active size="small" style={{ width: 120 }} />}
        {termData?.terms.length === 0 && <Text type="secondary">没有匹配的概念，换个词试试</Text>}
        {termData?.terms.map((t) => (
          <Tag
            key={t.term}
            className={`explore-chip${query.keyword === t.term ? ' active' : ''}`}
            color={TERM_TYPE_COLOR[t.type] || 'default'}
            bordered={false}
            role="button"
            onClick={() => pickTerm(t.term)}
          >
            {t.term}
          </Tag>
        ))}
      </div>
      {termData && termData.total > termData.terms.length && (
        <Text type="secondary" className="explore-filter-hint">
          共 {termData.total} 个概念，输入关键词可查找更多
        </Text>
      )}

      <div className="explore-section-title">
        关系明细
        {query.keyword && <span className="explore-section-sub">（概念：{query.keyword}）</span>}
      </div>

      {!edges && <Skeleton active paragraph={{ rows: 4 }} />}
      {edges?.edges.length === 0 && (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有找到关系，换个关键词或类型" />
      )}
      {edges?.edges.map((e) => (
        <Card key={e.id} size="small" className="explore-edge">
          <div className="explore-edge-head">
            <div className="explore-edge-line">
              <Text code>{e.src_term}</Text>
              <Tag color={EDGE_REL_COLOR[e.rel] || 'default'} bordered={false}>{e.rel}</Tag>
              <Text type="secondary">→</Text>
              <Text code>{e.dst_term}</Text>
            </div>
            <Button type="link" size="small" onClick={() => toggleEdge(e.id)}>
              {openEdge === e.id ? '收起' : `${e.support} 处原文`}
            </Button>
          </div>
          {openEdge === e.id && (
            <EvidenceList
              rows={evidence[e.id]} loading={loadingEv === e.id}
              back="/explore/graph" onOpen={navigate}
            />
          )}
        </Card>
      ))}

      {edges?.total > 20 && (
        <Pagination
          className="explore-pager"
          current={query.page} pageSize={20} total={edges.total}
          showSizeChanger={false} showTotal={(t) => `共 ${t} 条关系`}
          onChange={(p) => setQuery((q) => ({ ...q, page: p }))}
        />
      )}

      <Modal
        open={!!detail}
        title={detail ? `${detail.edge.source} ${detail.edge.rel} ${detail.edge.target}` : ''}
        footer={null}
        width={isMobile ? '94%' : 720}
        onCancel={() => setDetail(null)}
      >
        {detail && (
          <>
            <Text type="secondary" className="explore-filter-hint">
              原文证据 {detail.edge.support} 处；点击章节标题可跳转原文
            </Text>
            <EvidenceList
              rows={detail.rows}
              loading={detail.loading}
              back="/explore/graph"
              onOpen={(to) => { setDetail(null); navigate(to) }}
            />
          </>
        )}
      </Modal>
    </div>
  )
}

/** 经验共识：社区反复出现的观点簇，按主题浏览，展开看每句结论的原文出处 */
function ClaimsPanel({ overview }) {
  const { message } = AntdApp.useApp()
  const isMobile = useIsMobile()
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [topics, setTopics] = useState([])
  const [query, setQuery] = useState({ page: 1, keyword: '', topic: '', minSupport: 3 })
  const [text, setText] = useState('')
  const [openClaim, setOpenClaim] = useState(null)
  const [evidence, setEvidence] = useState({})
  const [loadingEv, setLoadingEv] = useState(null)

  useEffect(() => { api.exploreClaimTopics().then(setTopics).catch(() => {}) }, [])

  const load = useCallback(() => {
    api.exploreClaims({
      page: query.page, keyword: query.keyword,
      topic: query.topic, minSupport: query.minSupport,
    }).then(setData).catch((e) => message.error(e.message))
  }, [query, message])
  useEffect(() => { load() }, [load])

  const toggleClaim = async (id) => {
    if (openClaim === id) { setOpenClaim(null); return }
    setOpenClaim(id)
    if (evidence[id]) return
    setLoadingEv(id)
    try {
      const rows = await api.exploreClaimEvidence(id)
      setEvidence((e) => ({ ...e, [id]: rows }))
    } catch (err) { message.error(err.message) }
    finally { setLoadingEv(null) }
  }

  return (
    <div>
      <Alert
        type="info" showIcon icon={<BulbOutlined />} className="explore-intro"
        message="经验共识是很多篇文章里反复讲到的同一结论"
        description="我们把书里意思相近的表述聚成一簇，只有出现在至少 3 处原文、并被多人提到的观点才会显示。它不是标准答案，但可以作为学习时的参照，展开即可核对原文。"
      />

      <div className="explore-filter">
        <Select
          placeholder="主题" allowClear
          style={{ width: isMobile ? '100%' : 190 }}
          value={query.topic || undefined}
          onChange={(v) => setQuery((q) => ({ ...q, page: 1, topic: v || '' }))}
          options={topics.map((t) => ({
            value: t.topic, label: `${t.topic}（${t.runtime_count}）`,
          }))}
        />
        <Input.Search
          placeholder="搜索共识表述" allowClear
          style={{ width: isMobile ? '100%' : 240 }}
          value={text}
          onChange={(e) => {
            const v = e.target.value
            setText(v)
            if (!v) setQuery((q) => ({ ...q, page: 1, keyword: '' }))
          }}
          onSearch={(v) => setQuery((q) => ({ ...q, page: 1, keyword: v.trim() }))}
        />
        <Select
          placeholder="最低支持度" allowClear style={{ width: 140 }}
          value={query.minSupport || undefined}
          onChange={(v) => setQuery((q) => ({ ...q, page: 1, minSupport: v || 0 }))}
          options={[3, 5, 10, 20].map((n) => ({ value: n, label: `≥ ${n} 处原文` }))}
        />
        <Text type="secondary" className="explore-filter-hint">
          {overview ? `全库 ${overview.claims.total} 条可用共识` : ' '}
        </Text>
      </div>

      {!data && <Skeleton active paragraph={{ rows: 6 }} />}
      {data?.claims.length === 0 && (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的共识，换个主题或关键词" />
      )}
      {data?.claims.map((c) => (
        <Card key={c.id} size="small" className="explore-claim">
          <div className="explore-claim-head">
            <Tag color={TOPIC_COLORS[c.topic] || 'default'} bordered={false}>{c.topic || '其他'}</Tag>
            <Tag bordered={false} color="geekblue">
              {c.support_authors} 位作者 · {c.support_chunks} 处原文
            </Tag>
          </div>
          <Paragraph className="explore-claim-text">{c.text}</Paragraph>
          <div className="explore-claim-meta">
            {fmtBooks(c.books) || '出处见原文'}
            <Button type="link" size="small" onClick={() => toggleClaim(c.id)}>
              {openClaim === c.id ? '收起原文' : '查看原文出处'}
            </Button>
          </div>
          {openClaim === c.id && (
            <EvidenceList
              rows={evidence[c.id]} loading={loadingEv === c.id}
              back="/explore/claims" onOpen={navigate}
            />
          )}
        </Card>
      ))}

      {data?.total > 0 && (
        <Pagination
          className="explore-pager"
          current={query.page} pageSize={10} total={data.total}
          showSizeChanger={false} showTotal={(t) => `共 ${t} 条共识`}
          onChange={(p) => setQuery((q) => ({ ...q, page: p }))}
        />
      )}
    </div>
  )
}

/**
 * 知识探索（公开只读，游客与普通用户均可访问）：
 * 「经验共识」看反复出现的观点簇（默认页签），「知识图谱」看术语与证据关系；
 * 两条路径都能下钻到原文，帮助用户顺着知识脉络学习。
 */
export default function ExploreView() {
  const isMobile = useIsMobile()
  const navigate = useNavigate()
  const { tab } = useParams()
  // 默认落在「经验共识」：对普通用户更易读，图谱作为进阶入口
  const active = tab === 'graph' ? 'graph' : 'claims'
  const [overview, setOverview] = useState(null)

  useEffect(() => { api.exploreOverview().then(setOverview).catch(() => {}) }, [])

  return (
    <div className="explore-content">
      <div className="explore-stats">
        <div className="explore-stat">
          <span className="explore-stat-icon"><ClusterOutlined /></span>
          <div>
            <div className="explore-stat-num">{overview?.terms.total ?? '-'}</div>
            <div className="explore-stat-label">概念术语</div>
          </div>
        </div>
        <div className="explore-stat">
          <span className="explore-stat-icon"><PartitionOutlined /></span>
          <div>
            <div className="explore-stat-num">{overview?.edges.total ?? '-'}</div>
            <div className="explore-stat-label">证据关系</div>
          </div>
        </div>
        <div className="explore-stat">
          <span className="explore-stat-icon"><BulbOutlined /></span>
          <div>
            <div className="explore-stat-num">{overview?.claims.total ?? '-'}</div>
            <div className="explore-stat-label">共识条目</div>
          </div>
        </div>
      </div>

      <Tabs
        activeKey={active}
        size={isMobile ? 'small' : 'middle'}
        onChange={(k) => navigate(`/explore/${k}`)}
        items={[
          { key: 'claims', label: '经验共识', children: <ClaimsPanel overview={overview} /> },
          { key: 'graph', label: '知识图谱', children: <GraphPanel overview={overview} /> },
        ]}
      />
    </div>
  )
}
