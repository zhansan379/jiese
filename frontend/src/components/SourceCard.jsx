import { Typography } from 'antd'

const { Paragraph } = Typography

/** 引用条目（极简列表式）：编号 + 来源 + 单行摘要，整行点击跳转原文 */
export default function SourceCard({ index, citation, onOpenSource }) {
  const handleRowClick = (e) => {
    if (e.target.closest('.ant-typography-expand')) return  // 展开/收起不触发跳转
    onOpenSource(citation)
  }

  return (
    <div id={`cite-${index}`} className="source-item" onClick={handleRowClick}>
      <span className="source-num">{index}</span>
      <div className="source-main">
        <div className="source-line">
          <span className="source-book">《{citation.book_title}》</span>
          <span className="source-chapter">{citation.chapter_title}</span>
          {citation.author && citation.author !== citation.book_title && (
            <span className="source-author">{citation.author}</span>
          )}
        </div>
        <Paragraph
          className="source-snippet"
          ellipsis={{ rows: 1, expandable: true, symbol: '展开' }}
        >
          {citation.text}
        </Paragraph>
      </div>
    </div>
  )
}
