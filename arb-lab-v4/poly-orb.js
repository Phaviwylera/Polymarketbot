'use strict';
/* Signal-driven network orb. Every visual cue maps to live engine state:
   packet energy = oracle impulse + calibrated mispricing, region glow = conservative
   edge after fees, pulsing seed rings = a detected mispricing, bursts = paper fills.
   It is a telemetry visualization, never a visualization of real order routing. */
(() => {
 const TAU=2*Math.PI,N=192,LOOP=60000,ASSETS=['BTC','ETH','BTC15','ETH15'];
 const LABEL={BTC:'BTC 5m',ETH:'ETH 5m',BTC15:'BTC 15m',ETH15:'ETH 15m'};
 const colors=['#ffc45a','#65d5ff','#bdabff','#67f3a2'];
 const nodes=Array.from({length:N},(_,i)=>{const y=1-2*(i+.5)/N,r=Math.sqrt(1-y*y),a=i*Math.PI*(3-Math.sqrt(5));return {x:Math.cos(a)*r,y,z:Math.sin(a)*r};});
 const links=[],neighbors=nodes.map(()=>[]),used=new Set();
 nodes.forEach((a,i)=>nodes.map((b,j)=>({j,d:(a.x-b.x)**2+(a.y-b.y)**2+(a.z-b.z)**2})).filter(x=>x.j!==i).sort((a,b)=>a.d-b.d).slice(0,4).forEach(({j})=>{const key=[Math.min(i,j),Math.max(i,j)].join(':');if(!used.has(key)){used.add(key);links.push([i,j]);neighbors[i].push(j);neighbors[j].push(i);}}));
 const seeds=[38,82,126,170],packets=[],seen={},counts=[0,0,0,0];
 let snap={},clock=0,sequence=0,emitting=true;
 const sig=ASSETS.map(()=>({glow:0,target:0,edge:null,p:null,side:null,mispriced:false,position:null,trades:null,flash:0,smart:0}));
 const finite=v=>v!==null&&v!==undefined&&Number.isFinite(Number(v));
 const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
 const cubic=(a,b,c,d,t)=>{const u=1-t;return u*u*u*a+3*u*u*t*b+3*u*t*t*c+t*t*t*d;};
 function project(p,phase){const a=phase*TAU,c=Math.cos(a),s=Math.sin(a),x=p.x*c+p.z*s,z=-p.x*s+p.z*c,tilt=-.22,ct=Math.cos(tilt),st=Math.sin(tilt),y=p.y*ct-z*st,depth=p.y*st+z*ct,scale=3.4/(3.4-depth*.28);return{x:x*scale,y:y*scale,z:depth,scale};}
 function arc(a,b,t){const x=a.x*(1-t)+b.x*t,y=a.y*(1-t)+b.y*t,z=a.z*(1-t)+b.z*t,r=Math.hypot(x,y,z),radius=Math.hypot(a.x,a.y,a.z)*(1-t)+Math.hypot(b.x,b.y,b.z)*t;return {x:x/r*radius,y:y/r*radius,z:z/r*radius};}
 function fresh(a){return !!a&&finite(a.spotAgeSec)&&finite(a.twapAgeSec)&&Math.max(a.spotAgeSec,a.twapAgeSec)<5&&['market','clob','rtds'].every(k=>a.feed?.[k]==='live');}
 function route(asset,id){const result=[seeds[asset]];let previous=-1;for(let k=0;k<7;k++){const current=result.at(-1),options=neighbors[current].filter(n=>n!==previous);const next=options[(id+k*3+asset)%options.length];result.push(next);previous=current;}return result;}
 function burst(i,n,energy){if(!emitting||document.hidden)return;for(let k=0;k<n&&packets.length<96;k++)packets.push({asset:i,born:clock+k*.08,route:route(i,sequence++),energy,sourceAt:0,fill:true});}
 function readSignals(){
  ASSETS.forEach((asset,i)=>{const a=snap.assets?.[asset],st=sig[i],es=a?.strategies?.edgeSignal||snap.edge?.signals?.[asset]||{},best=es.best||{};
   const ce=Number(best.conservativeEdge);st.edge=finite(ce)?ce:null;st.p=finite(es.pUp)?Number(es.pUp):null;st.side=best.side||null;
   st.mispriced=es.decision==='MISPRICED';st.smart=Number(best.smartFlow||0);
   st.target=st.mispriced?1:st.edge===null?0:clamp((st.edge+.06)/.06,0,.55);
   const pos=a?.paper?.position||null,trades=Number(a?.paper?.trades||0);
   if(pos&&!st.position){burst(i,6,1);st.flash=1;}
   if(st.trades!==null&&trades>st.trades){burst(i,10,1);st.flash=1;}
   st.position=pos?(pos.mode||'OPEN'):null;st.trades=trades;
  });
 }
 function setTelemetry(value){
  snap=value||{};readSignals();
  ASSETS.forEach((asset,i)=>{const a=snap.assets?.[asset];if(!fresh(a))return;
   const observed=a.bookHealth?.observedAt||{},stamp=Math.max(...Object.values(observed).map(Number).filter(Number.isFinite),0);
   // A repeated HTTP snapshot is not a new market observation.
   if(!stamp||stamp<=Number(seen[asset]||0))return;seen[asset]=stamp;
   const st=sig[i],intensity=clamp(Math.abs(Number(a.model?.impulse5Bps||0))/3+st.glow*.6,.2,1);
   if(emitting&&!document.hidden&&packets.length<64){packets.push({asset:i,born:clock,route:route(i,sequence++),energy:intensity,sourceAt:stamp});counts[i]++;}
  });
 }
 function frame(ctx,w,h,phase,data){
  const state=data?.snap||snap,time=clock,cx=w*.70,cy=h*.50,R=Math.min(w*.255,h*.38),compact=w<640;
  ctx.clearRect(0,0,w,h);ctx.fillStyle='#080d0e';ctx.fillRect(0,0,w,h);
  const glow=ctx.createRadialGradient(cx,cy,0,cx,cy,R*1.5);glow.addColorStop(0,'rgba(101,213,255,.065)');glow.addColorStop(1,'rgba(8,13,14,0)');ctx.fillStyle=glow;ctx.fillRect(0,0,w,h);
  sig.forEach(st=>{st.glow+=(st.target-st.glow)*.06;st.flash*=.965;});
  const energy=Array(N).fill(0),active=packets.filter(p=>time-p.born<9&&time>=p.born);
  for(const p of active){const age=time-p.born-1.4;if(age<0)continue;for(let j=0;j<p.route.length;j++){const dt=age-j*.70;if(dt>=0&&dt<2.2)energy[p.route[j]]+=p.energy*Math.exp(-dt*2.4);}}
  const geometry=nodes.map((p,i)=>{const r=1+.018*Math.min(1,energy[i]);return{x:p.x*r,y:p.y*r,z:p.z*r};});
  const points=geometry.map(p=>project(p,phase)),screen=p=>({x:cx+p.x*R,y:cy+p.y*R,z:p.z});
  const screenPoints=points.map(screen),left=compact?10:20,cardW=w*(compact?.22:.20),step=h*.19,top=h*.13,cardH=step*.72;
  const paths=ASSETS.map((asset,i)=>{const start={x:left+cardW,y:top+i*step+cardH*.5},end=screenPoints[seeds[i]];return {start,end,c1:{x:w*.37,y:start.y},c2:{x:cx-R*1.25,y:end.y}};});
  const lanePoint=(i,t)=>{const {start,end,c1,c2}=paths[i];return{x:cubic(start.x,c1.x,c2.x,end.x,t),y:cubic(start.y,c1.y,c2.y,end.y,t)};};
  ctx.font='500 '+(compact?9:11)+'px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';ctx.fillStyle='#879b9f';ctx.fillText('MARKET INPUTS',left,top-14);
  ASSETS.forEach((asset,i)=>{const a=state.assets?.[asset],live=fresh(a),path=paths[i],y=top+i*step;
   ctx.fillStyle='#0a1114';ctx.strokeStyle=live?colors[i]+'45':'#233136';ctx.lineWidth=1;ctx.beginPath();ctx.roundRect(left,y,cardW,cardH,9);ctx.fill();ctx.stroke();
   ctx.fillStyle=live?colors[i]:'#879b9f';ctx.font='600 '+(compact?11:13)+'px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';ctx.fillText(LABEL[asset]||asset,left+10,y+cardH*.38);
   ctx.fillStyle='#e4edef';ctx.font=(compact?10:14)+'px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
   const price=finite(a?.spot)?'$'+Number(a.spot).toLocaleString('en-US',{maximumFractionDigits:2}):'Awaiting feed';ctx.fillText(price,left+10,y+cardH*.74);
   const st=sig[i];
   if(!compact){ctx.textAlign='right';ctx.font='500 9px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';ctx.fillStyle=st.position?'#ffc45a':live?'#67f3a2':'#879b9f';ctx.fillText(st.position?(st.position==='EDGE_HOLD'?'HOLDING':'IN TRADE'):live?'LIVE':'WAIT',left+cardW-9,y+cardH*.38);
    if(st.p!==null){ctx.font='500 9px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';ctx.fillStyle=st.mispriced?'#67f3a2':'#879b9f';
     const e=st.edge===null?'':' · '+(st.edge>=0?'+':'−')+Math.abs(st.edge*100).toFixed(1)+'¢';ctx.fillText('P↑ '+Math.round(st.p*100)+'%'+e,left+cardW-9,y+cardH*.74);}
    ctx.textAlign='left';}
   if(st.glow>.05){ctx.strokeStyle=colors[i];ctx.globalAlpha=.25+.6*st.glow;ctx.lineWidth=1.4;ctx.beginPath();ctx.roundRect(left-1,y-1,cardW+2,cardH+2,10);ctx.stroke();ctx.globalAlpha=1;}
   ctx.strokeStyle=live?colors[i]+'50':'#233136';ctx.lineWidth=.8;ctx.beginPath();ctx.moveTo(path.start.x,path.start.y);ctx.bezierCurveTo(path.c1.x,path.c1.y,path.c2.x,path.c2.y,path.end.x,path.end.y);ctx.stroke();
  });
  // Great-circle edges and travelling packets share arc(), including perspective.
  const edges=links.map(([a,b])=>({a,b,z:(points[a].z+points[b].z)/2})).sort((a,b)=>a.z-b.z);
  for(const edge of edges){const {a,b,z}=edge,front=clamp((z+1)/2,0,1),power=Math.min(1,energy[a]+energy[b]);ctx.globalAlpha=.10+.28*front+.35*power;ctx.strokeStyle=colors[Math.floor(a/N*4)];ctx.lineWidth=.55+front*.45+power*.5;ctx.beginPath();for(let k=0;k<=6;k++){const p=screen(project(arc(geometry[a],geometry[b],k/6),phase));k?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y);}ctx.stroke();}
  ctx.globalAlpha=1;
  // No independent/random signal dots. Every luminous packet has one source and route.
  for(const p of active){const age=time-p.born;let pos,depth=1;
   if(age<1.4){pos=lanePoint(p.asset,clamp(age/1.4,0,1));}
   else {const progress=(age-1.4)/.70,j=Math.floor(progress);if(j>=p.route.length-1)continue;const u=progress-j,point=project(arc(geometry[p.route[j]],geometry[p.route[j+1]],u),phase);pos=screen(point);depth=point.z;}
   const alpha=.4+.6*clamp((depth+1)/2,0,1);ctx.fillStyle=colors[p.asset];ctx.globalAlpha=alpha*.12;ctx.beginPath();ctx.arc(pos.x,pos.y,8+p.energy*3,0,TAU);ctx.fill();ctx.globalAlpha=alpha;ctx.beginPath();ctx.arc(pos.x,pos.y,2+p.energy,0,TAU);ctx.fill();ctx.fillStyle='#e4edef';ctx.beginPath();ctx.arc(pos.x,pos.y,.8,0,TAU);ctx.fill();
  }
  ctx.globalAlpha=1;
  screenPoints.map((p,i)=>({...p,i})).sort((a,b)=>a.z-b.z).forEach(p=>{const region=sig[Math.floor(p.i/N*4)],front=clamp((p.z+1)/2,0,1),power=Math.min(1,energy[p.i]+region.glow*.45+region.flash*.5),radius=1+front*1.1+power*1.2;ctx.fillStyle=colors[Math.floor(p.i/N*4)];ctx.globalAlpha=.3+.7*front;if(power>.03){ctx.globalAlpha=.13*power;ctx.beginPath();ctx.arc(p.x,p.y,radius*3,0,TAU);ctx.fill();ctx.globalAlpha=.55+.45*front;}ctx.beginPath();ctx.arc(p.x,p.y,radius,0,TAU);ctx.fill();});
  // The four feed endpoints stay pinned to the same nodes throughout rotation.
  seeds.forEach((index,i)=>{const p=screenPoints[index],st=sig[i];ctx.globalAlpha=.75;ctx.strokeStyle=colors[i];ctx.lineWidth=1;ctx.beginPath();ctx.arc(p.x,p.y,4.5+energy[index]*3,0,TAU);ctx.stroke();
   if(st.mispriced){const pulse=(time*1.6+i*.25)%1;ctx.globalAlpha=.6*(1-pulse);ctx.lineWidth=1.6;ctx.beginPath();ctx.arc(p.x,p.y,6+pulse*R*.22,0,TAU);ctx.stroke();}
   if(st.flash>.05){ctx.globalAlpha=.35*st.flash;ctx.fillStyle=colors[i];ctx.beginPath();ctx.arc(p.x,p.y,10+20*(1-st.flash),0,TAU);ctx.fill();}});ctx.globalAlpha=1;
  const edge=state.edge||{},promoted=edge.promotedStrategy,es=edge.strategies?.edge,cal=edge.calibration||{};
  ctx.textAlign='center';ctx.font='600 '+(compact?10:12)+'px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';ctx.globalAlpha=.9;
  ctx.fillStyle=promoted?'#67f3a2':'#ffc45a';ctx.fillText(promoted?'STRATEGY LIVE ON PAPER':edge.version?'SHADOW TESTING':'CONNECTING',cx,cy-4);
  ctx.font=(compact?9:10)+'px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';ctx.fillStyle='#b9c7ca';
  if(es)ctx.fillText(promoted?('gate passed · '+es.bets+' bets · '+(es.winRatePct??0).toFixed(0)+'% won'):('evidence '+Math.min(es.bets,40)+'/40 bets · '+cal.samples+' calibration samples'),cx,cy+12);
  ctx.globalAlpha=1;ctx.fillStyle='#879b9f';ctx.font='10px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';ctx.fillText(compact?'LIVE SIGNAL NETWORK':'LIVE SIGNAL NETWORK · glow = mispricing after fees · rings = price error · bursts = paper fills',cx,h-21);ctx.textAlign='left';
 }
 function mount(canvas){
  if(!canvas||canvas.dataset.networkOrb)return;canvas.dataset.networkOrb='signal-driven-v162';const ctx=canvas.getContext('2d',{alpha:false});if(!ctx)return;
  const media=window.matchMedia('(prefers-reduced-motion: reduce)'),pause=document.getElementById('orbPause'),exportButton=document.getElementById('orbExport');let paused=media.matches,visible=true,last=null,raf=0,elapsed=0,width=0,height=0;
  function size(){const r=canvas.getBoundingClientRect();width=Math.max(1,r.width);height=Math.max(1,r.height);const dpr=Math.min(window.devicePixelRatio||1,2);const bw=Math.round(width*dpr),bh=Math.round(height*dpr);if(canvas.width!==bw||canvas.height!==bh){canvas.width=bw;canvas.height=bh;}ctx.setTransform(bw/width,0,0,bh/height,0,0);}
  function paint(){size();frame(ctx,width,height,(elapsed%LOOP)/LOOP);canvas.dataset.activePackets=String(packets.filter(p=>clock-p.born<7).length);canvas.dataset.receivedPackets=String(counts.reduce((a,b)=>a+b,0));}
  function active(){return !paused&&visible&&!document.hidden;}
  function tick(t){raf=0;if(!active()){last=null;return;}if(last!==null){const dt=Math.min(100,t-last);elapsed+=dt;clock+=dt/1000;}last=t;while(packets.length&&clock-packets[0].born>9)packets.shift();paint();raf=requestAnimationFrame(tick);}
  function sync(){emitting=active();if(!emitting)packets.length=0;if(pause){pause.textContent=paused?'Play':'Pause';pause.setAttribute('aria-pressed',String(paused));pause.setAttribute('aria-label',paused?'Play network orb animation':'Pause network orb animation');}if(active()&&!raf){last=null;raf=requestAnimationFrame(tick);}if(!active()&&raf){cancelAnimationFrame(raf);raf=0;last=null;}paint();}
  pause?.addEventListener('click',()=>{paused=!paused;sync();});
  exportButton?.addEventListener('click',()=>{const out=document.createElement('canvas');out.width=3840;out.height=2160;const c=out.getContext('2d',{alpha:false});c.scale(3840/width,2160/height);frame(c,width,height,(elapsed%LOOP)/LOOP);out.toBlob(blob=>{if(!blob)return;const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='POLY-EDGE-Network-Orb-4K.png';a.click();setTimeout(()=>URL.revokeObjectURL(url),5000);},'image/png');});
  document.addEventListener('visibilitychange',sync);media.addEventListener?.('change',()=>{paused=media.matches;sync();});new ResizeObserver(()=>{paint();sync();}).observe(canvas);new IntersectionObserver(es=>{visible=es[0].isIntersecting;sync();}).observe(canvas);paint();sync();
 }
 window.PolyNetworkOrb={mount,frame,project,nodes,links,arc,route,loopMs:LOOP,setTelemetry};
})();
