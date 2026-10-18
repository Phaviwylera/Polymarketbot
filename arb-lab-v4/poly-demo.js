'use strict';
(() => {
  const $=id=>document.getElementById(id);
  let last=null;

  function set(id,val,cls=''){const e=$(id);if(!e)return;e.textContent=val;if(cls)e.className=cls}
  function money(n,d=4){n=Number(n)||0;return (n>=0?'+$':'-$')+Math.abs(n).toFixed(d)}
  function pct(n,d=1){return (Number(n)||0).toFixed(d)+'%'}
  const istTime=ts=>new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(Number(ts)*1000))+' IST';
  function feedText(feed){
    if(!feed)return 'WARMING';
    return 'MARKET '+String(feed.market||'—').toUpperCase()+' · CLOB '+String(feed.clob||'—').toUpperCase()+' · RTDS '+String(feed.rtds||'—').toUpperCase();
  }

  function renderReasons(reasons){
    const box=$('demoReasons');if(!box)return;box.replaceChildren();
    const entries=Object.entries(reasons||{}).sort((a,b)=>b[1]-a[1]).slice(0,16);
    if(!entries.length){
      const d=document.createElement('div');const s=document.createElement('span');s.textContent='AUDIT';const b=document.createElement('b');b.textContent='Collecting gate decisions…';d.append(s,b);box.append(d);return;
    }
    for(const [reason,count] of entries){
      const d=document.createElement('div'),s=document.createElement('span'),b=document.createElement('b');
      s.textContent=reason.toUpperCase();b.textContent=String(count)+' checks';d.append(s,b);box.append(d);
    }
  }

  function renderEvents(events){
    const body=$('demoTape');if(!body)return;body.replaceChildren();
    set('historyCount',String(Array.isArray(events)?events.length:0)+' EVENTS');
    if(!Array.isArray(events)||!events.length){
      const e=document.createElement('div');e.className='empty';e.textContent='No paper order yet. Decision audit above shows why.';body.append(e);return;
    }
    for(const x of events.slice(0,200)){
      const row=document.createElement('div');row.className='demoRow';
      const vals=[
        istTime(x.t),
        x.action||'—',x.side||'—',
        Number.isFinite(Number(x.price))?(Number(x.price)*100).toFixed(1)+'¢':'—',
        Number.isFinite(Number(x.pnl))?(Number(x.pnl)>=0?'+$':'-$')+Math.abs(Number(x.pnl)).toFixed(4):'—',
        x.detail||''
      ];
      vals.forEach((v,i)=>{
        const e=document.createElement(i===1||i===4?'b':'span');e.textContent=v;
        if(i===1)e.className=x.action==='BUY'?'buy':x.action==='SELL'?'sell':'';
        if(i===4&&Number.isFinite(Number(x.pnl)))e.className=Number(x.pnl)>=0?'win':'loss';
        row.append(e);
      });
      body.append(row);
    }
  }

  function render(j){
    last=j;
    const p=j.paper||{},eq=Number(p.equity)||0,start=Number(p.startingBalance)||20,real=Number(p.realizedPnl)||0;
    const total=Number.isFinite(Number(p.totalPnl))?Number(p.totalPnl):(eq-start);
    const unreal=Number.isFinite(Number(p.unrealizedPnl))?Number(p.unrealizedPnl):(total-real);
    set('demoEquity','$'+eq.toFixed(4),eq>=start?'good':'bad');
    set('mobileDockEquity','$'+eq.toFixed(2),eq>=start?'good':'bad');
    set('demoPnl',money(real,4),real>=0?'good':'bad');
    set('demoTotalPnl',money(total,4),total>=0?'good':'bad');
    set('demoUnrealized',money(unreal,4),unreal>=0?'good':'bad');
    set('demoTrades',String(p.trades||0));
    set('demoWins',String(p.wins||0));set('demoLosses',String(p.losses||0));
    set('demoWinRate',p.trades?pct(p.winRatePct||0,1):'—');
    set('demoFees','$'+Number(p.fees||0).toFixed(4));
    set('demoDrawdown',pct((Number(p.maxDrawdown)||0)*100,2));
    set('demoBlocked',String(p.blocked||0));
    set('demoNoFill',String(p.nofills||0));
    set('demoAttempts',String(p.executionAttempts||0));
    set('demoPostRejects',String(p.postLatencyRejects||0));
    set('pairAttemptsView',String(Number(p.pairAttempts||0)+Number(p.inventoryQuotes||0)));
    set('matchedSetsHistory',Number(p.matchedSets||0).toFixed(2));
    set('lockedPairPnlHistory',money(p.lockedPairPnl||0,4),Number(p.lockedPairPnl||0)>=0?'good':'bad');
    set('avgPairCostHistory',Number.isFinite(Number(p.avgMatchedSetCost))?Number(p.avgMatchedSetCost).toFixed(4):'—');
    const left=Math.max(0,Math.floor((Number(p.endsAt)||0)-Number(j.serverTime||Date.now()/1000)));
    set('demoLeft',String(Math.floor(left/3600)).padStart(2,'0')+':'+String(Math.floor((left%3600)/60)).padStart(2,'0')+':'+String(left%60).padStart(2,'0'));
    const status=p.status==='complete'?'COMPLETE':p.status==='risk_locked'?'RISK LOCKED':'SERVER LIVE PAPER';
    set('demoStatus',status,p.status==='risk_locked'?'bad':p.status==='complete'?'warn':'good');
    const riskText=p.status==='risk_locked'?(p.riskLockReason||'LOSS LIMIT'):(Number(p.cooldownUntil||0)>Number(j.serverTime||0)?'COOLDOWN':'ARMED');
    set('riskState',riskText,p.status==='risk_locked'?'bad':riskText==='COOLDOWN'?'warn':'good');

    if(p.position){
      const side=p.position.side||'—',key=side==='UP'?'up':'down';
      const bid=Number(j.bbo?.[key]?.bid);
      const txt=side+' · '+Number(p.position.shares||0).toFixed(2)+' shares · entry '+(Number(p.position.entryPrice||0)*100).toFixed(1)+'¢ · live bid '+(Number.isFinite(bid)?(bid*100).toFixed(1)+'¢':'—');
      set('demoPosition',txt,'warn');set('positionForensic',txt);
    }else{
      const inv=p.inventory||j.pair?.inventory;
      if(inv){
        const upLots=Array.isArray(inv.upLots)?inv.upLots:[],dnLots=Array.isArray(inv.downLots)?inv.downLots:[];
        const calc=lots=>lots.reduce((a,x)=>({sh:a.sh+Number(x.shares||0),cost:a.cost+Number(x.cost||0)}),{sh:0,cost:0});
        const up=calc(upLots),dn=calc(dnLots);
        if(up.sh>dn.sh+1e-9||dn.sh>up.sh+1e-9){
          const side=up.sh>dn.sh?'UP':'DOWN',obj=side==='UP'?up:dn,key=side.toLowerCase(),bid=Number(j.bbo?.[key]?.bid);
          const shares=Math.abs(up.sh-dn.sh),unit=obj.sh?obj.cost/obj.sh:0;
          const txt='V13 RESIDUAL '+side+' · '+shares.toFixed(2)+' shares · avg '+(unit*100).toFixed(1)+'¢ · live bid '+(Number.isFinite(bid)?(bid*100).toFixed(1)+'¢':'—')+' · unrealized '+money(p.unrealizedPnl||0,4);
          set('demoPosition',txt,'warn');set('positionForensic',txt);
        }else if(inv.quote){
          const txt='V13 '+String(inv.quote.kind||'').toUpperCase()+' QUOTE · '+String(inv.quote.side||'').toUpperCase()+' '+(Number(inv.quote.price||0)*100).toFixed(1)+'¢ · '+Number(inv.quote.shares||0).toFixed(2)+' shares';
          set('demoPosition',txt);set('positionForensic',txt);
        }else{
          const txt='V13 INVENTORY IDLE · cycles '+Number(inv.cycles||0)+' / 3';
          set('demoPosition',txt,'good');set('positionForensic',txt);
        }
      }else{set('demoPosition','NO OPEN PAPER POSITION');set('positionForensic','NO OPEN PAPER POSITION');}
    }

    set('demoWhy',(j.mode?j.mode+' · ':'')+(j.decision||'warming up'),String(j.decision||'').startsWith('OPPORTUNITY_')?'good':j.mode==='RISK_LOCK'?'bad':'warn');
    if(j.candidate){
      set('demoBest',(j.candidate.side||'—')+' ask '+(Number(j.candidate.ask||0)*100).toFixed(1)+'¢ · fair '+(Number(j.candidate.fair||0)*100).toFixed(1)+'% · net '+(Number(j.candidate.net||0)*100).toFixed(2)+'¢');
    }else set('demoBest','—');
    set('demoFeed',feedText(j.feed),(j.feed?.market==='live'&&j.feed?.clob==='live'&&j.feed?.rtds==='live')?'good':'warn');

    renderReasons(j.reasonCounts);
    renderEvents(p.events);
  }

  function acceptSnapshot(j){
    if(j&&typeof j==='object')render(j);
  }

  window.addEventListener('poly-edge-server-snapshot',e=>acceptSnapshot(e.detail));
  window.addEventListener('poly-edge-server-error',e=>{
    const d=e.detail||{};
    if(d.status===429){set('demoStatus','RATE LIMIT BACKOFF','warn');set('demoWhy','Shared feed backing off '+Math.ceil((d.retryMs||0)/1000)+'s','warn')}
  });

  function boot(){
    const snap=window.POLY_EDGE_STATE?.snap;if(snap)acceptSnapshot(snap);
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot,{once:true});else boot();
})();