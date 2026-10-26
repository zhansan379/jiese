import { useEffect, useRef } from 'react'
import * as echarts from 'echarts/core'
import { GraphChart as EChartsGraph } from 'echarts/charts'
import { LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([EChartsGraph, TooltipComponent, LegendComponent, CanvasRenderer])

/** 节点类型 → 分类序号（与后端 terms.type 一致） */
const CATEGORIES = [
  { name: '症状', itemStyle: { color: '#ff4d4f' } },
  { name: '方法', itemStyle: { color: '#52c41a' } },
  { name: '因素', itemStyle: { color: '#fa8c16' } },
  { name: '概念', itemStyle: { color: '#4D6BFE' } },
]
const CAT_INDEX = Object.fromEntries(CATEGORIES.map((c, i) => [c.name, i]))
const REL_COLOR = { 对治: '#52c41a', 导致: '#fa8c16' }

/** 节点尺寸：按度数开方放大，避免超级节点吞掉全图 */
const nodeSize = (degree) => 9 + Math.min(21, Math.sqrt(degree) * 6)

/**
 * 知识图谱力导向图（ECharts canvas）：
 * 拖拽/滚轮缩放，点击节点回调聚焦该概念，点击连线回调查看原文证据。
 */
export default function GraphChart({ nodes, links, selected, height = 460, onSelectNode, onSelectEdge }) {
  const boxRef = useRef(null)
  const chartRef = useRef(null)

  useEffect(() => {
    if (!boxRef.current) return undefined
    const chart = echarts.init(boxRef.current, null, { renderer: 'canvas' })
    chartRef.current = chart
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(boxRef.current)
    return () => {
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const linksData = links.map((l) => ({
      ...l,
      lineStyle: {
        color: REL_COLOR[l.rel] || '#9aa0b4',
        width: 1 + Math.min(2.5, l.support / 12),
        opacity: 0.6,
        curveness: 0.12,
      },
    }))
    chart.setOption({
      animationDuration: 500,
      tooltip: {
        confine: true,
        backgroundColor: 'rgba(255,255,255,.96)',
        borderColor: '#e6e9f0',
        textStyle: { color: '#2c2f42', fontSize: 12 },
        formatter: (p) => (p.dataType === 'edge'
          ? `${p.data.source} <b>${p.data.rel}</b> ${p.data.target}<br/>原文证据 ${p.data.support} 处`
          : `<b>${p.data.name}</b><br/>类型：${p.data.type}｜关联 ${p.data.degree} 条关系`),
      },
      legend: [{
        bottom: 0,
        itemWidth: 9,
        itemHeight: 9,
        itemGap: 12,
        textStyle: { fontSize: 11, color: '#6b7280' },
        data: CATEGORIES.map((c) => c.name),
      }],
      series: [{
        type: 'graph',
        layout: 'force',
        roam: true,             // 桌面拖拽/滚轮缩放；移动端单指平移、双指缩放
        draggable: true,
        top: 8,
        bottom: 30,
        symbol: 'circle',
        label: { show: true, position: 'right', fontSize: 10, color: '#3a3f58' },
        labelLayout: { hideOverlap: true },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: [0, 5],
        categories: CATEGORIES,
        data: nodes.map((n) => ({
          name: n.name,
          value: n.degree,
          type: n.type,
          degree: n.degree,
          support: n.support,
          category: CAT_INDEX[n.type] ?? 3,
          symbolSize: nodeSize(n.degree),
          itemStyle: selected === n.name
            ? { borderColor: '#1f2140', borderWidth: 2, shadowBlur: 8, shadowColor: 'rgba(31,33,64,.35)' }
            : undefined,
          label: selected === n.name ? { fontWeight: 600 } : undefined,
        })),
        links: linksData,
        force: {
          repulsion: 240,
          edgeLength: [70, 150],
          gravity: 0.08,
          friction: 0.2,
          layoutAnimation: true,
        },
        emphasis: {
          focus: 'adjacency',
          lineStyle: { width: 3, opacity: 1 },
          label: { show: true, fontWeight: 600 },
        },
        blur: { itemStyle: { opacity: 0.2 }, label: { opacity: 0.25 } },
      }],
    }, { notMerge: true })
  }, [nodes, links, selected])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return undefined
    const onClick = (p) => {
      if (p.dataType === 'edge') onSelectEdge?.(p.data)
      else onSelectNode?.(p.name)
    }
    chart.on('click', onClick)
    return () => chart.off('click', onClick)
  }, [onSelectNode, onSelectEdge])

  return <div ref={boxRef} style={{ width: '100%', height }} role="img" aria-label="知识图谱关系图" />
}
