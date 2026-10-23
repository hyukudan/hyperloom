// Mockup shell: theme from URL hash (#dark), sidebar, mobile top bar, tiny SVG chart helpers. SAMPLE DATA ONLY.
(function(){
  if (location.hash.indexOf('dark')>=0) document.documentElement.dataset.theme='dark';
  if (location.hash.indexOf('narrow')>=0) document.documentElement.classList.add('nw');
  const page=document.body.dataset.page;
  const I={
    devices:'<rect x="3" y="4" width="14" height="10" rx="2"/><path d="M7 17h6"/>',
    topology:'<circle cx="5" cy="5" r="2.2"/><circle cx="15" cy="5" r="2.2"/><circle cx="10" cy="15" r="2.2"/><path d="M7 5h6M6.2 7l2.6 6M13.8 7l-2.6 6"/>',
    models:'<path d="M10 2l7 4v8l-7 4-7-4V6z"/><path d="M3 6l7 4 7-4M10 10v8"/>',
    engines:'<path d="M5 4l10 6-10 6z"/>',
    stats:'<path d="M3 16V9M8 16V4M13 16v-6M18 16v-9"/>',
    logs:'<path d="M4 4h12M4 8h12M4 12h8M4 16h10"/>',
    settings:'<circle cx="10" cy="10" r="3"/><path d="M10 2v3M10 15v3M2 10h3M15 10h3M4.3 4.3l2 2M13.7 13.7l2 2M4.3 15.7l2-2M13.7 6.3l2-2"/>'
  };
  const items=[['devices','Devices'],['topology','Topology & links'],['models','Models & placement'],['engines','Engines'],['stats','Live stats'],['logs','Logs'],['settings','Settings & pairing']];
  const badges={devices:'<span class="pill p-info badge">1 new</span>',logs:'<span class="pill p-warn badge">2</span>'};
  const nav=items.map(([k,t])=>`<a class="${k===page?'on':''}" href="${k}.html${location.hash}"><svg class="ic" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${I[k]}</svg>${t}${badges[k]||''}</a>`).join('');
  const side=document.querySelector('.side');
  if(side) side.innerHTML=`<div class="brand"><div class="logo">hl</div>Hyperloom</div><nav class="nav">${nav}</nav>
    <div class="foot"><div class="row between"><span>Control</span><span class="pill p-ok">workstation</span></div><div style="margin-top:6px">hyperloom 0.1.0 · lab mode</div></div>`;
  const m=document.querySelector('.mtop');
  if(m) m.innerHTML=`<div class="logo">hl</div><b>${(items.find(i=>i[0]===page)||['',''])[1]}</b><span style="flex:1"></span><span class="pill p-ok">Running</span><svg width="22" height="22" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 6h14M3 10h14M3 14h14"/></svg>`;
})();
function cssv(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim()}
// line chart: series=[{data:[...],color:'--s1',fill:true,dash:false}], opts {h,max,min,ylabels:[...],xlabels:[...]}
function lineChart(el,series,o){
  const W=el.clientWidth||600,H=o.h||160,pl=o.pl??34,pb=18,pt=6,pr=6;
  const max=o.max,min=o.min||0,n=series[0].data.length;
  const x=i=>pl+(W-pl-pr)*i/(n-1), y=v=>pt+(H-pt-pb)*(1-(v-min)/(max-min));
  let s=`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`;
  (o.yt||[]).forEach(v=>{s+=`<line x1="${pl}" x2="${W-pr}" y1="${y(v)}" y2="${y(v)}" stroke="${cssv('--grid')}"/><text x="${pl-6}" y="${y(v)+4}" text-anchor="end">${v}${o.yu||''}</text>`});
  (o.xt||[]).forEach(([i,t])=>{const a=i===0?'start':(i===n-1?'end':'middle');s+=`<text x="${x(i)}" y="${H-4}" text-anchor="${a}">${t}</text>`});
  series.forEach(se=>{
    const c=cssv(se.color);const pts=se.data.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
    if(se.fill) s+=`<polygon points="${x(0)},${y(min)} ${pts} ${x(n-1)},${y(min)}" fill="${c}" opacity=".12"/>`;
    s+=`<polyline points="${pts}" fill="none" stroke="${c}" stroke-width="${se.w||2}" ${se.dash?'stroke-dasharray="4 3"':''} stroke-linejoin="round"/>`;
  });
  el.innerHTML=s+'</svg>';
}
function spark(el,data,color){lineChart(el,[{data,color,fill:true,w:1.6}],{h:38,max:Math.max(...data)*1.15,min:0,pl:0})}
// seeded noise
function series(n,base,amp,seed,trend=0){let s=seed,o=[];for(let i=0;i<n;i++){s=(s*9301+49297)%233280;o.push(+(base+trend*i+amp*(s/233280-.5)).toFixed(2))}return o}
