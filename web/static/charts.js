// 자산곡선 + 도넛을 캔버스로 직접 그린다.
// 외부 CDN에 의존하지 않는 이유: 이 대시보드는 Tailscale 뒤 자체 호스팅이고,
// 차트가 CDN 사정에 따라 깨지면 안 된다. 필요한 게 선/원호뿐이라 라이브러리가 과하다.

const CSS = getComputedStyle(document.documentElement);
const C = {
  fg: CSS.getPropertyValue('--fg').trim() || '#e6edf7',
  dim: CSS.getPropertyValue('--dim').trim() || '#8b98ad',
  accent: CSS.getPropertyValue('--accent').trim() || '#4aa8ff',
  line: 'rgba(255,255,255,.07)',
};
const PALETTE = ['#4aa8ff', '#5ddc9a', '#ffb454', '#ff7a90', '#b48ead', '#8fd3f4', '#f6c177'];

const nf = new Intl.NumberFormat('ko-KR');
export const fmtKRW = v => nf.format(Math.round(v)) + '원';

function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const { width, height } = canvas.getBoundingClientRect();
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return { ctx, w: width, h: height };
}

function niceTicks(min, max, count = 4) {
  if (min === max) return [min];
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || mag * 10;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max; v += step) out.push(v);
  return out;
}

/** 자산곡선. data: [{t: ISO, v: number}] */
export function lineChart(canvas, data) {
  const { ctx, w, h } = fitCanvas(canvas);
  const padL = 62, padR = 10, padT = 10, padB = 24;
  const plotW = w - padL - padR, plotH = h - padT - padB;

  const values = data.map(d => d.v);
  let lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || Math.max(hi * 0.001, 1);
  lo -= span * 0.15; hi += span * 0.15;

  const X = i => padL + (data.length === 1 ? plotW / 2 : (i / (data.length - 1)) * plotW);
  const Y = v => padT + plotH - ((v - lo) / (hi - lo)) * plotH;

  // y축 그리드 + 라벨
  ctx.font = '11px system-ui, sans-serif';
  ctx.textBaseline = 'middle';
  for (const t of niceTicks(lo, hi)) {
    const y = Y(t);
    ctx.strokeStyle = C.line;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillStyle = C.dim; ctx.textAlign = 'right';
    ctx.fillText(nf.format(Math.round(t)), padL - 8, y);
  }

  // x축 라벨 (양 끝 + 중앙)
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  const label = i => new Date(data[i].t).toLocaleString('ko-KR',
    { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  const idxs = data.length > 2 ? [0, Math.floor((data.length - 1) / 2), data.length - 1] : [0, data.length - 1];
  for (const i of [...new Set(idxs)]) {
    ctx.fillStyle = C.dim;
    ctx.fillText(label(i), Math.min(Math.max(X(i), 30), w - 30), h - padB + 7);
  }

  // 면 채우기 + 선
  ctx.beginPath();
  data.forEach((d, i) => (i ? ctx.lineTo(X(i), Y(d.v)) : ctx.moveTo(X(i), Y(d.v))));
  const area = ctx.createLinearGradient(0, padT, 0, padT + plotH);
  area.addColorStop(0, 'rgba(74,168,255,.22)');
  area.addColorStop(1, 'rgba(74,168,255,0)');
  ctx.lineTo(X(data.length - 1), padT + plotH);
  ctx.lineTo(X(0), padT + plotH);
  ctx.closePath(); ctx.fillStyle = area; ctx.fill();

  ctx.beginPath();
  data.forEach((d, i) => (i ? ctx.lineTo(X(i), Y(d.v)) : ctx.moveTo(X(i), Y(d.v))));
  ctx.strokeStyle = C.accent; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();

  // 마지막 점
  const last = data.length - 1;
  ctx.beginPath(); ctx.arc(X(last), Y(data[last].v), 3.5, 0, Math.PI * 2);
  ctx.fillStyle = C.accent; ctx.fill();

  // 호버 툴팁
  const tip = canvas.parentElement.querySelector('.tip') || (() => {
    const el = document.createElement('div');
    el.className = 'tip'; el.hidden = true;
    canvas.parentElement.appendChild(el);
    return el;
  })();

  canvas.onmousemove = e => {
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX - r.left;
    let best = 0, bd = Infinity;
    data.forEach((_, i) => { const d = Math.abs(X(i) - mx); if (d < bd) { bd = d; best = i; } });
    tip.hidden = false;
    tip.textContent = `${label(best)} · ${fmtKRW(data[best].v)}`;
    tip.style.left = Math.min(Math.max(X(best), 60), w - 60) + 'px';
    tip.style.top = Math.max(Y(data[best].v) - 34, 0) + 'px';
  };
  canvas.onmouseleave = () => { tip.hidden = true; };
}

/** 도넛. items: [{label, value}] */
export function donutChart(canvas, items) {
  const { ctx, w, h } = fitCanvas(canvas);
  const total = items.reduce((s, i) => s + i.value, 0);
  if (total <= 0) return;

  const cx = w / 2, cy = h / 2 - 6;
  const R = Math.min(w, h * 0.92) / 2 - 6, r = R * 0.62;
  let a = -Math.PI / 2;

  items.forEach((item, i) => {
    const sweep = (item.value / total) * Math.PI * 2;
    ctx.beginPath();
    ctx.arc(cx, cy, R, a, a + sweep);
    ctx.arc(cx, cy, r, a + sweep, a, true);
    ctx.closePath();
    ctx.fillStyle = PALETTE[i % PALETTE.length];
    ctx.fill();
    a += sweep;
  });

  ctx.fillStyle = C.fg;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.font = '600 15px system-ui, sans-serif';
  ctx.fillText(fmtKRW(total), cx, cy);
  ctx.font = '11px system-ui, sans-serif';
  ctx.fillStyle = C.dim;
  ctx.fillText('코인 평가액', cx, cy + 18);
}

export { PALETTE };
