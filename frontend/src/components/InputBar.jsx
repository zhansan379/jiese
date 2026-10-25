import { useState } from 'react'
import { Input } from 'antd'
import {
  ArrowUpOutlined, BulbOutlined, LoadingOutlined, SearchOutlined,
} from '@ant-design/icons'

/** 底部吸底输入区：文本框 + 深度思考/智能搜索开关 + 圆形发送按钮（DeepSeek 样式） */
export default function InputBar({ sending, onSend }) {
  const [text, setText] = useState('')
  const [deep, setDeep] = useState(false)
  const [smart, setSmart] = useState(false)

  const canSend = text.trim().length > 0 && !sending

  const submit = () => {
    if (!canSend) return
    onSend(text.trim(), { deepThinking: deep, smartSearch: smart })
    setText('')
  }

  return (
    <div className="input-bar">
      <div className="input-card">
        <Input.TextArea
          value={text}
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          autoSize={{ minRows: 1, maxRows: 6 }}
          variant="borderless"
          onChange={(e) => setText(e.target.value)}
          onPressEnter={(e) => {
            if (!e.shiftKey) { e.preventDefault(); submit() }
          }}
        />
        <div className="input-actions">
          <div className="input-toggles">
            <button
              type="button"
              className={`toggle-pill${deep ? ' on' : ''}`}
              onClick={() => setDeep((v) => !v)}
            >
              <BulbOutlined /> 深度思考
            </button>
            <button
              type="button"
              className={`toggle-pill${smart ? ' on' : ''}`}
              onClick={() => setSmart((v) => !v)}
            >
              <SearchOutlined /> 智能搜索
            </button>
          </div>
          <button
            type="button"
            className={`send-circle${text.trim() ? ' ready' : ''}`}
            disabled={!canSend}
            onClick={submit}
            aria-label="发送"
          >
            {sending ? <LoadingOutlined /> : <ArrowUpOutlined />}
          </button>
        </div>
      </div>
      <div className="input-hint">
        回答基于《戒为良药》《戒者录》原文生成，请以原著为准
      </div>
    </div>
  )
}
