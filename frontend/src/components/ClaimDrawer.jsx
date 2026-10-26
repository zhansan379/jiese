import { useEffect, useMemo, useState } from 'react'
import { Drawer, Empty, Grid, Spin, Tag, Typography } from 'antd'
import { api } from '../api.js'

const { Text } = Typography
const { useBreakpoint: useBp } = Grid

const BOOK_NAMES = { jwly: '戒为良药', jzl: '戒者录' }

/**
 * 经验共识证据抽屉：答案内「共识」胶囊点击打开。
 * 展示规范表述 / 支持度 / 书籍分布 / 关联摘录 [n] + 证据原文（点击跳原文高亮）。
 */
export default function ClaimDrawer({ claimId, citations, onClose, onOpenSource, onJumpCite }) {
  const screens = useBp()
  const isMobile = !screens.md
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!claimId) return undefined
    let alive = true
    setData(null)
    setError(null)
    setLoading(true)
    api.claimEvidence(claimId)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(e.message))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [claimId])

  // 该共识在本次答案里的关联摘录编号
  const citeIndexes = useMemo(() => {
    if (!claimId) return []
    const out = []
    ;(citations || []).forEach((c, i) => {
      if ((c.claims || []).some((cl) => cl.id === claimId)) out.push(i + 1)
    })
    return out
  }, [citations, claimId])

  const claim = data?.claim
  const evidence = data?.evidence || []
  const books = claim?.books || {}

  return (
    <Drawer
      title="经验共识 · 证据"
      open={!!claimId}
      onClose={onClose}
      width={isMobile ? '94%' : 560}
      destroyOnClose
    >
      {loading && (
        <div style={{ padding: '32px 0', textAlign: 'center' }}>
          <Spin /> <Text type="secondary" style={{ marginLeft: 8 }}>加载证据中…</Text>
        </div>
      )}
      {error && <Empty description={`证据加载失败：${error}`} />}
      {claim && (
        <div>
          <div className="claim-quote">{claim.text}</div>

          <div className="claim-meta">
            <Tag color="geekblue" bordered={false}>
              {claim.support_authors} 位作者 · {claim.support_chunks} 处
            </Tag>
            {claim.topic && <Tag color="purple" bordered={false}>{claim.topic}</Tag>}
            {Object.entries(books).map(([b, n]) => (
              <Tag key={b} bordered={false}>{BOOK_NAMES[b] || b} {n} 处</Tag>
            ))}
          </div>

          {citeIndexes.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>本次答案引用：</Text>
              {citeIndexes.map((n) => (
                <Tag
                  key={n}
                  color="blue"
                  bordered={false}
                  style={{ cursor: 'pointer', marginLeft: 4 }}
                  onClick={() => onJumpCite?.(n)}
                >
                  [{n}]
                </Tag>
              ))}
            </div>
          )}

          <div className="claim-evidence-title">
            <Text strong>证据原文</Text>
            <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
              {evidence.length} 条 · 点击跳转原文并高亮
            </Text>
          </div>

          <div className="claim-evidence-list">
            {evidence.map((c) => (
              <button
                type="button"
                key={c.id}
                className="claim-evidence-item"
                onClick={() => onOpenSource?.(c)}
              >
                <div className="claim-evidence-head">
                  <span className="claim-evidence-crumb">{c.breadcrumb}</span>
                  {c.author && <span className="claim-evidence-author">{c.author}</span>}
                </div>
                <div className="claim-evidence-text">{c.text}…</div>
              </button>
            ))}
            {!evidence.length && <Empty description="暂无证据" />}
          </div>

          <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 14 }}>
            共识由书中多位作者的同义表述聚合而来（support ≥ 3 才参与回答）；证据为原文片段，
            点击任意条目可在阅读页定位高亮。
          </Text>
        </div>
      )}
    </Drawer>
  )
}
