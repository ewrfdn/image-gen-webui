const { chromium } = require('playwright');
const fs = require('fs');
const assert = (ok, label) => { if (!ok) throw Error(label); };
(async () => {
 const browser = await chromium.launch({ headless: true, channel: 'msedge' });
 const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
 const errors = []; page.on('pageerror', e => errors.push(String(e)));
 const tasks = Array.from({length: 30}, (_, i) => {
  const height = [720, 400, 560][i % 3];
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="400" height="${height}"><rect width="400" height="${height}" fill="hsl(${i*13},25%,30%)"/><text x="30" y="80" fill="white" font-size="40">Task ${i}</text></svg>`;
  return { id: `fixture-${String(i).padStart(2,'0')}`, prompt: `Task ${i}`, model_id: 'test-image', model_revision: 'r1', created_at: new Date(Date.UTC(2026,9,15,12,0,0)-i*60000).toISOString(), parameters: {size: `400x${height}`}, files: [{url: 'data:image/svg+xml;base64,'+Buffer.from(svg).toString('base64'), mime: 'image/png', storage_key: `${i}.png`}], loras: [], references: [], status: 'succeeded' };
 });
 await page.route('**/api/history?*', route => { const url = new URL(route.request().url()); const offset = +url.searchParams.get('offset'); const limit = +url.searchParams.get('limit'); return route.fulfill({json: {total:tasks.length, tasks:tasks.slice(offset,offset+limit).reverse()}}); });
 await page.goto('http://127.0.0.1:18082');
 await page.locator('#frame-username').fill('admin'); await page.locator('#frame-password').fill('ui-verification-token-0000000000'); await page.locator('#frame-login-form button[type=submit]').click();
 await page.locator('[data-page=gallery]').click();
 await page.waitForFunction(() => document.querySelectorAll('.ig-artwork-card').length === 24 && document.querySelector('#ig-personal-gallery').getAttribute('aria-busy') === 'false');
 for (const width of [1440,390,320]) {
  await page.setViewportSize({width,height:1000});
  await page.waitForTimeout(250);
  const boxes = await page.locator('.ig-artwork-card').evaluateAll(cards => cards.map(c => ({id:c.dataset.artwork, x:c.getBoundingClientRect().x, y:c.getBoundingClientRect().y})));
  assert(boxes.every((b,i)=>b.id===`fixture-${String(i).padStart(2,'0')}`), 'task creation time descending');
  assert(boxes.every((b,i)=>i===0 || b.y >= boxes[i-1].y-1), 'visual rows follow descending order');
  if (width === 1440) assert(boxes.slice(0,3).every(b=>Math.abs(b.y-boxes[0].y)<2), 'three latest tasks occupy first row');
  const header = await page.locator('#ig-workspace-header').boundingBox();
  await page.locator('#ig-workspace-scroll').evaluate(el => el.scrollTop = el.scrollHeight);
  assert(await page.locator('#ig-workspace-scroll').evaluate(el => el.scrollTop > 0), 'content has internal scrollbar');
  const after = await page.locator('#ig-workspace-header').boundingBox();
  assert(header.y===after.y, 'header stays fixed');
  assert(await page.evaluate(()=>document.scrollingElement.scrollTop===0 && document.documentElement.scrollWidth<=innerWidth+2 && document.documentElement.scrollHeight<=innerHeight+2), 'no document scrolling or overflow');
  await page.locator('#ig-workspace-scroll').evaluate(el => el.scrollTop = 0);
 }
 await page.locator('#ig-gallery-more').click();
 await page.waitForFunction(()=>document.querySelectorAll('.ig-artwork-card').length===30);
 assert(await page.locator('.ig-artwork-card').last().getAttribute('data-artwork') === 'fixture-29', 'pagination keeps task order');
 tasks.unshift({...tasks[0], id:'new-task', created_at:'2026-10-15T13:00:00Z'});
 await page.locator('[data-page=generate]').click(); await page.locator('[data-page=gallery]').click();
 await page.waitForFunction(()=>document.querySelector('.ig-artwork-card')?.dataset.artwork==='new-task');
 assert(errors.length===0, errors.join(';'));
 fs.writeFileSync('verification-output/react-ui/gallery-layout-report.json', JSON.stringify({passed:true,checks:['descending visual task order','pagination','refresh on return','fixed header','internal scrollbar','1440/390/320 widths']},null,2));
 await browser.close(); console.log('PASS: Gallery order and workspace scrolling');
})().catch(e=>{console.error(e);process.exit(1)});
