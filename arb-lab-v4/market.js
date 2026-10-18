'use strict';
(() => {
  const $=id=>document.getElementById(id);
  let latestSeq=0,lastData=null,history=[],pollTimer=null,clockTimer=null,flowStarted=false;

  function money(v,p=5){const n=Number(v)||0;return (n<0?'-$':'+$')+Math.abs(n).toFixed(p)}
  function plainMoney(v,p=5){return '$'+(Number(v)||0).toFixed(p)}
  function cls(v){return v==='EXECUTE-CANDIDATE'?'green':v==='WATCH'?'amberText':'red'}
  function set(id,value){const e=$(id);if(e)e.textContent=value}
  function setClass(id,value){const e=$(id);if(e)e.className=value}

  function renderLatest(d){
    if(!d)return;
    lastData=d;
    const profit=Number(d.conservativeProfitUsd)||0;
    set('simProfit',money(profit,5)); if($('simProfit')) $('simProfit').style.color=profit>=0?'var(--green)':'var(--red)';
    set('simReturn',plainMoney(d.projectedReturnUsd,5));
    set('solPrice','$'+Number(d.solPriceUsd||0).toFixed(2));
    set('spreadBps',(Number(d.conservativeEdgeBps)>=0?'+':'')+Number(d.conservativeEdgeBps||0).toFixed(2)+' bps');
    set('lastScan',new Date((d.timestamp||0)*1000).toLocaleTimeString());
    set('confidence',Number(d.confidence||0).toFixed(0)+'%'); setClass('confidence',cls(d.verdict));
    if($('confidenceBar')) $('confidenceBar').style.width=Math.max(2,Math.min(100,Number(d.confidence)||0))+'%';
    set('qSpend',Number(d.startSolUi||0).toFixed(9)+' SOL');
    set('midUsdc','$'+Number(d.midUsdcUi||0).toFixed(6));
    set('qReturn',Number(d.returnSolUi||0).toFixed(9)+' SOL');
    set('rawProfit',money(d.rawProfitUsd,6));
    set('feeAllowance','-$'+Number(d.feeAllowanceUsd||0).toFixed(6));
    set('moveBuffer','-$'+Number(d.moveBufferUsd||0).toFixed(6));
    set('qNet',money(profit,6)); setClass('qNet',profit>=0?'green':'red');
    set('marketState',d.verdict); setClass('marketState',cls(d.verdict));
    set('scanStatus','LIVE · #'+String(d.seq||0)); setClass('scanStatus',cls(d.verdict));
    set('coreScore',Number(d.confidence||0).toFixed(0));
    set('scanCountdown','LIVE');
    set('networkState','LIVE SCANNER');

    const hint=d.verdict==='EXECUTE-CANDIDATE'
      ? 'Positive conservative quote detected. If the bot wallet is funded, unlocked and armed, the execution engine will immediately re-quote before any real trade.'
      : d.verdict==='WATCH'
      ? 'Raw quote is positive but does not clear conservative fee / move allowances. No trade.'
      : 'No fee-adjusted opportunity right now. Continuous scan remains active.';
    set('scanHint',hint);

    if((d.seq||0)!==latestSeq){
      const first=latestSeq===0;
      latestSeq=d.seq||0;
      if(!first && d.verdict==='EXECUTE-CANDIDATE'){
        notifyOpportunity(d);
        window.dispatchEvent(new CustomEvent('arb-lab-opportunity',{detail:d}));
      }
    }
  }

  function renderHistory(items){
    history=Array.isArray(items)?items.slice(0,24):[];
    const body=$('historyBody');if(!body)return;
    body.replaceChildren();
    if(!history.length){
      const e=document.createElement('div');e.className='history-empty';e.textContent='Live scanner warming up…';body.append(e);drawChart();return;
    }
    for(const h of history.slice(0,12)){
      const row=document.createElement('div');row.className='history-row';
      const time=document.createElement('span');time.textContent=new Date((h.timestamp||0)*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
      const pnl=document.createElement('b');pnl.textContent=money(h.conservativeProfitUsd,5);pnl.className=Number(h.conservativeProfitUsd)>=0?'pos':'neg';
      const edge=document.createElement('span');edge.textContent=(Number(h.conservativeEdgeBps)>=0?'+':'')+Number(h.conservativeEdgeBps||0).toFixed(1)+'bp';
      const conf=document.createElement('span');conf.textContent=Number(h.confidence||0).toFixed(0)+'%';
      const state=document.createElement('b');state.textContent=h.verdict||'—';state.className=h.verdict==='EXECUTE-CANDIDATE'?'pos':h.verdict==='WATCH'?'watch':'neg';
      row.append(time,pnl,edge,conf,state);body.append(row);
    }
    drawChart();
  }

  function drawChart(){
    const c=$('pnlCanvas');if(!c)return;
    const rect=c.getBoundingClientRect(),dpr=Math.max(1,Math.min(2,window.devicePixelRatio||1));
    const w=Math.max(280,rect.width||400),h=150;
    c.width=Math.floor(w*dpr);c.height=Math.floor(h*dpr);
    const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);
    x.strokeStyle='#14201e';x.lineWidth=1;
    for(let i=1;i<5;i++){x.beginPath();x.moveTo(0,i*h/5);x.lineTo(w,i*h/5);x.stroke()}
    const pts=history.slice(0,24).reverse();
    x.strokeStyle='#33423d';x.beginPath();x.moveTo(0,h/2);x.lineTo(w,h/2);x.stroke();
    if(!pts.length)return;
    const vals=pts.map(p=>Number(p.conservativeProfitUsd)||0);
    const max=Math.max(.0001,...vals.map(Math.abs)),zero=h/2;
    x.strokeStyle=vals[vals.length-1]>=0?'#77ff9b':'#ff5f72';x.lineWidth=1.5;x.beginPath();
    pts.forEach((p,i)=>{const px=pts.length===1?w/2:8+i*(w-16)/(pts.length-1);const py=zero-(Number(p.conservativeProfitUsd)||0)/max*(h*.38);if(i)x.lineTo(px,py);else x.moveTo(px,py)});x.stroke();
    pts.forEach((p,i)=>{const px=pts.length===1?w/2:8+i*(w-16)/(pts.length-1);const py=zero-(Number(p.conservativeProfitUsd)||0)/max*(h*.38);x.fillStyle=(Number(p.conservativeProfitUsd)||0)>=0?'#77ff9b':'#ff5f72';x.beginPath();x.arc(px,py,2.2,0,Math.PI*2);x.fill()});
  }

  async function poll(){
    try{
      const r=await fetch('/api/live-market',{cache:'no-store'});
      const j=await r.json();
      if(!r.ok)throw new Error(j.detail||('HTTP '+r.status));
      if(j.latest){renderLatest(j.latest);set('routeVenues',(j.latest.routeVenues||[]).join(' · ')||'Jupiter route');set('momentumShort',(Number(j.latest.shortMomentumBps||0)>=0?'+':'')+Number(j.latest.shortMomentumBps||0).toFixed(2)+' bps');set('dislocationScore',Number(j.latest.dislocationScore||0).toFixed(0)+' / 100');renderHistory(j.history)}
      else {set('scanStatus','SERVER SCANNER WARMING UP');setClass('scanStatus','amberText');set('networkState','SCANNER WARMING')}
    }catch(e){
      set('scanStatus','LIVE FEED ERROR');setClass('scanStatus','red');set('networkState','FEED ERROR');set('scanHint','Live feed error: '+e.message);
    }
  }

  function updateAge(){
    set('utcClock',new Date().toISOString().slice(11,19));
    if(lastData){
      const age=Math.max(0,Math.floor(Date.now()/1000-Number(lastData.timestamp||0)));
      set('freshness',age+'s AGO');
    }
  }

  async function enableAlerts(){
    const b=$('notifyBtn');if(!('Notification' in window)){if(b)b.textContent='ALERTS UNSUPPORTED';return}
    try{
      const p=await Notification.requestPermission();
      if(b)b.textContent=p==='granted'?'✓ ALERTS ENABLED':'ALERTS '+p.toUpperCase();
    }catch{if(b)b.textContent='ALERTS UNAVAILABLE'}
  }

  function notifyOpportunity(d){
    const msg='Projected $2 net '+money(d.conservativeProfitUsd,5)+' · '+Number(d.confidence||0).toFixed(0)+'% heuristic confidence';
    if('Notification' in window && Notification.permission==='granted'){
      try{new Notification('ARB/LAB · Solana opportunity',{body:msg,tag:'arb-lab-opportunity'})}catch{}
    }
    const status=$('scanStatus');if(status){status.textContent='OPPORTUNITY · '+msg;status.className='green'}
  }

  function startFlow(){
    if(flowStarted)return;flowStarted=true;
    const c=$('flowCanvas');if(!c)return;const ctx=c.getContext('2d');let t=0;
    function frame(){
      const r=c.getBoundingClientRect(),dpr=Math.max(1,Math.min(2,window.devicePixelRatio||1));
      if(c.width!==Math.floor(r.width*dpr)||c.height!==Math.floor(r.height*dpr)){c.width=Math.floor(r.width*dpr);c.height=Math.floor(r.height*dpr)}
      ctx.setTransform(dpr,0,0,dpr,0,0);const w=r.width,h=r.height,cx=w/2,cy=h/2;ctx.clearRect(0,0,w,h);t+=.008;
      const nodes=[],count=w<650?90:150;
      for(let i=0;i<count;i++){const a=i*.618033*6.283+t*(i%3===0?1:-.55),ring=.18+(i%11)/13,rx=w*(.13+ring*.34),ry=h*(.12+ring*.28),warp=Math.sin(t*2+i*.13)*8,nx=cx+Math.cos(a)*rx+Math.sin(i)*warp,ny=cy+Math.sin(a*1.03)*ry;nodes.push({x:nx,y:ny})}
      ctx.globalAlpha=.16;ctx.strokeStyle='#6fa7ff';ctx.lineWidth=.55;
      for(let i=0;i<nodes.length;i+=3){const a=nodes[i],b=nodes[(i*7+17)%nodes.length];ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.quadraticCurveTo(cx,cy,b.x,b.y);ctx.stroke()}
      ctx.globalAlpha=1;
      for(let i=0;i<nodes.length;i++){const n=nodes[i];ctx.fillStyle=i%5===0?'#ffc44d':i%7===0?'#77ff9b':'#6fa7ff';const z=i%9===0?2.4:1.2;ctx.fillRect(n.x-z/2,n.y-z/2,z,z)}
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function boot(){
    const nb=$('notifyBtn');if(nb)nb.addEventListener('click',enableAlerts);
    if(nb && 'Notification' in window && Notification.permission==='granted')nb.textContent='✓ ALERTS ENABLED';
    startFlow();poll();pollTimer=setInterval(poll,1000);clockTimer=setInterval(updateAge,1000);updateAge();
    window.addEventListener('resize',drawChart);
  }

  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})();