import { useEffect, useState } from 'react';
import { App, Button, Dropdown, Flex, Modal, Tag, Typography } from 'antd';
import { ChevronLeft, ChevronRight, Download, MoreHorizontal, SlidersHorizontal, Trash2, WandSparkles, X } from 'lucide-react';
import { icon, useStudio } from './App.jsx';
import { request, dateLabel } from './lib.js';
import './gallery.css';

const { Text, Paragraph } = Typography;
export default function ArtworkModal({ task: selected, onClose, onDeleted, initialIndex = 0 }) {
  const { navigate, setGenerationDraft, refresh } = useStudio();
  const { message, modal } = App.useApp();
  const [imageIndex, setImageIndex] = useState(initialIndex);
  useEffect(() => { setImageIndex(initialIndex); }, [selected?.id, initialIndex]);
  function recreate(task) { setGenerationDraft(task); onClose(); navigate('generate'); }
  function remove(task) {
    modal.confirm({ title: '删除整个作品？', content: `将永久删除此任务及其 ${task.files.length} 张生成图片。`, okText: '删除作品', cancelText: '取消', okButtonProps: { danger: true }, onOk: async () => {
      try { await request('/api/tasks/' + task.id, { method: 'DELETE' }); onClose(); onDeleted?.(task.id); await refresh(); message.success('作品及关联图片已删除'); }
      catch (e) { message.error(e.message); throw e; }
    } });
  }
  const taskMenu = task => ({ items: [{ key: 'recreate', label: '相同参数创建', icon: icon(WandSparkles, 15) }, ...(task.status === 'succeeded' ? [{ type: 'divider' }, { key: 'delete', label: '删除整个作品', danger: true, icon: icon(Trash2, 15) }] : [])], onClick: ({ key }) => key === 'delete' ? remove(task) : recreate(task) });
  const currentFile = selected?.files[Math.min(imageIndex, selected.files.length - 1)];
  return (
    <Modal aria-label="作品详情" open={!!selected} onCancel={() => onClose()} width="100vw" className="ig-detail-modal" rootClassName="ig-artwork-overlay" closable={false} style={{ top: 0, paddingBottom: 0, maxWidth: '100vw', margin: 0 }} styles={{ mask: { backdropFilter: 'blur(24px)', WebkitBackdropFilter: 'blur(24px)', background: 'rgba(6, 8, 14, .5)' }, container: { height: '100dvh', borderRadius: 0, padding: 0, background: 'transparent', boxShadow: 'none', overflow: 'hidden' }, body: { height: '100%' } }} footer={null} destroyOnHidden>
      {selected && currentFile && <div id="ig-artwork-detail" className="ig-detail-layout">
        <div className="ig-detail-viewer">
          <div className="ig-detail-ambience" aria-hidden="true" style={{ backgroundImage: `url(${JSON.stringify(currentFile.url)})` }} />
          <div className="ig-detail-toolbar"><Flex gap={10} align="center"><Button type="text" shape="circle" aria-label="关闭" icon={icon(X, 22)} onClick={() => onClose()} /><span className="ig-detail-heading"><strong>作品详情</strong><small>{selected.model_id}</small></span><Button id="ig-recreate" type="primary" shape="round" icon={icon(WandSparkles, 16)} onClick={() => recreate(selected)}>相同参数创建</Button></Flex><Flex gap={8}><Button shape="circle" href={currentFile.url} download={currentFile.storage_key.split('/').pop()} icon={icon(Download, 18)} aria-label="下载当前原图" /><Dropdown menu={taskMenu(selected)} trigger={['click']}><Button type="text" shape="circle" icon={icon(MoreHorizontal, 21)} aria-label="作品操作" /></Dropdown></Flex></div>
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
  );
}
