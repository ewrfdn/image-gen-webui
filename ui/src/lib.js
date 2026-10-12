export const taskNames = { pending: '等待调度', processing: '准备生成', loading: '加载模型', submitted: '正在生成', running: '正在生成', persisting: '保存结果', succeeded: '已完成', failed: '失败', unknown: '结果未知' };
export const statusNames = { available: '可生成', need_load: '需要加载', busy: '等待节点', unavailable: '暂不可用' };
export const roles = { user: '用户', admin: '管理员', super_admin: '超级管理员' };
export const modelKey = m => `${m.model_id}|${m.revision}`;
export const dateLabel = value => {
  if (!value) return '—';
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : value + 'Z';
  return new Date(normalized).toLocaleString('zh-CN');
};
export async function request(path, options = {}) {
  const headers = { ...options.headers };
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('frame:unauthorized'));
    let error = `请求失败：${response.status}`;
    try { error = (await response.json()).error?.message || error; } catch { /* non-JSON response */ }
    throw new Error(error);
  }
  return response;
}
export async function api(path, options) { return (await request(path, options)).json(); }
export const send = (path, method, body) => api(path, { method, body: JSON.stringify(body) });
export function sizeChoices(g) {
  if (!g) return [];
  const ratios = [['1:1', 1, 1], ['3:4', 3, 4], ['4:3', 4, 3], ['9:16', 9, 16], ['16:9', 16, 9]];
  const [dw, dh] = g.default_size.split('x').map(Number);
  let defaultRatio = ratios.find(r => Math.abs(dw / dh - r[1] / r[2]) < .025);
  if (!defaultRatio) { defaultRatio = ['默认', dw, dh]; ratios.push(defaultRatio); }
  const valid = (w, h) => w >= g.width_min && w <= g.width_max && h >= g.height_min && h <= g.height_max && w * h <= g.max_pixels && w % g.dimension_multiple === 0 && h % g.dimension_multiple === 0;
  return ratios.map(([ratio, rw, rh]) => {
    const sizes = new Map();
    for (const edge of [512, 768, 1024, 1280, 1536, 2048, 2560, Math.max(dw, dh)]) {
      const w = Math.round(edge * rw / Math.max(rw, rh) / g.dimension_multiple) * g.dimension_multiple;
      const h = Math.round(edge * rh / Math.max(rw, rh) / g.dimension_multiple) * g.dimension_multiple;
      if (valid(w, h)) sizes.set(`${w}x${h}`, { value: `${w}x${h}`, label: `${w} × ${h}` });
    }
    if (ratio === defaultRatio[0] && valid(dw, dh)) sizes.set(g.default_size, { value: g.default_size, label: `${dw} × ${dh} · 模型默认` });
    return { ratio, rw, rh, isDefault: ratio === defaultRatio[0], sizes: [...sizes.values()].sort((a, b) => a.value.split('x').reduce((x, y) => x * y, 1) - b.value.split('x').reduce((x, y) => x * y, 1)) };
  }).filter(r => r.sizes.length);
}
