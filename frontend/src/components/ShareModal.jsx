import { useEffect, useMemo, useRef, useState } from 'react'
import { Checkbox, Spin } from 'antd'
import { CloseOutlined, DownloadOutlined, LeftOutlined } from '@ant-design/icons'
import { renderShareImage } from '../shareImage.js'

const fmtDay = (ts) => {
  const d = new Date(ts || Date.now())
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

/**
 * 分享多选页（参考 DeepSeek 分享抽屉）：勾选若干会话 → 生成对话长图（非链接）。
 * 预览阶段可下载 PNG 或返回重选。
 */
export default function ShareModal({ open, sessions, defaultSessionId, onClose }) {
  const [selected, setSelected] = useState(() => new Set())
  const [phase, setPhase] = useState('pick')        // pick | rendering | preview
  const [imgUrl, setImgUrl] = useState('')
  const [error, setError] = useState('')
  const urlRef = useRef('')

  const shareable = useMemo(
    () => sessions.filter((s) => s.messages?.some((m) => m.role === 'assistant' && m.content)),
    [sessions]
  )

  useEffect(() => {
    if (!open) return
    setSelected(new Set(defaultSessionId && shareable.some((s) => s.id === defaultSessionId)
      ? [defaultSessionId]
      : []))
    setPhase('pick')
    setError('')
  }, [open, defaultSessionId])   // eslint-disable-line react-hooks/exhaustive-deps

  // 关闭/重生成时释放 blob URL
  useEffect(() => () => { if (urlRef.current) URL.revokeObjectURL(urlRef.current) }, [])

  const toggle = (id) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const allOn = shareable.length > 0 && selected.size === shareable.length
  const toggleAll = () => setSelected(allOn ? new Set() : new Set(shareable.map((s) => s.id)))

  const generate = async () => {
    const list = shareable.filter((s) => selected.has(s.id))
    if (!list.length) return
    setPhase('rendering')
    setError('')
    try {
      const blob = await renderShareImage(list)
      if (urlRef.current) URL.revokeObjectURL(urlRef.current)
      urlRef.current = URL.createObjectURL(blob)
      setImgUrl(urlRef.current)
      setPhase('preview')
    } catch (e) {
      setError(e.message || '生成失败，请重试')
      setPhase('pick')
    }
  }

  const download = () => {
    if (!imgUrl) return
    const a = document.createElement('a')
    const d = new Date()
    a.href = imgUrl
    a.download = `戒色知识库_${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}.png`
    a.click()
  }

  if (!open) return null

  return (
    <div className="share-overlay">
      <div className="share-panel">
        <div className="share-head">
          <span className="share-head-title">
            {phase === 'preview' ? '分享图片已生成' : '分享对话'}
          </span>
          <button type="button" className="share-close" onClick={onClose} aria-label="关闭">
            <CloseOutlined />
          </button>
        </div>

        {phase !== 'preview' && (
          <div className="share-subtitle">勾选要分享的对话，将生成一张长图（不包含深度思考内容）</div>
        )}

        {phase === 'preview' ? (
          <div className="share-preview">
            <img src={imgUrl} alt="分享图预览" />
          </div>
        ) : (
          <div className="share-list">
            {shareable.length === 0 && (
              <div className="share-subtitle" style={{ padding: '20px 12px', textAlign: 'center' }}>
                还没有可分享的对话
              </div>
            )}
            {shareable.map((s) => {
              const qa = s.messages.filter((m) => m.role === 'user').length
              return (
                <div key={s.id} className="share-item" onClick={() => toggle(s.id)}>
                  <Checkbox
                    checked={selected.has(s.id)}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggle(s.id)}
                  />
                  <div className="share-item-info">
                    <div className="share-item-title">{s.title}</div>
                    <div className="share-item-meta">{fmtDay(s.createdAt)} · {qa} 组问答</div>
                  </div>
                </div>
              )
            })}
          </div>
        )}

        <div className="share-foot">
          {phase === 'preview' ? (
            <>
              <button type="button" className="share-cancel" onClick={() => setPhase('pick')}>
                <LeftOutlined /> 重新选择
              </button>
              <span className="share-count">长按或右键图片可保存，也可点击下载</span>
              <button type="button" className="share-go" onClick={download}>
                <DownloadOutlined /> 下载图片
              </button>
            </>
          ) : (
            <>
              <Checkbox checked={allOn} onChange={toggleAll} disabled={!shareable.length}>
                全选
              </Checkbox>
              <span className="share-count">
                {phase === 'rendering' ? '正在生成图片…' : (error || `已选择 ${selected.size} 组对话`)}
              </span>
              <button type="button" className="share-cancel" onClick={onClose}>取消</button>
              <button
                type="button" className="share-go"
                disabled={!selected.size || phase === 'rendering'}
                onClick={generate}
              >
                {phase === 'rendering' ? <><Spin size="small" /> 生成中…</> : '生成分享图片'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
