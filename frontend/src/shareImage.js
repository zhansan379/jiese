/**
 * 分享长图生成：纯 Canvas 绘制（无第三方依赖）。
 * 版式参考 DeepSeek 分享卡：顶部品牌区（logo + 应用名 + 日期）、
 * 用户问题为品牌蓝右对齐气泡、AI 回答为左对齐正文、底部水印。
 */

const W = 750
const PAD = 48
const BRAND = '#4D6BFE'
const TEXT_MAIN = '#262A3A'
const TEXT_SUB = '#5E6772'
const TEXT_FAINT = '#9AA0B5'
const SANS = '-apple-system, "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif'
const MONO = 'Consolas, "Courier New", monospace'
const MAX_ANSWER_CHARS = 4000     // 单条回答超长截断，防止画布高度溢出

function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2)
  ctx.beginPath()
  ctx.moveTo(x + rr, y)
  ctx.arcTo(x + w, y, x + w, y + h, rr)
  ctx.arcTo(x + w, y + h, x, y + h, rr)
  ctx.arcTo(x, y + h, x, y, rr)
  ctx.arcTo(x, y, x + w, y, rr)
  ctx.closePath()
}

/** 中英混排换行：中文逐字断行，英文单词尽量不拆 */
function wrapText(ctx, text, maxW) {
  if (!text) return ['']
  const isLatin = (ch) => /[A-Za-z0-9]/.test(ch)
  const lines = []
  let cur = ''
  for (const ch of text) {
    const test = cur + ch
    if (cur && ctx.measureText(test).width > maxW) {
      let breakAt = -1
      if (isLatin(ch)) {
        for (let k = cur.length - 1; k > 0; k--) {
          if (cur[k] === ' ') { breakAt = k; break }
          if (!isLatin(cur[k])) break
        }
      }
      if (breakAt > 0) {
        lines.push(cur.slice(0, breakAt))
        cur = cur.slice(breakAt + 1) + ch
      } else {
        lines.push(cur)
        cur = ch
      }
    } else {
      cur = test
    }
  }
  if (cur) lines.push(cur)
  return lines.length ? lines : ['']
}

/** Markdown → 文本行描述（去语法符号；代码块按 mono 分组） */
export function mdToLines(md = '') {
  const out = []
  let inCode = false
  let codeBuf = null
  const flushCode = () => {
    if (codeBuf) { out.push({ code: codeBuf }); codeBuf = null }
  }
  for (const raw of String(md).split('\n')) {
    const line = raw.trimEnd()
    if (line.trim().startsWith('```')) {
      if (inCode) { flushCode(); inCode = false } else { inCode = true; codeBuf = [] }
      continue
    }
    if (inCode) { (codeBuf ||= []).push(raw); continue }
    if (!line.trim()) { flushCode(); out.push({ blank: true }); continue }
    let s = line
      .replace(/^#{1,6}\s*/, '')
      .replace(/^>\s?/, '')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/\*([^*]+)\*/g, '$1')
      .replace(/`([^`]+)`/g, '$1')
      .replace(/^\s*[-*+]\s+/, '• ')
      .replace(/^\|(.+)\|\s*$/, (_, cells) => cells.split('|').map((c) => c.trim()).join('  '))
    if (!s.trim()) continue
    flushCode()
    out.push({ text: s, quote: line.startsWith('>') })
  }
  flushCode()
  return out
}

function loadLogo() {
  return new Promise((resolve) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => resolve(null)
    img.src = '/logo-seal.png'
  })
}

const fmtDate = (ts) =>
  new Date(ts || Date.now()).toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })

/**
 * 生成分享图。sessions: [{title, createdAt, messages}]，返回 PNG Blob。
 * 采用“先画到大画布再裁剪”的方式，避免两遍测量。
 */
export async function renderShareImage(sessions) {
  const logo = await loadLogo()
  const c1 = document.createElement('canvas')
  c1.width = W
  c1.height = 26000
  const ctx = c1.getContext('2d')
  ctx.fillStyle = '#fff'
  ctx.fillRect(0, 0, W, c1.height)

  let y = 0

  // ---- 头部品牌区 ----
  y = 56
  if (logo) {
    ctx.save()
    roundRect(ctx, W / 2 - 36, y, 72, 72, 36)
    ctx.clip()
    ctx.drawImage(logo, W / 2 - 36, y, 72, 72)
    ctx.restore()
    y += 92
  }
  ctx.textAlign = 'center'
  ctx.fillStyle = BRAND
  ctx.font = `700 26px ${SANS}`
  ctx.fillText('戒色知识库', W / 2, y)
  y += 26
  ctx.fillStyle = TEXT_FAINT
  ctx.font = `400 14px ${SANS}`
  ctx.fillText(`${fmtDate(Date.now())} · AI 戒色知识问答`, W / 2, y)
  y += 44

  const contentW = W - PAD * 2

  // ---- 会话内容 ----
  for (let si = 0; si < sessions.length; si++) {
    const s = sessions[si]

    // 会话标题胶囊
    ctx.font = `400 14px ${SANS}`
    const t = s.title || '对话'
    const tw = ctx.measureText(t).width
    const chipW = tw + 36
    ctx.fillStyle = '#F5F6FA'
    roundRect(ctx, W / 2 - chipW / 2, y, chipW, 30, 15)
    ctx.fill()
    ctx.fillStyle = TEXT_SUB
    ctx.fillText(t, W / 2, y + 20)
    y += 54

    const msgs = s.messages || []
    for (let i = 0; i < msgs.length; i++) {
      const m = msgs[i]
      if (m.role === 'user') {
        // 用户问题：品牌蓝右对齐气泡
        ctx.font = `400 16px ${SANS}`
        const qLines = wrapText(ctx, m.content || '', contentW - 72)
        const lineH = 26
        let bw = 0
        for (const ln of qLines) bw = Math.max(bw, ctx.measureText(ln).width)
        bw += 36
        const bh = qLines.length * lineH + 24
        const bx = W - PAD - bw
        ctx.fillStyle = BRAND
        roundRect(ctx, bx, y, bw, bh, 16)
        ctx.fill()
        // 右下角小直角，示意"我方发言"
        ctx.beginPath()
        ctx.moveTo(W - PAD, y + bh - 16)
        ctx.lineTo(W - PAD, y + bh)
        ctx.lineTo(W - PAD - 16, y + bh)
        ctx.closePath()
        ctx.fill()
        ctx.fillStyle = '#fff'
        ctx.textAlign = 'left'
        qLines.forEach((ln, li) => ctx.fillText(ln, bx + 18, y + 24 + li * lineH))
        y += bh + 26
      } else if (m.role === 'assistant' && m.content) {
        // AI 回答：左对齐正文
        let content = m.content
        if (content.length > MAX_ANSWER_CHARS) {
          content = `${content.slice(0, MAX_ANSWER_CHARS)}\n\n……（回答过长，分享图已截断）`
        }
        ctx.textAlign = 'left'
        for (const item of mdToLines(content)) {
          if (item.blank) { y += 10; continue }
          if (item.code) {
            ctx.font = `400 13px ${MONO}`
            const codeLines = []
            for (const cl of item.code) codeLines.push(...wrapText(ctx, cl, contentW - 36))
            const ch = codeLines.length * 22 + 24
            ctx.fillStyle = '#F5F6FA'
            roundRect(ctx, PAD, y, contentW, ch, 10)
            ctx.fill()
            ctx.fillStyle = TEXT_SUB
            codeLines.forEach((ln, li) => ctx.fillText(ln, PAD + 18, y + 22 + li * 22))
            y += ch + 12
            continue
          }
          ctx.font = `400 16px ${SANS}`
          const lines = wrapText(ctx, item.text, contentW)
          for (const ln of lines) {
            ctx.fillStyle = item.quote ? TEXT_FAINT : TEXT_MAIN
            ctx.fillText(ln, PAD, y + 16)
            y += 26
          }
          y += 4
        }
        // 引用角标数量提示
        if (m.citations?.length) {
          ctx.font = `400 13px ${SANS}`
          ctx.fillStyle = TEXT_FAINT
          ctx.fillText(`参考来源 ${m.citations.length} 条`, PAD, y + 14)
          y += 30
        }
        y += 14
      }
      if (y > 25000) break
    }

    // 会话间分隔
    if (si < sessions.length - 1 && y < 25000) {
      y += 10
      ctx.textAlign = 'center'
      ctx.fillStyle = '#D5D8E0'
      ctx.font = `400 16px ${SANS}`
      ctx.fillText('· · ·', W / 2, y)
      y += 40
    }
    if (y > 25000) break
  }

  // ---- 底部水印 ----
  y += 6
  ctx.textAlign = 'center'
  ctx.fillStyle = '#E4E7EC'
  ctx.fillRect(PAD, y, contentW, 1)
  y += 34
  ctx.fillStyle = TEXT_FAINT
  ctx.font = `400 13px ${SANS}`
  ctx.fillText('内容由 AI 生成，仅供参考 · 戒色知识库', W / 2, y)
  y += 44

  // ---- 裁剪到实际高度 ----
  const c2 = document.createElement('canvas')
  c2.width = W
  c2.height = Math.ceil(y)
  const ctx2 = c2.getContext('2d')
  ctx2.fillStyle = '#fff'
  ctx2.fillRect(0, 0, W, c2.height)
  ctx2.drawImage(c1, 0, 0)

  return new Promise((resolve, reject) => {
    c2.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('图片生成失败'))), 'image/png')
  })
}
