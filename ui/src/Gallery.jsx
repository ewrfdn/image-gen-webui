import { useEffect, useState } from 'react';
import { Alert, App, Button, Card, Dropdown, Empty, Flex, Grid, Modal, Spin, Tag, Typography } from 'antd';
import { ArrowLeft, ChevronLeft, ChevronRight, Download, Layers, MoreHorizontal, RefreshCw, SlidersHorizontal, Trash2, WandSparkles, X } from 'lucide-react';
import { icon, PageHeading, useStudio } from './App.jsx';
import { api, request, dateLabel } from './lib.js';
import './gallery.css';

const { Text, Paragraph } = Typography;
const PAGE_SIZE = 24;
export default function Gallery() {
  const { navigate, setGenerationDraft, refresh } = useStudio();
  const { message, modal } = App.useApp();
  const [imageIndex, setImageIndex] = useState(0);
  const screens = Grid.useBreakpoint();
  const [records, setRecords] = useState([]), [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0), [version, setVersion] = useState(0);
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [selected, setSelected] = useState(null);
  const columns = screens.xxl ? 4 : screens.xl ? 3 : screens.sm ? 2 : 1;
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError('');
    api(`/api/history?limit=${PAGE_SIZE}&offset=${offset}`, { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      setTotal(result.total);
      setRecords(previous => offset === 0 ? result.tasks : [...new Map([...previous, ...result.tasks].map(t => [t.id, t])).values()]);
    }).catch(e => { if (!controller.signal.aborted) setError(e.message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [offset, version]);
  function reload() { setOffset(0); setVersion(v => v + 1); }
  function openTask(task) { setSelected(task); setImageIndex(0); }
  function recreate(task) { setGenerationDraft(task); setSelected(null); navigate('generate'); }
  function remove(task) {
    modal.confirm({ title: '删除整个作品？', content: `将永久删除此任务及其 ${task.files.length} 张生成图片。`, okText: '删除作品', cancelText: '取消', okButtonProps: { danger: true }, onOk: async () => {
      try { await request('/api/tasks/' + task.id, { method: 'DELETE' }); setSelected(null); reload(); await refresh(); message.success('作品及关联图片已删除'); }
      catch (e) { message.error(e.message); throw e; }
    } });
  }
  const taskMenu = task => ({ items: [{ key: 'recreate', label: '相同参数创建', icon: icon(WandSparkles, 15) }, { type: 'divider' }, { key: 'delete', label: '删除整个作品', danger: true, icon: icon(Trash2, 15) }], onClick: ({ key }) => key === 'delete' ? remove(task) : recreate(task) });
  const currentFile = selected?.files[imageIndex];
  return <>
    <PageHeading eyebrow="YOUR IMAGE LIBRARY" title="我的作品" description="按时间收藏你的每一次创作，点击图片查看提示词与生成参数。" extra={<Flex gap={8} wrap><Button icon={icon(ArrowLeft, 14)} onClick={() => navigate('generate')}>返回创作台</Button><Button aria-label="刷新作品" icon={icon(RefreshCw, 14)} onClick={reload} /></Flex>} />
    <Flex justify="space-between" style={{ marginBottom: 20 }}><Text type="secondary" style={{ fontSize: 12 }}>已生成的历史图片</Text><Tag id="ig-gallery-total" variant="filled">{total} 幅作品</Tag></Flex>
    {error && <Alert type="error" showIcon title={error} style={{ marginBottom: 20 }} />}
    <Flex id="ig-personal-gallery" role="list" aria-label="历史作品瀑布流" aria-busy={loading} style={{ display: 'block', columnCount: columns, columnGap: 18 }}>
      {records.map(task => <Card key={task.id} className="ig-artwork-card" data-artwork={task.id} role="listitem" styles={{ body: { padding: 0 } }} style={{ breakInside: 'avoid', marginBottom: 18, overflow: 'hidden', background: '#191b22', borderColor: '#ffffff0d' }}>
        <div className="ig-gallery-cover">
          <Button type="text" aria-label={`查看作品：${task.prompt.slice(0, 80)}`} onClick={() => openTask(task)} className="ig-gallery-open"><img src={task.files[0].url} alt={task.prompt.slice(0, 100)} loading="lazy" /></Button>
          {task.files.length > 1 && <span className="ig-image-count" aria-label={`${task.files.length} 张图片`}>{icon(Layers, 13)} {task.files.length}</span>}
          <Dropdown menu={taskMenu(task)} trigger={['click']} placement="bottomRight"><Button className="ig-task-menu" shape="circle" icon={icon(MoreHorizontal, 18)} aria-label="作品操作" /></Dropdown>
        </div>
        <div className="ig-gallery-caption"><Paragraph ellipsis={{ rows: 2 }} style={{ margin: '0 0 12px', fontSize: 13, lineHeight: 1.7 }}>{task.prompt}</Paragraph>
          <Flex justify="space-between" align="center" gap={8}><Flex vertical gap={4} style={{ minWidth: 0 }}><Text type="secondary" ellipsis style={{ fontSize: 11 }}>{task.model_id} · {task.parameters.size}</Text><Text type="secondary" style={{ fontSize: 10 }}>{dateLabel(task.created_at)}</Text></Flex><Button type="text" shape="circle" href={task.files[0].url} download={task.files[0].storage_key.split('/').pop()} icon={icon(Download, 16)} aria-label="下载作品" /></Flex>
        </div>
      </Card>)}
    </Flex>
    {!records.length && !loading && !error && <Empty description="还没有历史图片，去创作第一幅作品吧。"><Button type="primary" onClick={() => navigate('generate')}>开始创作</Button></Empty>}
    <Flex align="center" justify="center" gap={12} style={{ padding: 24 }}>{loading ? <Spin /> : !error && offset + PAGE_SIZE < total ? <Button id="ig-gallery-more" onClick={() => setOffset(o => o + PAGE_SIZE)}>加载更多作品</Button> : records.length > 0 && <Text type="secondary" style={{ fontSize: 11 }}>已展示全部 {total} 幅作品</Text>}{error && <Button onClick={() => setVersion(v => v + 1)}>重新加载</Button>}</Flex>
    <Modal aria-label="作品详情" open={!!selected} onCancel={() => setSelected(null)} width="100vw" className="ig-detail-modal" rootClassName="ig-artwork-overlay" closable={false} style={{ top: 0, paddingBottom: 0, maxWidth: '100vw', margin: 0 }} styles={{ mask: { backdropFilter: 'blur(24px)', WebkitBackdropFilter: 'blur(24px)', background: 'rgba(6, 8, 14, .5)' }, container: { height: '100dvh', borderRadius: 0, padding: 0, background: 'transparent', boxShadow: 'none', overflow: 'hidden' }, body: { height: '100%' } }} footer={null} destroyOnHidden>
      {selected && currentFile && <div id="ig-artwork-detail" className="ig-detail-layout">
        <div className="ig-detail-viewer">
          <div className="ig-detail-ambience" aria-hidden="true" style={{ backgroundImage: `url(${JSON.stringify(currentFile.url)})` }} />
          <div className="ig-detail-toolbar"><Flex gap={10} align="center"><Button type="text" shape="circle" aria-label="关闭" icon={icon(X, 22)} onClick={() => setSelected(null)} /><span className="ig-detail-heading"><strong>作品详情</strong><small>{selected.model_id}</small></span><Button id="ig-recreate" type="primary" shape="round" icon={icon(WandSparkles, 16)} onClick={() => recreate(selected)}>相同参数创建</Button></Flex><Flex gap={8}><Button shape="circle" href={currentFile.url} download={currentFile.storage_key.split('/').pop()} icon={icon(Download, 18)} aria-label="下载当前原图" /><Dropdown menu={taskMenu(selected)} trigger={['click']}><Button type="text" shape="circle" icon={icon(MoreHorizontal, 21)} aria-label="作品操作" /></Dropdown></Flex></div>
          <div className="ig-detail-stage">
            {selected.files.length > 1 && <Button className="ig-image-prev" shape="circle" aria-label="上一张图片" disabled={imageIndex === 0} icon={icon(ChevronLeft, 22)} onClick={() => setImageIndex(i => i - 1)} />}
            <img className="ig-detail-image" src={currentFile.url} alt={`生成图片 ${imageIndex + 1}`} />
            {selected.files.length > 1 && <Button className="ig-image-next" shape="circle" aria-label="下一张图片" disabled={imageIndex === selected.files.length - 1} icon={icon(ChevronRight, 22)} onClick={() => setImageIndex(i => i + 1)} />}
          </div>
          <div className="ig-image-pagination"><span>{String(imageIndex + 1).padStart(2, '0')} <span className="ig-page-total">/ {String(selected.files.length).padStart(2, '0')}</span></span>{selected.files.length > 1 && <div className="ig-page-dots">{selected.files.map((f, i) => <button key={f.id} aria-label={`查看第 ${i + 1} 张图片`} aria-current={i === imageIndex ? 'true' : undefined} className={i === imageIndex ? 'active' : ''} onClick={() => setImageIndex(i)} />)}</div>}<a href={currentFile.url} download={currentFile.storage_key.split('/').pop()}>下载原图</a></div>
        </div>
        <aside className="ig-detail-sidebar">
          <div className="ig-data-card"><div className="ig-data-heading">{icon(SlidersHorizontal, 20)}<span>生成参数</span><Tag bordered={false}>IMAGE</Tag></div>
            <section className="ig-data-section"><div className="ig-section-title">使用资源</div><div className="ig-resource-row"><div><strong>{selected.model_id}</strong><small>{selected.model_revision}</small></div><span className="ig-resource-kind">模型</span></div>{selected.loras?.length ? selected.loras.map(l => <div className="ig-resource-row" key={l.lora_id}><div><strong>{l.display_name || l.lora_id}</strong><small>强度 {l.scale}</small></div><span className="ig-resource-kind">LoRA</span></div>) : <Text type="secondary" style={{ fontSize: 12 }}>未使用 LoRA</Text>}</section>
            <section className="ig-data-section"><div className="ig-section-title">正向提示词</div><Paragraph copyable ellipsis={{ rows: 6, expandable: true, symbol: '展开' }} className="ig-detail-prompt">{selected.prompt}</Paragraph></section>
            <section className="ig-data-section"><div className="ig-section-title">负向提示词</div><Paragraph copyable={!!selected.negative_prompt} ellipsis={{ rows: 4, expandable: true, symbol: '展开' }} className="ig-detail-prompt">{selected.negative_prompt || '未设置'}</Paragraph></section>
            <section className="ig-data-section"><div className="ig-section-title">其他参数</div><div className="ig-meta-chips">{[['分辨率', selected.parameters.size], ['Steps', selected.parameters.num_inference_steps], ['Guidance', selected.parameters.guidance_scale], ['Seed', selected.parameters.seed ?? '随机'], ['张数', selected.files.length]].map(([label, value]) => <span key={label}><small>{label}</small>{value ?? '—'}</span>)}</div></section>
            <div className="ig-created-at"><span>生成时间</span><time>{dateLabel(selected.created_at)}</time></div>
          </div>
        </aside>
      </div>}
    </Modal>
  </>;
}
