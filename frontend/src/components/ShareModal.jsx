import { useEffect, useMemo, useRef, useState } from 'react'
import { App as AntdApp, Checkbox, Spin } from 'antd'
import {
  CheckOutlined, CloseOutlined, CopyOutlined, DownloadOutlined, LeftOutlined,
} from '@ant-design/icons'
import { renderShareImage } from '../shareImage.js'
import { copyText } from '../clipboard.js'
import { api } from '../api.js'

const fmtDay = (ts) => {
  const d = new Date(ts || Date.now())
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

/** 分享链接地址：站点为 HashRouter，形如 https://host/#/s/abc123 */
function buildShareUrl(id) {
  const { origin, pathname } = window.location
  return `${origin}${pathname}#/s/${id}`
}

/**
 * 分享多选页（参考 DeepSeek 分享抽屉）：勾选若干会话 → 生成公开链接 或 对话长图。
 * 预览阶段可下载 PNG 或返回重选；链接阶段可复制/打开/撤销。
 */
export default function ShareModal({ open, sessions, defaultSessionId, onClose }) {
  const { message: toast } = AntdApp.useApp()
  const [selected, setSelected] = useState(() => new Set())
  const [phase, setPhase] = useState('pick')        // pick | linking | link | rendering | preview
  const [imgUrl, setImgUrl] = useState('')
  const [link, setLink] = useState(null)            // { id, url }
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  const urlRef = useRef('')
  const copiedTimer = useRef(0)

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
    setLink(null)
    setCopied(false)
    setError('')
  }, [open, defaultSessionId])   // eslint-disable-line react-hooks/exhaustive-deps

  // 关闭/重生成时释放 blob URL
  useEffect(() => () => {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current)
    clearTimeout(copiedTimer.current)
  }, [])

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

  const pickedList = () => shareable.filter((s) => selected.has(s.id))

  const generateLink = async () => {
    const list = pickedList()
    if (!list.length) return
    setPhase('linking')
    setError('')
    try {
      const res = await api.createShareLink(list)
      setLink({ id: res.id, url: buildShareUrl(res.id) })
      setPhase('link')
    } catch (e) {
      setError(e.message || '链接生成失败，请重试')
      setPhase('pick')
    }
  }

  const copyLink = async () => {
    if (!link) return
    const ok = await copyText(link.url)
    if (!ok) {
      toast.error('复制失败，请手动选中链接复制')
      return
    }
    setCopied(true)
    clearTimeout(copiedTimer.current)
    copiedTimer.current = setTimeout(() => setCopied(false), 1800)
  }

  const revokeLink = async () => {
    if (!link) return
    try {
      await api.deleteShare(link.id)
      toast.success('已撤销该分享链接')
    } catch (e) {
      toast.error(e.message || '撤销失败，请稍后再试')
    }
    setLink(null)
    setPhase('pick')
  }

  const generateImage = async () => {
    const list = pickedList()
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

  const busy = phase === 'linking' || phase === 'rendering'
  const headTitle = phase === 'preview'
    ? '分享图片已生成'
    : phase === 'link'
      ? '分享链接已生成'
      : '分享对话'

  return (
    <div className="share-overlay">
      <div className="share-panel">
        <div className="share-head">
          <span className="share-head-title">{headTitle}</span>
          <button type="button" className="share-close" onClick={onClose} aria-label="关闭">
            <CloseOutlined />
          </button>
        </div>

        {phase === 'pick' && (
          <div className="share-subtitle">勾选要分享的对话，可生成公开链接或一张长图（都不包含深度思考内容）</div>
        )}
        {phase === 'link' && (
          <div className="share-subtitle">任何拿到链接的人都能查看这些对话，请确认没有隐私内容</div>
        )}

        {phase === 'preview' && (
          <div className="share-preview">
            <img src={imgUrl} alt="分享图预览" />
          </div>
        )}

        {phase === 'link' && link && (
          <div className="share-linkbox">
            <div className="share-link-row">
              <input
                className="share-link-input"
                value={link.url}
                readOnly
                onFocus={(e) => e.target.select()}
                aria-label="分享链接"
              />
              <button type="button" className="share-go" onClick={copyLink}>
                {copied ? <><CheckOutlined /> 已复制</> : <><CopyOutlined /> 复制</>}
              </button>
            </div>
            <div className="share-link-actions">
              <a className="share-link-open" href={link.url} target="_blank" rel="noreferrer">
                在新标签页打开
              </a>
              <button type="button" className="share-link-revoke" onClick={revokeLink}>
                撤销链接
              </button>
            </div>
          </div>
        )}

        {(phase === 'pick' || phase === 'linking' || phase === 'rendering') && (
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
          ) : phase === 'link' ? (
            <>
              <span className="share-count">链接已生成，可复制发给朋友</span>
              <button type="button" className="share-go" onClick={onClose}>完成</button>
            </>
          ) : (
            <>
              <Checkbox checked={allOn} onChange={toggleAll} disabled={!shareable.length}>
                全选
              </Checkbox>
              <span className="share-count">
                {phase === 'linking' ? '正在生成链接…'
                  : phase === 'rendering' ? '正在生成图片…'
                    : (error || `已选择 ${selected.size} 组对话`)}
              </span>
              <button type="button" className="share-cancel" onClick={onClose} disabled={busy}>取消</button>
              <button
                type="button" className="share-outline"
                disabled={!selected.size || busy}
                onClick={generateLink}
              >
                {phase === 'linking' ? <><Spin size="small" /> 生成中…</> : '生成链接'}
              </button>
              <button
                type="button" className="share-go"
                disabled={!selected.size || busy}
                onClick={generateImage}
              >
                {phase === 'rendering' ? <><Spin size="small" /> 生成中…</> : '生成图片'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
