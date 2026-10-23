'use strict';
/* POLY/EDGE terminal. Every panel is drawn from the live paper engine snapshot; nothing is simulated.
   Panels show an explicit collecting state until the engine has data for them. */
(() => {
 const LANES=['BTC','ETH','BTC15','ETH15'];
 const NAME={BTC:'BTC 5m',ETH:'ETH 5m',BTC15:'BTC 15m',ETH15:'ETH 15m'};
 const COL={BTC:'#ffc45a',ETH:'#65d5ff',BTC15:'#bdabff',ETH15:'#67f3a2'};
 const C={bg:'#070c0e',panel:'#0a1114',line:'#1c2a2f',grid:'#132024',dim:'#6f8589',dim2:'#879b9f',text:'#e4edef',cyan:'#65d5ff',green:'#67f3a2',amber:'#ffc45a',violet:'#bdabff',red:'#ff7786'};
 const $=id=>document.getElementById(id);
 const ok=v=>v!==null&&v!==undefined&&Number.isFinite(Number(v));
 const money=(v,d=2)=>ok(v)?(v<0?'−$':'$')+Math.abs(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
 const signed=(v,d=2)=>ok(v)?(v<0?'−$':'+$')+Math.abs(v).toFixed(d):'—';
 const cents=v=>ok(v)?Math.round(v*100)+'¢':'—';
 const pct=v=>ok(v)?(v*100).toFixed(1)+'%':'—';
 const cls=v=>!ok(v)||v===0?'':v>0?'pos':'neg';
 const set=(id,t,c)=>{const e=$(id);if(!e)return;e.textContent=t;if(c!==undefined)e.className=c;};
 let S=null,lane='BTC',busy=false;
 const buf={};LANES.forEach(l=>buf[l]={lead:[],impulse:[],spread:[],imbalance:[],vol:[],edge:[],smart:[],decision:[]});
 const MAXB=160;
 function push(a,v){a.push(v);if(a.length>MAXB)a.shift();}

 // ---------- canvas helpers ----------
 function ctx2d(c){const r=c.getBoundingClientRect(),dpr=Math.min(2,devicePixelRatio||1),w=Math.max(10,r.width),h=Math.max(10,r.height);
  if(c.width!==Math.round(w*dpr)||c.height!==Math.round(h*dpr)){c.width=Math.round(w*dpr);c.height=Math.round(h*dpr);}
  const g=c.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,w,h);return [g,w,h];}
 function text(g,s,x,y,col=C.dim,size=9,align='left',weight=''){g.fillStyle=col;g.font=`${weight} ${size}px ui-monospace,SFMono-Regular,Consolas,monospace`;g.textAlign=align;g.fillText(s,x,y);g.textAlign='left';}
 function spark(c,data,color){const[g,w,h]=ctx2d(c);const v=data.filter(ok);if(v.length<2){text(g,'collecting',2,h-6,C.dim,8);return;}
  let lo=Math.min(...v),hi=Math.max(...v);if(hi-lo<1e-9){hi+=1;lo-=1;}const x=i=>i/(data.length-1)*w,y=x0=>h-2-(x0-lo)/(hi-lo)*(h-4);
  if(lo<0&&hi>0){g.strokeStyle=C.grid;g.beginPath();g.moveTo(0,y(0));g.lineTo(w,y(0));g.stroke();}
  g.beginPath();let d=false;data.forEach((p,i)=>{if(!ok(p)){d=false;return;}d?g.lineTo(x(i),y(p)):g.moveTo(x(i),y(p));d=true;});g.strokeStyle=color;g.lineWidth=1.2;g.stroke();
  g.lineTo(w,h);g.lineTo(0,h);g.closePath();g.globalAlpha=.08;g.fillStyle=color;g.fill();g.globalAlpha=1;}

 // ---------- static scaffolding ----------
 function build(){
  $('laneTabs').innerHTML=LANES.map(l=>`<button data-lane="${l}" class="${l===lane?'on':''}">${NAME[l]}</button>`).join('');
  $('laneTabs').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;lane=b.dataset.lane;document.querySelectorAll('#laneTabs button').forEach(x=>x.classList.toggle('on',x.dataset.lane===lane));render();});
  const inputs=[['lead','ORACLE LEAD','bps',C.amber],['impulse','5S IMPULSE','bps',C.amber],['spread','UP SPREAD','¢',C.violet],['imbalance','BOOK IMBALANCE','',C.cyan],['vol','VOLATILITY','σ·1e4',C.violet],['edge','EDGE AFTER FEES','¢',C.green],['smart','SKILLED FLOW','sh',C.green]];
  $('inputs').innerHTML=inputs.map(([k,n,u])=>`<div class="inp"><span>${n}<b id="in_${k}">—</b></span><canvas id="sp_${k}"></canvas></div>`).join('');
  window.__inputs=inputs;
  $('timelines').innerHTML=LANES.map(l=>`<div class="tl"><span style="color:${COL[l]}">${NAME[l]}</span><canvas id="tl_${l}"></canvas><em id="tld_${l}">—</em></div>`).join('');
 }

 // ---------- data ingest ----------
 function ingest(s){
  LANES.forEach(l=>{const a=s.assets?.[l];if(!a)return;const b=buf[l],m=a.model||{},bbo=a.bbo||{},sig=a.strategies?.edgeSignal||{},best=sig.best||{};
   push(b.lead,ok(m.leadBps)?+m.leadBps:null);push(b.impulse,ok(m.impulse5Bps)?+m.impulse5Bps:null);
   const up=bbo.up||{};push(b.spread,ok(up.ask)&&ok(up.bid)?(up.ask-up.bid)*100:null);
   const d=a.depth?.up||{},bs=(d.bids||[]).reduce((n,r)=>n+Number(r.size||0),0),as=(d.asks||[]).reduce((n,r)=>n+Number(r.size||0),0);push(b.imbalance,bs+as>0?(bs-as)/(bs+as):null);
   push(b.vol,ok(m.sigma)?m.sigma*1e4:null);push(b.edge,ok(best.conservativeEdge)?best.conservativeEdge*100:null);push(b.smart,ok(best.smartFlow)?+best.smartFlow:null);
   push(b.decision,{t:Date.now(),state:stateOf(a),text:a.decision||''});});
 }
 function stateOf(a){const p=a.paper||{},d=String(a.decision||'').toLowerCase();
  if(p.position)return 'trade';if(p.status==='risk_locked'||d.includes('lock'))return 'lock';
  if(d.startsWith('edge:')||d.startsWith('model:')||d.includes('opportunity'))return 'signal';
  if(d.includes('stale')||d.includes('unavailable')||d.includes('not live'))return 'feed';return 'wait';}

 // ---------- header, ticker, wallet ----------
 function header(s){
  const chips=[];const seen={};
  LANES.forEach(l=>{const a=s.assets?.[l];if(!a)return;const coin=l.replace('15','');if(!seen[coin]&&ok(a.spot)){seen[coin]=1;const open=a.openRef,chg=ok(open)&&open?((a.spot/open-1)*1e4):null;
   chips.push(`<div class="chip"><span>${coin} ORACLE</span><b>${money(a.spot,coin==='BTC'?0:2)}</b><i class="${cls(chg)}">${ok(chg)?(chg>=0?'+':'')+chg.toFixed(1)+'bp':''}</i></div>`);}});
  LANES.forEach(l=>{const a=s.assets?.[l];if(!a)return;const b=a.bbo||{},left=a.market?.endTs?Math.max(0,a.market.endTs-Date.now()/1000):null;
   chips.push(`<div class="chip"><span style="color:${COL[l]}">${NAME[l]} · ${ok(left)?Math.floor(left/60)+':'+String(Math.floor(left%60)).padStart(2,'0'):'—'}</span><b class="pos">${cents(b.up?.ask)}</b> <b class="neg">${cents(b.down?.ask)}</b></div>`);});
  $('chips').innerHTML=chips.join('');
  const live=LANES.filter(l=>['market','clob','rtds'].every(k=>s.assets?.[l]?.feed?.[k]==='live')).length;
  const tag=$('feedTag');tag.textContent=live===LANES.length?'LIVE FEED':`FEED ${live}/${LANES.length}`;tag.className='tag'+(live===LANES.length?' ok':'');
  const ev=LANES.flatMap(l=>(s.assets?.[l]?.paper?.events||[]).slice(0,12).map(e=>({...e,lane:l}))).filter(e=>e.action!=='CORE').sort((a,b)=>b.t-a.t).slice(0,24);
  const parts=ev.map(e=>{const t=new Date(e.t*1000).toISOString().slice(11,19),pn=ok(e.pnl)?` <span class="${e.pnl>=0?'u':'d'}">${signed(e.pnl)}</span>`:'';
   return `<b>${t}</b> <span style="color:${COL[e.lane]}">${NAME[e.lane]}</span> <em>${e.action}</em> ${e.side||''} ${ok(e.price)?cents(e.price):''}${pn} · ${String(e.detail||'').slice(0,70)}`;});
  const lanesTxt=LANES.map(l=>`<span style="color:${COL[l]}">${NAME[l]}</span> ${String(s.assets?.[l]?.decision||'').slice(0,60)}`);
  const html=(parts.length?parts:lanesTxt).join('   ◆   ');if($('tape').dataset.h!==html){$('tape').innerHTML=html;$('tape').dataset.h=html;}
 }
 function wallet(s){
  const p=s.portfolio||{},e=s.edge||{},tr=e.flows||[];const n=tr.reduce((a,f)=>a+f.trades,0),w=tr.reduce((a,f)=>a+f.wins,0),pnl=tr.reduce((a,f)=>a+f.pnl,0);
  const life=(p.lifetimeRealizedPnl||0)+((p.totalPnl||0)-(p.realizedPnl||0));
  set('lifePnl',signed(life),'big '+cls(life));
  const since=p.epochStart?new Date(p.epochStart*1000).toISOString().slice(5,16).replace('T',' ')+' UTC':null;
  set('lifeLabel',since?'PAPER P&L SINCE FRESH START':'ALL-TIME PAPER P&L');
  set('lifeSub',`${p.lifetimeTrades??'—'} closed trades · $${p.startingBalance||200} paper${since?' since '+since:''}`+(p.archivedTrades?` · earlier ${p.archivedTrades} trades ${signed(p.archivedRealizedPnl)} kept for analysis`:''));
  set('equity',money(p.equity));set('sessPnl',signed(p.totalPnl),cls(p.totalPnl));set('winRate',n?(100*w/n).toFixed(0)+'%':'—');set('avgTrade',n?signed(pnl/n,3):'—',cls(pnl));
  set('maxDd',money(p.maxDrawdownUsd));const open=LANES.filter(l=>s.assets?.[l]?.paper?.position).length;set('openPos',open+' / '+LANES.length);
  rules(s.rulesScorecard);
  const b=$('walletBadge');b.textContent=p.riskLocked?'PORTFOLIO LOCK':'PAPER ONLY';b.className='badge '+(p.riskLocked?'bad':'live');
  $('exposure').innerHTML=(p.assets||[]).map(a=>{const pos=s.assets?.[a.asset]?.paper?.position,cost=pos?Number(pos.entryTotalCost||0):0,eq=Number(a.equity||0),alloc=Number(a.allocation||1);
   const used=Math.min(100,100*cost/alloc),gain=Math.max(-100,Math.min(100,100*(eq-alloc)/alloc));
   return `<div class="bar"><span><b style="color:${COL[a.asset]}">${NAME[a.asset]}</b><span>${money(eq)} / ${money(alloc,0)} · ${a.status}</span></span><div><i style="width:${Math.max(1,used)}%;background:${COL[a.asset]}"></i><i style="width:${Math.abs(gain)}%;background:${gain>=0?C.green:C.red};opacity:.55"></i></div></div>`;}).join('');
 }

 function rules(r){
  const tb=document.querySelector('#rulesTbl tbody');if(!tb)return;
  if(!r||!r.rules){tb.innerHTML='<tr><td colspan="6">collecting…</td></tr>';return;}
  set('rulesDesc',`buy < ${(r.rules.maxEntryPrice*100).toFixed(0)}¢ · edge ≥ ${(r.rules.minEstimatedEdge*100).toFixed(0)}¢ · since ${r.rules.liveSinceUtc.slice(5)} UTC`);
  const row=(label,m,hl)=>{if(!m||!m.trades)return `<tr${hl?' class="hl"':''}><td>${label}</td><td>0</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>`;
   const wr=m.winRatePct,wc=wr>=70?'pos':wr<50?'neg':'';
   return `<tr${hl?' class="hl"':''}><td>${label}</td><td>${m.trades}</td><td class="${wc}">${wr.toFixed(0)}%</td><td class="${cls(m.avgPnlUsd)}">${signed(m.avgPnlUsd,2)}</td><td class="${cls(m.netPnlUsd)}">${signed(m.netPnlUsd,2)}</td><td>${m.profitFactor==null?'—':m.profitFactor.toFixed(2)}</td></tr>`;};
  tb.innerHTML=row('LIVE',r.sinceRulesLive,true)+row('BACKTEST',r.matchingCurrentRules)+row('ALL TIME',r.allHistory);
 }

 // ---------- price candles + ladder ----------
 function candles(s){
  const a=s.assets?.[lane]||{},h=(a.history||[]).filter(x=>ok(x.spot));set('priceTitle',NAME[lane]);
  const[g,w,H]=ctx2d($('candles'));if(h.length<4){text(g,'collecting oracle samples…',10,H/2);return;}
  const bars=[];for(const x of h){const k=Math.floor(x.t/10)*10;let b=bars.at(-1);if(!b||b.k!==k){b={k,o:x.spot,h:x.spot,l:x.spot,c:x.spot,tw:x.twap};bars.push(b);}b.h=Math.max(b.h,x.spot);b.l=Math.min(b.l,x.spot);b.c=x.spot;b.tw=x.twap;}
  const vals=bars.flatMap(b=>[b.h,b.l]).concat(ok(a.openRef)?[a.openRef]:[]);let lo=Math.min(...vals),hi=Math.max(...vals);const pad=(hi-lo)*.12||hi*1e-5;lo-=pad;hi+=pad;
  const L=6,R=w-50,T=8,B=H-14,y=v=>B-(v-lo)/(hi-lo)*(B-T),bw=(R-L)/bars.length;
  for(let i=0;i<4;i++){const v=lo+(hi-lo)*i/3;g.strokeStyle=C.grid;g.beginPath();g.moveTo(L,y(v));g.lineTo(R,y(v));g.stroke();text(g,v.toFixed(v>1000?0:v>10?2:4),w-46,y(v)+3,C.dim,8.5);}
  if(ok(a.openRef)){g.setLineDash([3,3]);g.strokeStyle=C.dim2;g.beginPath();g.moveTo(L,y(a.openRef));g.lineTo(R,y(a.openRef));g.stroke();g.setLineDash([]);text(g,'OPEN',R-28,y(a.openRef)-3,C.dim2,8);}
  g.beginPath();bars.forEach((b,i)=>{if(!ok(b.tw))return;const x=L+bw*(i+.5);i?g.lineTo(x,y(b.tw)):g.moveTo(x,y(b.tw));});g.strokeStyle=C.cyan;g.globalAlpha=.6;g.lineWidth=1;g.stroke();g.globalAlpha=1;
  bars.forEach((b,i)=>{const x=L+bw*(i+.5),up=b.c>=b.o,col=up?C.green:C.red;g.strokeStyle=col;g.beginPath();g.moveTo(x,y(b.h));g.lineTo(x,y(b.l));g.stroke();
   const top=y(Math.max(b.o,b.c)),bh=Math.max(1,Math.abs(y(b.o)-y(b.c)));g.fillStyle=col;g.globalAlpha=up?.85:.75;g.fillRect(x-Math.max(1,bw*.32),top,Math.max(2,bw*.64),bh);g.globalAlpha=1;});
  const last=bars.at(-1).c;g.fillStyle=C.amber;g.fillRect(R+2,y(last)-7,46,14);text(g,last.toFixed(last>1000?0:2),R+4,y(last)+3,'#140d02',9,'left','600');
  text(g,'─ 60s TWAP',L+2,H-3,C.cyan,8.5);text(g,'--- round open',L+70,H-3,C.dim2,8.5);
  const dep=a.depth||{},row=(p,sz,col,max)=>`<div><i style="width:${Math.min(100,100*sz/max)}%;background:${col}"></i><span>${cents(p)}</span><span>${Number(sz).toFixed(0)}</span></div>`;
  let html='';for(const side of ['up','down']){const d=dep[side]||{},asks=(d.asks||[]).slice(0,3).reverse(),bids=(d.bids||[]).slice(0,3),max=Math.max(1,...asks.concat(bids).map(r=>+r.size||0));
   html+=`<div class="h">${side.toUpperCase()} · ASK / BID</div>`+asks.map(r=>row(r.price,r.size,C.red,max)).join('')+bids.map(r=>row(r.price,r.size,C.green,max)).join('');}
  $('ladder').innerHTML=html||'<div class="note">no book</div>';
 }

 // ---------- lifecycle (rounds, fills, resolutions) ----------
 function lifecycle(s){
  const[g,w,h]=ctx2d($('lifecycle')),now=Date.now()/1000,span=1200,t0=now-span*.7,x=t=>70+(t-t0)/span*(w-76),rows=LANES.length,rh=(h-14)/rows;
  for(let k=0;k<=4;k++){const t=t0+span*k/4;g.strokeStyle=C.grid;g.beginPath();g.moveTo(x(t),0);g.lineTo(x(t),h-12);g.stroke();text(g,new Date(t*1000).toISOString().slice(11,16),x(t)-12,h-2,C.dim,8);}
  const res=(s.edge?.resolutions||[]);let fills=0;
  LANES.forEach((l,i)=>{const a=s.assets?.[l]||{},y=i*rh+3,m=a.market;text(g,NAME[l],0,y+rh*.62,COL[l],9);
   res.filter(r=>r.lane===l).forEach(r=>{const x0=Math.max(70,x(r.start)),x1=x(r.end);if(x1<70)return;g.fillStyle=r.winner==='up'?C.green:C.red;g.globalAlpha=.28;g.fillRect(x0,y+2,x1-x0-1,rh-5);g.globalAlpha=1;});
   if(m){const x0=Math.max(70,x(m.startTs)),x1=Math.min(w,x(m.endTs));g.strokeStyle=COL[l];g.lineWidth=1;g.strokeRect(x0,y+2,x1-x0,rh-5);g.fillStyle=COL[l];g.globalAlpha=.12;g.fillRect(x0,y+2,(x(now)-x0),rh-5);g.globalAlpha=1;}
   (a.paper?.events||[]).forEach(e=>{if(!['BUY','SELL','SETTLE','MERGE'].includes(e.action)||e.t<t0)return;fills++;const cx=x(e.t),cy=y+rh/2;g.fillStyle=e.action==='BUY'?C.amber:ok(e.pnl)&&e.pnl<0?C.red:C.green;g.beginPath();g.arc(cx,cy,e.action==='BUY'?3:3.6,0,7);g.fill();});});
  g.strokeStyle=C.text;g.globalAlpha=.6;g.beginPath();g.moveTo(x(now),0);g.lineTo(x(now),h-12);g.stroke();g.globalAlpha=1;
  set('lifeBadge',`${fills} fills · ${res.length} resolved`);
 }

 // ---------- resolution grid ----------
 function resgrid(s){
  const res=(s.edge?.resolutions||[]).slice(-24).reverse(),live=LANES.map(l=>{const a=s.assets?.[l]||{},b=a.bbo?.up||{},mid=ok(b.ask)&&ok(b.bid)?(b.ask+b.bid)/2:null;return `<div class="tile live"><small>${NAME[l]} LIVE</small>${cents(mid)}<small>${a.market?.endTs?Math.max(0,Math.round(a.market.endTs-Date.now()/1000))+'s':'—'}</small></div>`;});
  const tiles=res.map(r=>`<div class="tile ${r.winner}"><small>${NAME[r.lane]||r.lane}</small>${r.winner==='up'?'UP':'DN'}<small>mkt ${cents(r.lastUpMid)} · cal ${cents(r.lastPUp)}</small></div>`);
  const empty=Math.max(0,16-live.length-tiles.length);
  $('resgrid').innerHTML=live.concat(tiles).concat(Array.from({length:empty},()=>'<div class="tile empty"><small>awaiting</small>·</div>')).join('');
  const b=$('resBadge');b.textContent=res.length?res.length+' verified':'awaiting verified resolutions';b.className='badge '+(res.length?'live':'warn');
 }

 // ---------- neural shell ----------
 const shellPts=[],corePts=[];
 (function sphere(){const N=560,G=Math.PI*(3-Math.sqrt(5));for(let i=0;i<N;i++){const y=1-2*(i+.5)/N,r=Math.sqrt(1-y*y),a=i*G;shellPts.push({x:Math.cos(a)*r,y,z:Math.sin(a)*r,j:Math.random()});}
  let seed=7;const rnd=()=>(seed=(seed*16807)%2147483647)/2147483647;for(let i=0;i<300;i++){const u=rnd()*2-1,t=rnd()*Math.PI*2,r=Math.cbrt(rnd())*.42,q=Math.sqrt(1-u*u);corePts.push({x:q*Math.cos(t)*r,y:u*r,z:q*Math.sin(t)*r,j:rnd()});}})();
 const GATES=[['FEED','feeds live'],['OPEN','trusted open'],['ORACLE','oracle direction'],['BOOK','book fresh'],['SPREAD','spread ≤ 2¢'],['EDGE','edge > fees'],['RISK','risk budget'],['ENTRY','order']];
 function gates(a){const d=String(a?.decision||'').toLowerCase(),p=a?.paper||{},sig=a?.strategies?.edgeSignal||{},best=sig.best||{};
  const feed=['market','clob','rtds'].every(k=>a?.feed?.[k]==='live'),open=!!a?.openTrusted;
  const fail=k=>d.includes(k);const st={FEED:feed,OPEN:open,ORACLE:!(fail('oracle')||fail('impulse')||fail('direction')),BOOK:!(fail('book older')||fail('depth')),SPREAD:!fail('spread'),
   EDGE:ok(best.conservativeEdge)?best.conservativeEdge>0:!fail('advantage'),RISK:!(p.status==='risk_locked'||fail('lock')||fail('cooldown')||fail('loss')),ENTRY:!!p.position||d.includes('opportunity')||d.startsWith('edge:')||d.startsWith('model:')};
  return st;}
 let fills={},pulse=0;
 function shellFrame(t){
  const c=$('net');if(!c||!S){requestAnimationFrame(shellFrame);return;}
  const[g,w,h]=ctx2d(c),a=S.assets?.[lane]||{},st=gates(a),sig=a.strategies?.edgeSignal||{},best=sig.best||{},col=COL[lane];
  const pUp=ok(sig.pUp)?sig.pUp:null,edge=ok(best.conservativeEdge)?best.conservativeEdge:null,mis=sig.decision==='MISPRICED';
  const trades=Number(a.paper?.trades||0);if(fills[lane]!==undefined&&trades>fills[lane])pulse=1;fills[lane]=trades;pulse*=.97;
  const inX=0,gx=w*.34,cx=w*.70,cy=h*.52,R=Math.min(w*.25,h*.42),ins=(window.__inputs||[]).length||7,iy=i=>h*(i+.5)/ins,gy=i=>h*.1+i*(h*.8)/(GATES.length-1);
  const time=t/1000;
  // input -> gate flows
  (window.__inputs||[]).forEach((inp,i)=>{GATES.forEach((gt,j)=>{if((i+j)%3)return;const pass=st[gt[0]];g.strokeStyle=pass?inp[3]:C.red;g.globalAlpha=pass?.22:.12;g.lineWidth=1;
   g.beginPath();g.moveTo(inX,iy(i));g.bezierCurveTo(gx*.5,iy(i),gx*.5,gy(j),gx,gy(j));g.stroke();
   const u=((time*.35+i*.13+j*.07)%1);const bx=Math.pow(1-u,3)*inX+3*Math.pow(1-u,2)*u*gx*.5+3*(1-u)*u*u*gx*.5+u*u*u*gx,by=Math.pow(1-u,3)*iy(i)+3*Math.pow(1-u,2)*u*iy(i)+3*(1-u)*u*u*gy(j)+u*u*u*gy(j);
   g.globalAlpha=pass?.9:.35;g.fillStyle=pass?inp[3]:C.red;g.beginPath();g.arc(bx,by,1.6,0,7);g.fill();});});
  // gate -> core flows
  GATES.forEach((gt,j)=>{const pass=st[gt[0]];g.strokeStyle=pass?col:C.red;g.globalAlpha=pass?.28:.12;g.beginPath();g.moveTo(gx,gy(j));g.bezierCurveTo(gx+(cx-gx)*.45,gy(j),cx-R*1.1,cy+(gy(j)-cy)*.3,cx-R*.95,cy+(gy(j)-cy)*.35);g.stroke();});
  g.globalAlpha=1;
  // gate column
  g.strokeStyle=C.line;g.beginPath();g.moveTo(gx,gy(0)-12);g.lineTo(gx,gy(GATES.length-1)+12);g.stroke();
  GATES.forEach((gt,j)=>{const pass=st[gt[0]],y=gy(j);g.fillStyle=pass?C.green:C.red;g.globalAlpha=.18;g.beginPath();g.arc(gx,y,9,0,7);g.fill();g.globalAlpha=1;g.beginPath();g.arc(gx,y,4,0,7);g.fill();
   const roomy=(gy(1)-gy(0))>=26;text(g,gt[0],gx+12,roomy?y-1:y+3,pass?C.text:C.red,9,'left','600');if(roomy)text(g,gt[1],gx+12,y+9,C.dim,8);});
  // sphere: teal shell + gold core; core grows with calibrated conviction and pulses on fills
  const rot=time*.18,tilt=-.3,conv=pUp===null?0:Math.abs(pUp-.5)*2,coreScale=.75+conv*.5+pulse*.35;
  const proj=(p,s)=>{const x=p.x*s,y=p.y*s,z=p.z*s,c1=Math.cos(rot),s1=Math.sin(rot),x1=x*c1+z*s1,z1=-x*s1+z*c1,c2=Math.cos(tilt),s2=Math.sin(tilt),y1=y*c2-z1*s2,z2=y*s2+z1*c2,k=3/(3-z2);return{x:cx+x1*R*k,y:cy+y1*R*k,z:z2};};
  const glow=g.createRadialGradient(cx,cy,0,cx,cy,R*1.25);glow.addColorStop(0,'rgba(255,196,90,'+(.10+conv*.12+pulse*.15)+')');glow.addColorStop(1,'rgba(7,12,14,0)');g.fillStyle=glow;g.fillRect(cx-R*1.3,cy-R*1.3,R*2.6,R*2.6);
  const pts=shellPts.map(p=>({...proj(p,1+.012*Math.sin(time*2+p.j*9)),c:mis?C.green:C.cyan,s:.9})).concat(corePts.map(p=>({...proj(p,coreScale*(1+.04*Math.sin(time*3+p.j*7))),c:C.amber,s:1.25})));
  pts.sort((a,b)=>a.z-b.z).forEach(p=>{const f=(p.z+1)/2;g.globalAlpha=.25+.75*f;g.fillStyle=p.c;g.beginPath();g.arc(p.x,p.y,p.s*(.6+f),0,7);g.fill();});
  g.globalAlpha=1;
  if(mis){const u=(time*.8)%1;g.strokeStyle=C.green;g.globalAlpha=.7*(1-u);g.lineWidth=1.5;g.beginPath();g.arc(cx,cy,R*(1+u*.35),0,7);g.stroke();g.globalAlpha=1;}
  text(g,NAME[lane]+' DECISION CORE',cx,cy+R+22,C.dim2,9,'center','600');
  text(g,mis?'MISPRICING DETECTED':a.paper?.position?'IN POSITION':(a.decision||'').slice(0,48),cx,cy+R+34,mis?C.green:C.dim,8.5,'center');
  requestAnimationFrame(shellFrame);
 }
 function shellPanel(s){
  const a=s.assets?.[lane]||{},b=buf[lane],sig=a.strategies?.edgeSignal||{},best=sig.best||{},e=s.edge||{},es=e.strategies?.edge||{},ms=a.strategies?.momentumSizing||{};
  set('shellLane',NAME[lane]);set('confLane',NAME[lane]);
  (window.__inputs||[]).forEach(([k,,u,col])=>{const v=b[k].filter(ok).at(-1);set('in_'+k,ok(v)?(Math.abs(v)<10?v.toFixed(2):v.toFixed(0))+(u?' '+u:''):'—');spark($('sp_'+k),b[k],col);});
  const p=ok(sig.pUp)?sig.pUp:null;set('confP',p===null?'—':pct(p),p===null?'':p>=.5?'pos':'neg');$('confBar').style.width=(p===null?0:Math.abs(p-.5)*200)+'%';$('confBar').style.background=p>=.5?C.green:C.red;
  set('confEdge',`EDGE AFTER FEES ${ok(best.conservativeEdge)?(best.conservativeEdge*100).toFixed(1)+'¢ '+(best.side||''):'—'} · ${sig.cellRounds??0} cal rounds`);
  set('confStrat',e.promotedStrategy?`STRATEGY ${e.promotedStrategy.toUpperCase()} · LIVE ON PAPER`:`STRATEGY SHADOW · ${Math.min(es.bets||0,40)}/40 BETS`);
  set('confSize',`MOMENTUM SIZING ${ms.earned?'EARNED 1–2×':'1× (earning)'} · ${ms.trades??0} rec`);
  const mk=e.maker||{};set('confMaker',`MAKER ${mk.gatePassed?'PROMOTED · FUNDED QUOTES':'SHADOW · '+Math.min(mk.bets||0,40)+'/40 CYCLES'}`);
  const sb=$('shellBadge');sb.textContent=sig.decision||a.decision||'—';sb.className='badge '+(sig.decision==='MISPRICED'?'live':'');
  LANES.forEach(l=>{const c=$('tl_'+l);if(!c)return;const[g,w,h]=ctx2d(c),d=buf[l].decision,cw=w/MAXB;
   const colr={trade:C.amber,signal:C.green,lock:C.red,feed:C.violet,wait:'#1d3036'};d.forEach((x,i)=>{g.fillStyle=colr[x.state];g.fillRect(w-(d.length-i)*cw,0,Math.ceil(cw),h);});
   set('tld_'+l,(s.assets?.[l]?.decision||'—'));});
 }

 // ---------- edge matrix ----------
 function matrix(s){
  const all=LANES.flatMap(l=>((s.edge?.samples||{})[l]||[]).map(p=>({...p,l}))).filter(p=>ok(p.upMid)&&ok(p.pUp));
  const[g,w,h]=ctx2d($('scatter')),L=28,B=h-16,T=6,R=w-6,x=v=>L+v*(R-L),y=v=>B-v*(B-T);
  g.fillStyle='rgba(103,243,162,.05)';g.beginPath();g.moveTo(x(0),y(0));g.lineTo(x(0),y(1));g.lineTo(x(1),y(1));g.fill();g.fillStyle='rgba(255,119,134,.05)';g.beginPath();g.moveTo(x(0),y(0));g.lineTo(x(1),y(0));g.lineTo(x(1),y(1));g.fill();
  for(let k=0;k<=4;k++){const v=k/4;g.strokeStyle=C.grid;g.beginPath();g.moveTo(x(v),T);g.lineTo(x(v),B);g.moveTo(L,y(v));g.lineTo(R,y(v));g.stroke();text(g,(v*100).toFixed(0),x(v)-6,h-4,C.dim,8);text(g,(v*100).toFixed(0),2,y(v)+3,C.dim,8);}
  g.strokeStyle=C.dim2;g.setLineDash([4,3]);g.beginPath();g.moveTo(x(0),y(0));g.lineTo(x(1),y(1));g.stroke();g.setLineDash([]);
  text(g,'MODEL P(UP) ↑',L+4,T+10,C.dim2,8.5);text(g,'MARKET UP PRICE →',R-96,B-4,C.dim2,8.5);
  const now=Date.now()/1000;all.forEach(p=>{const age=Math.min(1,(now-p.t)/900),d=p.pUp-p.upMid;g.globalAlpha=.25+.75*(1-age);g.fillStyle=d>=0?C.green:C.red;g.beginPath();g.arc(x(p.upMid),y(p.pUp),2.4,0,7);g.fill();});
  g.globalAlpha=1;if(!all.length)text(g,'collecting model-vs-market samples…',w/2,h/2,C.dim,10,'center');
  const[g2,w2,h2]=ctx2d($('hist')),bins=Array(24).fill(0);all.forEach(p=>{const i=Math.max(0,Math.min(23,Math.floor((p.pUp-p.upMid+.3)/.6*24)));bins[i]++;});const mx=Math.max(1,...bins);
  bins.forEach((n,i)=>{const bw=(w2-28)/24,v=-.3+(i+.5)*.6/24;g2.fillStyle=v>=0?C.green:C.red;g2.globalAlpha=.75;g2.fillRect(28+i*bw,h2-12-(h2-16)*n/mx,bw-1,(h2-16)*n/mx);});g2.globalAlpha=1;
  text(g2,'−30¢',28,h2-2,C.dim,8);text(g2,'model − market',w2/2,h2-2,C.dim,8,'center');text(g2,'+30¢',w2-24,h2-2,C.dim,8);
  const e=s.edge||{},cal=e.calibration||{};
  $('etable').innerHTML='<div class="h"><span>LANE</span><span>EDGE · DECISION</span></div>'+LANES.map(l=>{const sg=(e.signals||{})[l]||{},b=sg.best||{},v=b.conservativeEdge;
   return `<div><span style="color:${COL[l]}">${NAME[l]}</span><span class="${cls(v)}">${ok(v)?(v*100).toFixed(1)+'¢':'—'}</span></div><div style="border:0;color:${C.dim};font-size:9px">${String(sg.decision||'—').slice(0,30)}</div>`;}).join('')+
   `<div class="h" style="margin-top:6px"><span>CALIBRATION</span><span>${cal.samples||0} samples</span></div><div><span>Brier heur.</span><span>${ok(cal.brierHeuristic)?cal.brierHeuristic.toFixed(3):'—'}</span></div><div><span>Brier calib.</span><span>${ok(cal.brierCalibrated)?cal.brierCalibrated.toFixed(3):'—'}</span></div>`;
  const mb=$('matrixBadge');const above=all.filter(p=>p.pUp>p.upMid).length;mb.textContent=all.length?`${all.length} samples · ${Math.round(100*above/all.length)}% model > market`:'collecting';
 }

 // ---------- money flow sankey ----------
 function sankey(s){
  const f=(s.edge?.flows||[]).filter(x=>x.trades>0),[g,w,h]=ctx2d($('sankey'));
  if(!f.length){text(g,'no closed trades in the record yet',w/2,h/2,C.dim,10,'center');set('flowBadge','—');return;}
  const SN={MOMENTUM_SCALP:'MOMENTUM',EDGE_HOLD:'MISPRICING',MODEL_SCALP:'MODEL',LAG_SCALP:'LAG',PAIR:'PAIR'},lanes=[...new Set(f.map(x=>x.lane))],strats=[...new Set(f.map(x=>x.strategy))];
  const total=f.reduce((a,x)=>a+x.trades,0),wins=f.reduce((a,x)=>a+x.wins,0),losses=total-wins,pad=6,top=14,usable=h-top-8-22*4;
  const col=(items,weights,X)=>{const gap=pad*(items.length-1),sc=(usable-gap)/total;let y=top;return Object.fromEntries(items.map((k,i)=>{const hh=weights[i]*sc,r={x:X,y,h:hh,used:0,usedR:0};y+=Math.max(hh,22)+pad;return [k,r];}));};
  const LX=86,MX=w*.48,RX=w-92,NW=10;
  const nL=col(lanes,lanes.map(l=>f.filter(x=>x.lane===l).reduce((a,x)=>a+x.trades,0)),LX),nM=col(strats,strats.map(k=>f.filter(x=>x.strategy===k).reduce((a,x)=>a+x.trades,0)),MX),nR=col(['WIN','LOSS'],[wins,losses],RX);
  const sc=(usable-pad*(Math.max(lanes.length,strats.length,2)-1))/total;
  const band=(a,b,hh,color,ay,by)=>{g.fillStyle=color;g.globalAlpha=.32;g.beginPath();const x0=a.x+NW,x1=b.x,mx=(x0+x1)/2;g.moveTo(x0,ay);g.bezierCurveTo(mx,ay,mx,by,x1,by);g.lineTo(x1,by+hh);g.bezierCurveTo(mx,by+hh,mx,ay+hh,x0,ay+hh);g.closePath();g.fill();g.globalAlpha=1;};
  f.forEach(x=>{const a=nL[x.lane],b=nM[x.strategy],hh=x.trades*sc;band(a,b,hh,COL[x.lane]||C.cyan,a.y+a.usedR,b.y+b.used);a.usedR+=hh;b.used+=hh;});
  strats.forEach(k=>{const m=nM[k];m.usedR=0;const fw=f.filter(x=>x.strategy===k),wn=fw.reduce((a,x)=>a+x.wins,0),ls=fw.reduce((a,x)=>a+x.losses,0);
   [['WIN',wn,C.green],['LOSS',ls,C.red]].forEach(([o,n,c])=>{if(!n)return;const r=nR[o],hh=n*sc;band(m,r,hh,c,m.y+m.usedR,r.y+r.used);m.usedR+=hh;r.used+=hh;});});
  const node=(n,label,color,sub,right)=>{g.fillStyle=color;g.fillRect(n.x,n.y,NW,n.h);text(g,label,right?n.x+NW+5:n.x-5,n.y+n.h/2,color,9,right?'left':'right','600');if(sub)text(g,sub,right?n.x+NW+5:n.x-5,n.y+n.h/2+10,C.dim,8,right?'left':'right');};
  lanes.forEach(l=>{const fl=f.filter(x=>x.lane===l);node(nL[l],NAME[l]||l,COL[l]||C.cyan,`${fl.reduce((a,x)=>a+x.trades,0)} · ${signed(fl.reduce((a,x)=>a+x.pnl,0))}`,false);});
  strats.forEach(k=>{const fl=f.filter(x=>x.strategy===k);node(nM[k],SN[k]||k,C.text,`${signed(fl.reduce((a,x)=>a+x.pnl,0))}`,true);});
  const won=f.reduce((a,x)=>a+x.won,0),lost=f.reduce((a,x)=>a+x.lost,0);node(nR.WIN,'WINS '+wins,C.green,signed(won),true);if(losses)node(nR.LOSS,'LOSSES '+losses,C.red,signed(-lost),true);
  text(g,'LANE',LX,9,C.dim,8.5);text(g,'STRATEGY',MX,9,C.dim,8.5);text(g,'OUTCOME',RX,9,C.dim,8.5);
  set('flowBadge',`${total} closed positions · net ${signed(won-lost)}`);
 }

 // ---------- footer ----------
 function footer(s){
  const p=s.portfolio||{},e=s.edge||{},r=e.readiness||{},chk=(r.checks||[]).filter(c=>c.pass).length,f=e.flows||[],n=f.reduce((a,x)=>a+x.trades,0),w=e.wallets||{},inf=s.infrastructure||{};
  $('foot').innerHTML=[['LIFETIME',`<b class="${cls(p.lifetimeRealizedPnl)}">${signed(p.lifetimeRealizedPnl)}</b>`],['TRADES',`<b>${p.lifetimeTrades??n}</b>`],['AVG TRADE',`<b>${n?signed(f.reduce((a,x)=>a+x.pnl,0)/n,3):'—'}</b>`],
   ['CALIBRATION',`<b>${e.calibration?.samples||0}</b> samples`],['WALLETS',`<b>${w.skilledWallets||0}</b> skilled / ${w.walletsTracked||0} studied`],['ROUNDS PENDING',`<b>${e.pendingRounds??'—'}</b>`],
   ['READY FOR REAL MONEY',`<b class="${r.readyForRealMoney?'pos':'neg'}">${chk}/${(r.checks||[]).length||6}</b>`],['STORAGE',`<b>${inf.backend||'—'}</b>`],['REVISION',`<b>${p.revision||'—'}</b>`],['PAPER ONLY','<b>no real orders</b>']]
   .map(([k,v])=>`<span>${k} ${v}</span>`).join('');
 }

 function render(){if(!S)return;header(S);wallet(S);candles(S);lifecycle(S);resgrid(S);shellPanel(S);matrix(S);sankey(S);footer(S);}
 async function poll(){if(busy)return;busy=true;try{const r=await fetch('/api/poly/demo/server?all_assets=true',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);S=await r.json();ingest(S);render();}
  catch(e){const t=$('feedTag');t.textContent='RECONNECTING';t.className='tag';}finally{busy=false;setTimeout(poll,document.hidden?4000:1300);}}
 function clock(){const d=new Date();set('clock',d.toISOString().slice(11,19));set('clock2','UTC · '+d.toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:false})+' IST');}
 build();setInterval(clock,1000);clock();poll();requestAnimationFrame(shellFrame);
 let rt;window.addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(render,120);});
})();
