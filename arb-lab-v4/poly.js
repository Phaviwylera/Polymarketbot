'use strict';
const POLY_ENGINE_BASE='https://poly-edge-paper-engine.onrender.com';
(() => {
  const $=id=>document.getElementById(id);
  const state={
    asset:'BTC',snap:null,history:[],tape:[],alerts:false,lastMarket:'',lastSignalKey:'',lastSignalAt:0,
    market:null,spot:0,twap:0,openRef:0,openTrusted:false,bbo:{up:{},down:{}},
    latestBest:null,latestOpportunity:false,latestMode:'WARMING',
    clob:{readyState:0},rtds:{readyState:0}
  };
  window.POLY_EDGE_STATE=state;

  const num=v=>{if(v===null||v===undefined||v==='')return null;const x=Number(v);return Number.isFinite(x)?x:null;};
  const fmt=(v,d=2)=>num(v)!==null?Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}):'—';
  const pct=v=>num(v)!==null?(Number(v)*100).toFixed(1)+'%':'—';
  const bps=v=>num(v)!==null?(Number(v)>=0?'+':'')+Number(v).toFixed(2)+' bps':'—';
  const istClock=d=>new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(d);
  const istTape=d=>new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(d);
  const istMarket=ts=>new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:true}).format(new Date(Number(ts)*1000));
  const set=(id,val,cls='')=>{const e=$(id);if(!e)return;e.textContent=val;if(cls)e.className=cls};

  function renderPortfolio(j){
    const p=j.portfolio;
    set('assetPriceLabel',state.asset+' / TWAP PRICE');set('mobileAssetLabel',state.asset);
    if(!p){set('portfolioRisk','ENGINE UPDATE PENDING');return;}
    set('portfolioEquity','$'+fmt(p.equity,4));
    set('portfolioPnl','P/L '+(p.totalPnl>=0?'+$':'-$')+fmt(Math.abs(p.totalPnl),4),p.totalPnl>=0?'good':'bad');
    set('portfolioRisk',p.riskLocked?'PORTFOLIO LOCKED':'PAPER SCANNERS ACTIVE',p.riskLocked?'bad':'good');
    set('assetScope','Selected '+state.asset+' · $'+fmt(p.allocationPerAsset,2)+' allocation · all four scanners run in parallel');
    for(const lane of p.assets||[]){
      const button=document.querySelector('[data-asset="'+lane.asset+'"]');if(!button)continue;
      const fresh=lane.spotAgeSec!=null&&lane.twapAgeSec!=null&&Math.max(lane.spotAgeSec,lane.twapAgeSec)<5;
      const live=fresh&&Object.values(lane.feed||{}).every(v=>v==='live');
      button.textContent=lane.asset+' · '+(lane.status==='risk_locked'?'LOCKED':live?'LIVE':'SYNCING')+' · '+(lane.totalPnl>=0?'+$':'-$')+fmt(Math.abs(lane.totalPnl||0),3);
      button.setAttribute('aria-pressed',String(lane.asset===state.asset));
      button.title=(lane.decision||'Waiting for market/feed')+' · '+(lane.trades||0)+' completed paper trades';
    }
  }
  function selectAsset(asset){
    if(asset===state.asset)return;
    state.asset=asset;state.history=[];state.tape=[];state.snap=null;state.lastMarket='';state.lastSignalKey='';
    for(const button of document.querySelectorAll('[data-asset]'))button.setAttribute('aria-pressed',String(button.dataset.asset===asset));
    set('assetPriceLabel',asset+' / TWAP PRICE');set('mobileAssetLabel',asset);set('feedStatus','SWITCHING TO '+asset,'warn');
    window.PolyNetworkOrb?.setTelemetry({},[]);
    // In-flight responses for the old asset are discarded by render().
    if(!pollBusy){clearTimeout(pollTimer);poll();}
  }

  function syncPublicState(j){
    state.snap=j;
    state.market=j.market||null;state.spot=num(j.spot)||0;state.twap=num(j.twap)||0;
    state.openRef=num(j.openRef)||0;state.openTrusted=!!j.openTrusted;state.bbo=j.bbo||state.bbo;
    state.latestBest=j.candidate||null;state.latestOpportunity=String(j.decision||'').startsWith('OPPORTUNITY_');
    state.latestMode=j.mode||'WAIT';state.clob.readyState=j.feed?.clob==='live'?1:0;state.rtds.readyState=j.feed?.rtds==='live'?1:0;
  }

  function renderCore(core){
    if(!core){
      set('vegaVerdict','WARMING','warn');set('vegaSummary','Waiting for asset/TWAP direction and impulse.');
      set('titanVerdict','WAIT','warn');set('titanSummary','Waiting for executable signal and hard risk checks.');
      set('coreFinal','WAIT','warn');return;
    }
    const v=core.VEGA||{},t=core.TITAN||{},fv=String(v.verdict||'WAIT'),ft=String(t.verdict||'WAIT');
    set('vegaVerdict',fv,/CONFIRM/.test(fv)?'good':'warn');
    set('vegaSummary',v.summary||'Waiting for lag signal.');
    set('titanVerdict',ft,/APPROVE/.test(ft)?'good':/REJECT/.test(ft)?'bad':'warn');
    set('titanSummary',t.summary||'Waiting for risk checks.');
    set('coreFinal',String(core.final||ft),/APPROVE/.test(core.final||ft)?'good':/REJECT/.test(core.final||ft)?'bad':'warn');
    set('mobileDockCore',String(core.final||ft),/APPROVE/.test(core.final||ft)?'good':/REJECT/.test(core.final||ft)?'bad':'warn');
  }

  function render(j){
    if(j.asset&&j.asset!==state.asset)return;
    syncPublicState(j);
    renderPortfolio(j);
    renderCore(j.core);
    window.dispatchEvent(new CustomEvent('poly-edge-server-snapshot',{detail:j}));
    const m=j.market||{},b=j.bbo||{},model=j.model||{},c=j.candidate||{},paper=j.paper||{};
    const now=Number(j.serverTime)||Date.now()/1000,left=Math.max(0,(Number(m.endTs)||now)-now);
    const feedLive=j.feed?.market==='live'&&j.feed?.clob==='live'&&j.feed?.rtds==='live';
    set('feedStatus',feedLive?'SERVER LIVE':'SYNCING',feedLive?'good':'warn');
    set('clock',istClock(new Date(now*1000))+' IST');
    set('marketState',m.acceptingOrders?'ACCEPTING ORDERS':'READ ONLY',m.acceptingOrders?'good':'warn');
    set('question',m.startTs&&m.endTs?((j.asset||'BTC')+' Up or Down · '+istMarket(m.startTs)+' – '+istMarket(m.endTs)+' IST'):(m.question||(j.asset||'BTC')+' Up or Down · 5m'));
    const leftText=String(Math.floor(left/60)).padStart(2,'0')+':'+String(Math.floor(left%60)).padStart(2,'0');
    set('timeLeft',leftText);set('mobileDockTimer',leftText);
    set('volume','$'+fmt(m.volume,0));set('liquidity','$'+fmt(m.liquidity,0));set('minOrder',m.minOrderSize?fmt(m.minOrderSize,2)+' shares':'—');
    if($('marketLink')&&m.slug)$('marketLink').href='https://polymarket.com/event/'+encodeURIComponent(m.slug);

    const up=b.up||{},down=b.down||{};
    set('upBbo',(num(up.bid)==null?'—':fmt(up.bid,3))+' / '+(num(up.ask)==null?'—':fmt(up.ask,3)));
    set('downBbo',(num(down.bid)==null?'—':fmt(down.bid,3))+' / '+(num(down.ask)==null?'—':fmt(down.ask,3)));
    set('upAsk',num(up.ask)==null?'—':pct(up.ask));set('downAsk',num(down.ask)==null?'—':pct(down.ask));

    const pair=j.pair||{},inv=pair.inventory||paper.inventory||null;
    const pairEdge=num(pair.lockedEdgePerShare),pairCost=num(pair.takerPairCost),pairRaw=num(pair.rawPairCost);
    let residual='NONE',residualWarn=false,invQuote='—';
    if(paper.position){
      residual=(paper.position.side||'NONE')+' '+fmt(paper.position.shares,2)+' sh';residualWarn=true;
    }else if(inv){
      const upLots=Array.isArray(inv.upLots)?inv.upLots:[],dnLots=Array.isArray(inv.downLots)?inv.downLots:[];
      const upSh=upLots.reduce((a,x)=>a+Number(x.shares||0),0),dnSh=dnLots.reduce((a,x)=>a+Number(x.shares||0),0);
      if(upSh>dnSh+1e-9){residual='UP '+fmt(upSh-dnSh,2)+' sh';residualWarn=true}
      else if(dnSh>upSh+1e-9){residual='DOWN '+fmt(dnSh-upSh,2)+' sh';residualWarn=true}
      if(inv.quote)invQuote=String(inv.quote.side||'').toUpperCase()+' '+(Number(inv.quote.price||0)*100).toFixed(1)+'c';
    }
    const lockedPnl=Number(paper.lockedPairPnl||0);
    set('pairRaw',pairRaw==null?'—':pairRaw.toFixed(4));
    set('pairCost',pairCost==null?'—':pairCost.toFixed(4));
    set('pairEdge',pairEdge==null?'—':((pairEdge>=0?'+':'')+(pairEdge*100).toFixed(2)+'c'),pairEdge>0?'good':'bad');
    set('makerPair',invQuote);
    set('matchedSets',fmt(paper.matchedSets||0,2));
    set('lockedPairPnl',(lockedPnl>=0?'+USD ':'-USD ')+Math.abs(lockedPnl).toFixed(4),lockedPnl>=0?'good':'bad');
    set('avgPairCost',num(paper.avgMatchedSetCost)==null?'—':Number(paper.avgMatchedSetCost).toFixed(4));
    set('residualState',residual,residualWarn?'warn':'good');
    const invState=inv&&inv.status?String(inv.status):'SCANNING';
    const pairStateText=pair.directEligible?'LOCKABLE NOW':(pair.hedge&&pair.hedge.eligible?'HEDGE READY':(invState==='RESIDUAL'?'V13 RESIDUAL':invState==='QUOTING'?'V13 QUOTING':invState==='MERGED'?'V13 MERGED':'V13 SCANNING'));
    set('pairState',pairStateText,(pair.directEligible||(pair.hedge&&pair.hedge.eligible)||invState==='MERGED')?'good':invState==='RESIDUAL'?'warn':'');
    set('orbPairCost',pairCost==null?'—':pairCost.toFixed(4));
    set('orbMatched',fmt(paper.matchedSets||0,2));
    set('orbResidual',residual,residualWarn?'warn':'good');

    set('spotPrice',num(j.spot)==null?'$—':'$'+fmt(j.spot,2));
    set('mobileDockBtc',num(j.spot)==null?'—':'$'+fmt(j.spot,0));
    set('twapPrice',num(j.twap)==null?'$—':'$'+fmt(j.twap,2));
    set('openRef',num(j.openRef)==null?'WAIT NEXT OPEN':'$'+fmt(j.openRef,2));
    set('projectedTwap',num(model.projected)==null?'$—':'$'+fmt(model.projected,2));
    set('modelDistance',bps((num(model.margin)||0)*10000),(num(model.margin)||0)>=0?'good':'bad');
    set('refState',j.openTrusted?'TRUSTED OPEN':'WAITING',j.openTrusted?'good':'warn');
    set('captureState',j.openTrusted?'TRUSTED':'WAIT',j.openTrusted?'good':'warn');

    const age=Math.max(num(j.spotAgeSec)||999,num(j.twapAgeSec)||999);
    set('latency',age<900?(age*1000).toFixed(0)+' ms':'—',age<5?'good':'bad');
    set('leadBps',bps(model.leadBps));set('impulse5',bps(model.impulse5Bps));set('impulse5Mirror',bps(model.impulse5Bps));
    set('lagScore',num(model.lagScore)==null?'—':Number(model.lagScore).toFixed(2));
    set('sigma',num(model.sigma)==null?'—':(Number(model.sigma)*10000).toFixed(3)+' bp/sqrt(s)');
    set('strategyMode',j.mode||'WAIT',String(j.decision||'').startsWith('OPPORTUNITY_')?'good':'warn');set('strategyModeMirror',j.mode||'WAIT');

    const bestNet=num(c.net),fair=num(c.fair),ask=num(c.ask),fee=num(c.fee);
    set('fairUp',pct(model.fairUp));set('fairDown',pct(model.fairDown));
    set('feePerShare',fee==null?'—':'$'+fee.toFixed(4));
    set('rawEdge',num(c.raw)==null?'—':((c.raw>=0?'+':'')+(c.raw*100).toFixed(2)+'c'));
    set('netEdge',bestNet==null?'—':((bestNet>=0?'+':'')+(bestNet*100).toFixed(2)+'c'),bestNet>0?'good':'bad');
    set('bestEdge',bestNet==null?'—':((bestNet>=0?'+':'')+(bestNet*100).toFixed(2)+'c / share'),bestNet>0?'good':'bad');
    set('bestSide',c.side?c.side+' @ '+(ask*100).toFixed(1)+'c':'NO TRADE');
    if($('edgeBar'))$('edgeBar').style.width=Math.max(0,Math.min(100,(bestNet||0)*900))+'%';

    const decision=String(j.decision||'warming up'),op=decision.startsWith('OPPORTUNITY_');
    set('modelState',op?'MARKET MISTAKE':(j.mode||'WAIT'),op?'good':'warn');
    set('edgeState',op?'OPPORTUNITY':'NO TRADE',op?'good':'warn');
    set('decision',(op?'OPPORTUNITY - ':'NO TRADE - ')+(j.mode||'WAIT')+' - '+decision.replace(/^OPPORTUNITY_/,'').replaceAll('_',' '),op?'good':'warn');
    set('confidence',paper.position?.confidence!=null?Number(paper.position.confidence).toFixed(0)+'%':(num(model.reliability)!=null?(Number(model.reliability)*100).toFixed(0)+'%':'—'));
    set('edgeWhy',c.side?('ALIGNED '+c.side+' · fair '+pct(fair)+' vs ask '+pct(ask)+' · round-trip edge '+(bestNet*100).toFixed(2)+'c · lead '+Number(model.leadBps||0).toFixed(2)+' bps · 5s impulse '+Number(model.impulse5Bps||0).toFixed(2)+' bps.'):'Waiting for executable order book.');

    set('clobGate',j.feed?.clob==='live'?'LIVE':String(j.feed?.clob||'WARMING').toUpperCase(),j.feed?.clob==='live'?'good':'warn');
    set('rtdsGate',j.feed?.rtds==='live'?'LIVE':String(j.feed?.rtds||'WARMING').toUpperCase(),j.feed?.rtds==='live'?'good':'warn');

    if(!state.history.length&&Array.isArray(j.history)&&j.history.length){
      state.history=j.history.map(x=>({t:Number(x.t)*1000,spot:num(x.spot),twap:num(x.twap),open:num(x.open),fairUp:num(x.fairUp),upAsk:num(x.upAsk),downAsk:num(x.downAsk),equity:num(x.equity)})).filter(x=>x.spot);
    }
    const h={t:now*1000,spot:num(j.spot),twap:num(j.twap),open:num(j.openRef),fairUp:num(model.fairUp),upAsk:num(up.ask),downAsk:num(down.ask),pairCost:num(pair.takerPairCost),pairEdge:num(pair.lockedEdgePerShare),equity:num(paper.equity)};
    if(h.spot&&(!state.history.length||h.t-state.history[state.history.length-1].t>=450))state.history.push(h);
    state.history=state.history.filter(x=>x.t>Date.now()-180000).slice(-360);

    renderResolutionGrid();

    if(m.slug&&m.slug!==state.lastMarket){
      window.dispatchEvent(new CustomEvent('poly-edge-market-change',{detail:{previous:{slug:state.lastMarket||null},current:m,finalTwap:j.twap,previousOpenRef:j.openRef}}));
      state.lastMarket=m.slug;
    }
    if(op&&c.side){
      const key=m.slug+'|'+c.side+'|'+j.mode;
      if(key!==state.lastSignalKey||Date.now()-state.lastSignalAt>4000){
        state.lastSignalKey=key;state.lastSignalAt=Date.now();
        const item={t:new Date(),side:c.side,ask,fair,net:bestNet,state:j.mode||'OPPORTUNITY'};
        state.tape.unshift(item);state.tape=state.tape.slice(0,40);renderTape();
        window.dispatchEvent(new CustomEvent('poly-edge-opportunity',{detail:{best:c,m:model,confidence:paper.position?.confidence||0,market:m,openRef:j.openRef,openTrusted:j.openTrusted,mode:j.mode}}));
        if(state.alerts&&'Notification'in window&&Notification.permission==='granted'){
          try{new Notification('POLY/EDGE - '+j.mode+' '+c.side,{body:'Net edge '+(bestNet*100).toFixed(2)+'c/share - '+Math.round(left)+'s left',tag:key})}catch{}
        }
      }
    }
  }

  function renderTape(){
    const body=$('tapeBody'),full=$('tapeBodyFull');
    if(body)body.replaceChildren();
    if(full)full.replaceChildren();
    set('opportunityCount',String(state.tape.length)+' SIGNALS');
    if(!state.tape.length){
      if(body){const e=document.createElement('div');e.className='empty';e.textContent='No trade-quality signal yet.';body.append(e)}
      if(full){const e=document.createElement('div');e.className='empty';e.textContent='No qualified opportunity yet.';full.append(e)}
      return;
    }
    for(const x of state.tape){
      const vals=[istTape(x.t)+' IST',x.side,(x.ask*100).toFixed(1)+'c',pct(x.fair),(x.net*100).toFixed(2)+'c',x.state];
      if(body){
        const r=document.createElement('div');r.className='tapeRow';
        vals.forEach((v,i)=>{const e=document.createElement(i===5?'b':'span');e.textContent=v;if(i===4||i===5)e.className='good';r.append(e)});body.append(r);
      }
      if(full){
        const r=document.createElement('div');r.className='fullOppRow';
        vals.forEach((v,i)=>{const e=document.createElement(i===5?'b':'span');e.textContent=v;if(i===4||i===5)e.className='good';r.append(e)});full.append(r);
      }
    }
  }

  function drawChart(){
    const c=$('chart');if(!c)return;
    const r=c.getBoundingClientRect(),dpr=Math.max(1,Math.min(2,devicePixelRatio||1));
    const w=Math.max(120,r.width),h=Math.max(70,r.height);
    c.width=Math.floor(w*dpr);c.height=Math.floor(h*dpr);
    const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);
    const valid=v=>Number.isFinite(v)&&v>0,data=state.history.filter(q=>valid(q.spot)||valid(q.twap)).slice(-220);
    if(data.length<2){requestAnimationFrame(drawChart);return}
    const L=30,R=w-6,T=7,B=h-9,t0=data[0].t,t1=data[data.length-1].t||t0+1,px=t=>L+(t-t0)/Math.max(1,t1-t0)*(R-L);
    const vals=data.flatMap(q=>[q.spot,q.twap].filter(valid));let lo=Math.min(...vals),hi=Math.max(...vals),mid=(lo+hi)/2;
    const span=Math.max(4,(hi-lo)*1.35,mid*.000045);lo=mid-span/2;hi=mid+span/2;const py=p=>B-(p-lo)/Math.max(.01,hi-lo)*(B-T);
    x.font='6px ui-monospace,monospace';x.textAlign='right';x.textBaseline='middle';
    for(let i=0;i<4;i++){const yy=T+(B-T)*i/3,price=hi-(hi-lo)*i/3;x.strokeStyle='rgba(255,255,255,.05)';x.beginPath();x.moveTo(L,yy);x.lineTo(R,yy);x.stroke();x.fillStyle='rgba(135,150,143,.55)';x.fillText(price.toFixed(0),L-3,yy)}
    const line=(key,color,width)=>{const arr=data.filter(q=>valid(q[key]));if(arr.length<2)return;x.beginPath();arr.forEach((q,i)=>i?x.lineTo(px(q.t),py(q[key])):x.moveTo(px(q.t),py(q[key])));x.strokeStyle=color;x.lineWidth=width;x.lineJoin='round';x.stroke()};
    line('spot','#67f3a2',1.35);line('twap','#65d5ff',1.0);
    const open=[...data].reverse().map(q=>q.open).find(valid);if(valid(open)&&open>=lo&&open<=hi){x.setLineDash([3,3]);x.strokeStyle='rgba(255,196,90,.7)';x.beginPath();x.moveTo(L,py(open));x.lineTo(R,py(open));x.stroke();x.setLineDash([])}
    const last=data[data.length-1];if(valid(last.spot)){x.fillStyle='#67f3a2';x.beginPath();x.arc(px(last.t),py(last.spot),2.5,0,Math.PI*2);x.fill()}
    requestAnimationFrame(drawChart);
  }
  function drawOrb(){
    window.PolyNetworkOrb?.mount($('orbCanvas'));
    const update=()=>{
      const snap=state.snap||{},m=snap.model||{},best=snap.candidate||{},pair=snap.pair||{};
      window.PolyNetworkOrb?.setTelemetry(snap,state.history);
      set('orbMode',pair.directEligible?'PAIR LOCK':state.latestMode||'WAIT');
      set('orbSide',best.side||'—');
      set('orbEdge',num(best.net)==null?'—':((best.net>=0?'+':'')+(best.net*100).toFixed(2)+'c'));
      set('orbLead',num(m.leadBps)==null?'LEAD —':'LEAD '+bps(m.leadBps));
    };
    update();setInterval(update,1000);
  }
  function renderResolutionGrid(){
    const root=$('resolutionGrid');if(!root)return;
    const xs=state.history.slice(-12);root.replaceChildren();
    if(!xs.length){const e=document.createElement('div');e.className='empty';e.textContent='Building live window...';root.append(e);return}
    xs.forEach((v,i)=>{
      const p=Number(v.fairUp),cell=document.createElement('div');cell.className='resolutionCell';
      const side=p>=.5?'UP':'DOWN',strength=Math.abs(p-.5)*2;
      cell.style.setProperty('--heat',Math.max(.08,Math.min(.85,strength)).toFixed(2));cell.dataset.side=side;
      const a=document.createElement('span');a.textContent=(i===xs.length-1?'NOW':'-'+(xs.length-1-i));
      const b=document.createElement('b');b.textContent=Number.isFinite(p)?Math.round(p*100)+'%':'—';
      const c=document.createElement('small');c.textContent=side;cell.append(a,b,c);root.append(cell);
    });
  }

  function drawLifecycle(){
    const c=$('lifecycleCanvas');if(!c){requestAnimationFrame(drawLifecycle);return}
    const r=c.getBoundingClientRect(),dpr=Math.max(1,Math.min(2,devicePixelRatio||1)),w=Math.max(120,r.width),h=Math.max(70,r.height);
    c.width=Math.floor(w*dpr);c.height=Math.floor(h*dpr);const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);
    const snap=state.snap||{},m=snap.market||{},now=Number(snap.serverTime)||Date.now()/1000,start=Number(m.startTs)||now,end=Number(m.endTs)||now+300;
    const L=34,R=w-18,Y=h*.52,px=v=>L+(v-start)/Math.max(1,end-start)*(R-L);
    const bands=[[0,15,'OPEN'],[15,225,'LAG'],[225,296,'RESOLUTION'],[296,300,'CLOSE']];
    bands.forEach((b,i)=>{const x0=px(start+b[0]),x1=px(start+b[1]);x.fillStyle=i===1?'rgba(108,255,169,.06)':i===2?'rgba(255,200,87,.06)':'rgba(255,255,255,.025)';x.fillRect(x0,28,x1-x0,h-56);x.fillStyle='rgba(130,150,140,.68)';x.font='8px ui-monospace,monospace';x.textAlign='center';x.fillText(b[2],(x0+x1)/2,20)});
    x.strokeStyle='rgba(255,255,255,.16)';x.beginPath();x.moveTo(L,Y);x.lineTo(R,Y);x.stroke();
    const events=(snap.paper?.events||[]).filter(e=>Number(e.t)>=start&&Number(e.t)<=end).slice(0,80);
    events.forEach((e,i)=>{const xx=px(Number(e.t)),act=String(e.action||''),good=/BUY|PAIR_MERGE|PAIR_HEDGE_MERGE/.test(act),bad=/NOFILL|BLOCK|SELL/.test(act);x.fillStyle=good?'#6cffa9':bad?'#ff6076':'#63d7ff';x.beginPath();x.arc(xx,Y+(i%2?12:-12),3.2,0,Math.PI*2);x.fill()});
    const nx=Math.max(L,Math.min(R,px(now)));x.strokeStyle='#ffc857';x.lineWidth=2;x.beginPath();x.moveTo(nx,25);x.lineTo(nx,h-22);x.stroke();x.fillStyle='#ffc857';x.textAlign='center';x.fillText(Math.max(0,Math.round(end-now))+'s',nx,h-9);
    requestAnimationFrame(drawLifecycle);
  }

  function drawEdgeMatrix(){
    const c=$('edgeMatrixCanvas');if(!c){requestAnimationFrame(drawEdgeMatrix);return}
    const r=c.getBoundingClientRect(),dpr=Math.max(1,Math.min(2,devicePixelRatio||1)),w=Math.max(120,r.width),h=Math.max(80,r.height);
    c.width=Math.floor(w*dpr);c.height=Math.floor(h*dpr);const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);
    const data=state.history.slice(-180).map(q=>({lead:(q.spot&&q.twap)?(q.spot/q.twap-1)*10000:null,edge:(Number.isFinite(q.fairUp)&&Number.isFinite(q.upAsk))?(q.fairUp-q.upAsk)*100:null})).filter(q=>Number.isFinite(q.lead)&&Number.isFinite(q.edge));
    const L=25,R=w-8,T=8,B=h-17,absX=Math.max(.45,...data.map(q=>Math.abs(q.lead))),absY=Math.max(1.5,...data.map(q=>Math.abs(q.edge)));
    const px=v=>L+(v+absX)/(2*absX)*(R-L),py=v=>B-(v+absY)/(2*absY)*(B-T);
    // diagonal decision zones
    x.fillStyle='rgba(103,243,162,.055)';x.beginPath();x.moveTo(L,T);x.lineTo(R,T);x.lineTo(R,B);x.closePath();x.fill();
    x.fillStyle='rgba(255,102,122,.055)';x.beginPath();x.moveTo(L,T);x.lineTo(L,B);x.lineTo(R,B);x.closePath();x.fill();
    x.strokeStyle='rgba(255,255,255,.10)';x.beginPath();x.moveTo(L,B);x.lineTo(R,T);x.stroke();
    x.strokeStyle='rgba(255,255,255,.07)';x.beginPath();x.moveTo(px(0),T);x.lineTo(px(0),B);x.moveTo(L,py(0));x.lineTo(R,py(0));x.stroke();
    data.forEach((q,i)=>{const good=q.edge>0&&Math.sign(q.edge)===Math.sign(q.lead||1);x.fillStyle=good?'rgba(103,243,162,.60)':'rgba(255,102,122,.54)';x.beginPath();x.arc(px(q.lead),py(q.edge),i===data.length-1?3.2:1.35,0,Math.PI*2);x.fill()});
    x.fillStyle='rgba(132,149,140,.72)';x.font='6px ui-monospace,monospace';x.textAlign='left';x.fillText('LEAD BPS →',L,B+11);x.fillText('EDGE',L,T+6);
    requestAnimationFrame(drawEdgeMatrix);
  }
  function drawHedgeFlow(){
    const c=$('hedgeFlowCanvas');if(!c){requestAnimationFrame(drawHedgeFlow);return}
    const r=c.getBoundingClientRect(),dpr=Math.max(1,Math.min(2,devicePixelRatio||1)),w=Math.max(120,r.width),h=Math.max(80,r.height);
    c.width=Math.floor(w*dpr);c.height=Math.floor(h*dpr);const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);x.clearRect(0,0,w,h);
    const p=state.snap?.paper||{},pos=p.position||null,t=performance.now()/1000;
    const N={cash:[w*.07,h*.50],up:[w*.27,h*.27],down:[w*.27,h*.73],match:[w*.57,h*.35],res:[w*.57,h*.72],merge:[w*.88,h*.35]};
    const curve=(a,b,q)=>{const A=N[a],B=N[b],cx=(A[0]+B[0])/2,u=1-q;return [u*u*u*A[0]+3*u*u*q*cx+3*u*q*q*cx+q*q*q*B[0],u*u*u*A[1]+3*u*u*q*A[1]+3*u*q*q*B[1]+q*q*q*B[1]]};
    const stream=(a,b,width,color,dotColor,count)=>{
      const A=N[a],B=N[b],cx=(A[0]+B[0])/2;x.strokeStyle=color;x.lineWidth=width;x.beginPath();x.moveTo(A[0],A[1]);x.bezierCurveTo(cx,A[1],cx,B[1],B[0],B[1]);x.stroke();
      for(let i=0;i<count;i++){const q=(t*(.12+i*.008)+i/count)%1,P=curve(a,b,q);x.fillStyle=dotColor;x.beginPath();x.arc(P[0],P[1],1.2+(i%3)*.35,0,Math.PI*2);x.fill()}
    };
    const matched=Math.max(0,Number(p.matchedSets)||0),res=Math.max(0,Number(pos?.shares)||0),base=1.4+Math.min(9,matched*.42);
    stream('cash','up',2.0,'rgba(103,243,162,.22)','#67f3a2',5);
    stream('cash','down',2.0,'rgba(255,102,122,.20)','#ff667a',5);
    stream('up','match',base,'rgba(103,243,162,.34)','#67f3a2',7);
    stream('down','match',base,'rgba(101,213,255,.30)','#65d5ff',7);
    stream('match','merge',base+1.8,'rgba(255,196,90,.38)','#ffc45a',8);
    if(res>0)stream(pos.side==='UP'?'up':'down','res',3+Math.min(6,res),'rgba(255,196,90,.48)','#ffc45a',6);
    const node=(k,label,val)=>{const A=N[k],ww=Math.max(42,w*.095),hh=Math.max(22,h*.19);x.fillStyle='rgba(8,12,10,.96)';x.strokeStyle='rgba(255,255,255,.12)';x.fillRect(A[0]-ww/2,A[1]-hh/2,ww,hh);x.strokeRect(A[0]-ww/2,A[1]-hh/2,ww,hh);x.fillStyle='#899990';x.font='5px ui-monospace,monospace';x.textAlign='center';x.fillText(label,A[0],A[1]-2);x.fillStyle='#e6eee9';x.font='6px ui-monospace,monospace';x.fillText(val,A[0],A[1]+7)};
    node('cash','CASH','$'+Number(p.cash||0).toFixed(2));node('up','UP',pos?.side==='UP'?fmt(res,1):'—');node('down','DOWN',pos?.side==='DOWN'?fmt(res,1):'—');node('match','MATCHED',fmt(matched,1));node('res','RESIDUAL',res?fmt(res,1):'0');node('merge','$1 MERGE',(Number(p.lockedPairPnl||0)>=0?'+':'')+Number(p.lockedPairPnl||0).toFixed(2));
    requestAnimationFrame(drawHedgeFlow);
  }
  let pollTimer=null,pollBusy=false,pollDelay=1000;
  function schedulePoll(delay=pollDelay){
    clearTimeout(pollTimer);
    pollTimer=setTimeout(poll,document.hidden?Math.max(1800,delay):delay);
  }
  async function poll(){
    if(pollBusy){schedulePoll();return}
    pollBusy=true;
    const started=performance.now();
    try{
      const r=await fetch(POLY_ENGINE_BASE+'/api/poly/demo/server?asset='+encodeURIComponent(state.asset),{cache:'no-store',mode:'cors'});
      let j=null;try{j=await r.json()}catch{}
      if(r.status===429){
        const retry=Number(r.headers.get('Retry-After'))||5;
        pollDelay=Math.max(2500,retry*1000);
        set('feedStatus','RATE LIMIT BACKOFF','warn');
        set('decision','PAUSED POLLING - server rate-limit backoff '+Math.ceil(pollDelay/1000)+'s','warn');
        window.dispatchEvent(new CustomEvent('poly-edge-server-error',{detail:{status:429,message:j?.detail||'Rate limit reached',retryMs:pollDelay,t:Date.now()}}));
        return;
      }
      if(!r.ok)throw new Error(j?.detail||('HTTP '+r.status));
      if((j.asset||'BTC')!==state.asset)return;
      pollDelay=1000;
      render(j);
      window.POLY_EDGE_LATEST_SNAPSHOT=j;
      window.dispatchEvent(new CustomEvent('poly-edge-server-snapshot',{detail:j}));
      set('serverPing',Math.round(performance.now()-started)+' ms');
    }catch(e){
      pollDelay=Math.min(5000,Math.max(1500,pollDelay*1.5));
      set('feedStatus','SERVER RETRY','bad');set('decision','SERVER RETRY - '+e.message,'bad');
      window.dispatchEvent(new CustomEvent('poly-edge-server-error',{detail:{status:0,message:String(e.message||e),retryMs:pollDelay,t:Date.now()}}));
    }finally{
      pollBusy=false;
      schedulePoll();
    }
  }

  async function alerts(){
    if(!('Notification'in window)){set('alertsBtn','ALERTS UNSUPPORTED');return}
    const p=await Notification.requestPermission();state.alerts=p==='granted';set('alertsBtn',state.alerts?'ALERTS ENABLED':'ALERTS '+p.toUpperCase());
  }

  function initMobileDock(){
    const dock=document.querySelector('.mobileDock');if(!dock)return;
    const links=[...dock.querySelectorAll('a[href^="#"]')];
    for(const a of links)a.addEventListener('click',()=>{for(const x of links)x.classList.remove('active');a.classList.add('active')});
    if(!('IntersectionObserver' in window))return;
    const sections=links.map(a=>document.querySelector(a.getAttribute('href'))).filter(Boolean);
    const obs=new IntersectionObserver(entries=>{
      const visible=entries.filter(e=>e.isIntersecting).sort((a,b)=>b.intersectionRatio-a.intersectionRatio)[0];
      if(!visible)return;
      for(const a of links)a.classList.toggle('active',a.getAttribute('href')==='#'+visible.target.id);
    },{rootMargin:'-20% 0px -62% 0px',threshold:[.01,.15,.35]});
    for(const el of sections)obs.observe(el);
  }

  function boot(){
    initMobileDock();
    for(const button of document.querySelectorAll('[data-asset]'))button.addEventListener('click',()=>selectAsset(button.dataset.asset));
    if($('alertsBtn'))$('alertsBtn').onclick=alerts;
    poll();requestAnimationFrame(drawChart);requestAnimationFrame(drawOrb);requestAnimationFrame(drawLifecycle);requestAnimationFrame(drawEdgeMatrix);requestAnimationFrame(drawHedgeFlow);
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})();