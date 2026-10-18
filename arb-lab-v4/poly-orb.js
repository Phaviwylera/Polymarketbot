'use strict';
/* Procedural vector paths; no image/video assets. One complete loop = 40 seconds. */
(() => {
  const TAU=Math.PI*2, LOOP=40000, N=224;
  const palette=['#ffc45a','#65d5ff'];
  let telemetry={snap:{},history:[]};
  const finite=v=>v!==null&&v!==undefined&&Number.isFinite(Number(v));
  const curve=(a,b,c,d,q)=>{const t=1-q;return t*t*t*a+3*t*t*q*b+3*t*q*q*c+q*q*q*d;};
  const nodes=Array.from({length:N},(_,i)=>{
    const y=1-2*(i+.5)/N, r=Math.sqrt(1-y*y), a=i*Math.PI*(3-Math.sqrt(5));
    return {x:Math.cos(a)*r,y,z:Math.sin(a)*r,color:i%5<2?0:1};
  });
  const links=[],used=new Set();
  nodes.forEach((a,i)=>{
    nodes.map((b,j)=>({j,d:(a.x-b.x)**2+(a.y-b.y)**2+(a.z-b.z)**2}))
      .filter(v=>v.j!==i).sort((a,b)=>a.d-b.d).slice(0,5).forEach(({j})=>{
        const key=Math.min(i,j)+':'+Math.max(i,j);
        if(!used.has(key)){used.add(key);links.push([i,j]);}
      });
  });
  function project(p,phase){
    const angle=phase*TAU, c=Math.cos(angle),s=Math.sin(angle);
    const x=p.x*c+p.z*s,z=-p.x*s+p.z*c;
    const tilt=-.25+.045*Math.sin(angle),ct=Math.cos(tilt),st=Math.sin(tilt);
    const y=p.y*ct-z*st,depth=p.y*st+z*ct,scale=3.5/(3.5-depth*.22);
    return {x:x*scale,y:y*scale,z:depth,scale};
  }
  function frame(ctx,w,h,phase,data=telemetry){
    phase=((phase%1)+1)%1;
    const cx=w*.72,cy=h*.51,R=Math.min(w*.235,h*.37),u=R/135;
    ctx.clearRect(0,0,w,h);ctx.fillStyle='#080d0e';ctx.fillRect(0,0,w,h);
    const aura=ctx.createRadialGradient(cx,cy,R*.2,cx,cy,R*1.65);
    aura.addColorStop(0,'rgba(101,213,255,.075)');aura.addColorStop(1,'rgba(8,13,14,0)');
    ctx.fillStyle=aura;ctx.fillRect(0,0,w,h);
    // Telemetry cards -> signal spine -> spherical network. Pulses follow the actual curves.
    const snap=data.snap||{},model=snap.model||{},history=data.history||[],compact=w<560;
    const left=compact?7:12,cardW=w*(compact?.225:.18),spine=w*.365;
    const top=24,bottom=h-48,step=(bottom-top)/6,cardH=step-5;
    const cards=[
      ['BTC LEAD',model.leadBps,'lead',1],['IMPULSE',model.impulse5Bps,'imp',1],
      ['FAIR UP',model.fairUp,'fair',100],['UP ASK',snap.bbo?.up?.ask,'up',100],
      ['DOWN ASK',snap.bbo?.down?.ask,'down',100],['PAIR EDGE',snap.pair?.lockedEdgePerShare,'pair',100]
    ];
    ctx.textAlign='left';ctx.textBaseline='alphabetic';
    ctx.font=(compact?'6':'7')+'px ui-monospace,monospace';ctx.fillStyle='#819089';
    ctx.fillText('MARKET INPUTS',left,13);
    ctx.strokeStyle='rgba(129,144,137,.045)';ctx.lineWidth=.6;
    for(let i=1;i<8;i++){ctx.beginPath();ctx.moveTo(0,i*h/8);ctx.lineTo(w,i*h/8);ctx.stroke();}
    cards.forEach(([label,value,key,mult],i)=>{
      const y=top+i*step,sy=y+cardH/2,ty=cy+(i-2.5)*h*.045,start=left+cardW;
      ctx.fillStyle='#0a1112';ctx.strokeStyle='rgba(129,144,137,.2)';ctx.lineWidth=.7;
      ctx.fillRect(left,y,cardW,cardH);ctx.strokeRect(left,y,cardW,cardH);
      ctx.fillStyle='#819089';ctx.font=(compact?'6':'7')+'px ui-monospace,monospace';ctx.fillText(label,left+5,y+11);
      ctx.fillStyle=finite(value)?'#e9f2ed':'#819089';ctx.font=(compact?'9':'11')+'px ui-monospace,monospace';
      ctx.fillText(finite(value)?(Number(value)*mult).toFixed(mult===100?1:2):'—',left+5,y+Math.min(cardH-5,25));
      const vals=history.map(q=>key==='lead'?(q.spot&&q.twap?(q.spot/q.twap-1)*10000:null):key==='fair'?q.fairUp:key==='up'?q.upAsk:key==='down'?q.downAsk:key==='pair'?q.pairEdge:null).filter(finite).map(Number);
      if(!compact&&vals.length>2){
        const lo=Math.min(...vals),range=Math.max(.001,Math.max(...vals)-lo),sx=left+cardW*.46,sw=cardW*.5;
        ctx.strokeStyle='rgba(101,213,255,.65)';ctx.beginPath();
        vals.forEach((v,j)=>{const px=sx+j/(vals.length-1)*sw,py=y+cardH-5-(v-lo)/range*(cardH-12);j?ctx.lineTo(px,py):ctx.moveTo(px,py);});ctx.stroke();
      }
      const c1=start+(spine-start)*.45,c2=spine-(spine-start)*.25;
      ctx.strokeStyle='rgba(103,243,162,.28)';ctx.lineWidth=.8;ctx.beginPath();ctx.moveTo(start,sy);ctx.bezierCurveTo(c1,sy,c2,ty,spine,ty);ctx.stroke();
      const q=(phase*12+i/6)%1;
      ctx.fillStyle='#67f3a2';ctx.beginPath();ctx.arc(curve(start,c1,c2,spine,q),curve(sy,sy,ty,ty,q),1.8,0,TAU);ctx.fill();
    });
    ctx.strokeStyle='rgba(101,213,255,.5)';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(spine,top);ctx.lineTo(spine,bottom);ctx.stroke();
    for(let i=0;i<13;i++){ctx.fillStyle=i%3?'#65d5ff':'#ffc45a';ctx.beginPath();ctx.arc(spine,top+i*(bottom-top)/12,i%3?1.2:2,0,TAU);ctx.fill();}
    for(let i=0;i<4;i++){ctx.fillStyle='#ffc45a';ctx.beginPath();ctx.arc(spine,top+((phase*8+i/4)%1)*(bottom-top),2.4,0,TAU);ctx.fill();}
    // Endpoints are projected sphere nodes, so the paths stay attached as it rotates.
    for(let i=0;i<24;i++){
      const p=project(nodes[(i*9)%N],phase),ex=cx+p.x*R,ey=cy+p.y*R,sy=cy+(i-11.5)*h*.009;
      const c1=w*.46,c2=cx-R*.85;
      ctx.strokeStyle='rgba(101,213,255,'+(.10+(i%3)*.025)+')';ctx.lineWidth=.65;
      ctx.beginPath();ctx.moveTo(spine,sy);ctx.bezierCurveTo(c1,sy,c2,ey,ex,ey);ctx.stroke();
      const q=(phase*(6+i%4*2)+i/24)%1;
      ctx.fillStyle=i%4?'#65d5ff':'#67f3a2';ctx.globalAlpha=.8;
      ctx.beginPath();ctx.arc(curve(spine,c1,c2,ex,q),curve(sy,sy,ey,ey,q),1.3,0,TAU);ctx.fill();ctx.globalAlpha=1;
    }
    ctx.save();ctx.translate(cx,cy);
    // A restrained ground shadow gives the wire sphere a floating, three-dimensional silhouette.
    ctx.save();ctx.translate(0,R*1.18);ctx.scale(1,.12);
    const shadow=ctx.createRadialGradient(0,0,0,0,0,R*.76);
    shadow.addColorStop(0,'rgba(0,0,0,.3)');shadow.addColorStop(1,'rgba(54,85,123,0)');
    ctx.fillStyle=shadow;ctx.beginPath();ctx.arc(0,0,R*.76,0,TAU);ctx.fill();ctx.restore();
    const particles=Array.from({length:64},(_,i)=>{
      const a=TAU*(i/64+phase*(i%3===0?-1:1));
      const r=1.27+.22*((i*37%61)/61),p=project({x:Math.cos(a)*r,y:Math.sin(a)*(i%2?.42:.72),z:Math.sin(a+.6)*r},phase);
      return {...p,index:i};
    });
    const particle=p=>{
      ctx.globalAlpha=.14+.27*(p.z+1.5)/3;
      ctx.fillStyle=palette[p.index%2];ctx.beginPath();ctx.arc(p.x*R,p.y*R,(.6+(p.index%3)*.16)*u,0,TAU);ctx.fill();ctx.globalAlpha=1;
    };
    particles.filter(p=>p.z<0).forEach(particle);
    const pts=nodes.map(p=>({...project(p,phase),color:p.color}));
    // Back edges first, then front edges. No screen-space random motion or changing topology.
    const edges=links.map(([a,b])=>({a:pts[a],b:pts[b],z:(pts[a].z+pts[b].z)/2})).sort((a,b)=>a.z-b.z);
    edges.forEach(({a,b,z})=>{
      const front=(z+1)/2;
      ctx.strokeStyle=a.color===b.color?(a.color===0?'#ffc45a':'#65d5ff'):'#67f3a2';
      ctx.globalAlpha=.14+.34*front;ctx.lineWidth=(.44+.23*front)*u;
      ctx.beginPath();ctx.moveTo(a.x*R,a.y*R);ctx.lineTo(b.x*R,b.y*R);ctx.stroke();
    });
    pts.sort((a,b)=>a.z-b.z).forEach((p,i)=>{
      const front=(p.z+1)/2, radius=(1.10+1.18*front)*u*p.scale;
      ctx.globalAlpha=.27+.73*front;ctx.fillStyle=palette[p.color];
      if(front>.65){
        ctx.globalAlpha=.045+.045*front;ctx.beginPath();ctx.arc(p.x*R,p.y*R,radius*2.8,0,TAU);ctx.fill();
        ctx.globalAlpha=.09;ctx.beginPath();ctx.arc(p.x*R,p.y*R,radius*1.8,0,TAU);ctx.fill();
      }
      ctx.globalAlpha=.27+.73*front;ctx.beginPath();ctx.arc(p.x*R,p.y*R,radius,0,TAU);ctx.fill();
      if(front>.58){ctx.globalAlpha=.85;ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(p.x*R-radius*.15,p.y*R-radius*.2,radius*.28,0,TAU);ctx.fill();}
    });
    particles.filter(p=>p.z>=0).forEach(particle);
    ctx.globalAlpha=1;ctx.restore();
  }
  function mount(canvas){
    if(!canvas||canvas.dataset.networkOrb)return;
    canvas.dataset.networkOrb='terminal-flow-v2';
    const ctx=canvas.getContext('2d',{alpha:false});if(!ctx)return;
    const media=window.matchMedia('(prefers-reduced-motion: reduce)');
    const pause=document.getElementById('orbPause'),exportButton=document.getElementById('orbExport');
    let paused=media.matches,visible=true,elapsed=0,last=null,raf=0,width=0,height=0;
    function size(){
      const r=canvas.getBoundingClientRect();width=Math.max(1,r.width);height=Math.max(1,r.height);
      const dpr=Math.max(1,Math.min(window.devicePixelRatio||1,3840/width,2160/height));
      const bw=Math.round(width*dpr),bh=Math.round(height*dpr);
      if(canvas.width!==bw||canvas.height!==bh){canvas.width=bw;canvas.height=bh;}
      ctx.setTransform(bw/width,0,0,bh/height,0,0);
    }
    function paint(){size();frame(ctx,width,height,elapsed/LOOP);}
    function active(){return !paused&&visible&&!document.hidden;}
    function tick(t){raf=0;if(!active()){last=null;return;}
      if(last!==null)elapsed=(elapsed+Math.min(100,t-last))%LOOP;last=t;paint();raf=requestAnimationFrame(tick);
    }
    function sync(){
      if(pause){pause.textContent=paused?'Play':'Pause';pause.setAttribute('aria-pressed',String(paused));pause.setAttribute('aria-label',paused?'Play network orb animation':'Pause network orb animation');}
      if(active()&&!raf){last=null;raf=requestAnimationFrame(tick);}
      if(!active()&&raf){cancelAnimationFrame(raf);raf=0;last=null;}
    }
    if(pause)pause.addEventListener('click',()=>{paused=!paused;sync();});
    if(exportButton)exportButton.addEventListener('click',()=>{
      const out=document.createElement('canvas');out.width=3840;out.height=2160;
      frame(out.getContext('2d',{alpha:false}),out.width,out.height,elapsed/LOOP);
      out.toBlob(blob=>{if(!blob)return;const url=URL.createObjectURL(blob),a=document.createElement('a');
        a.href=url;a.download='POLY-EDGE-Network-Orb-4K.png';a.click();setTimeout(()=>URL.revokeObjectURL(url),5000);
      },'image/png');
    });
    document.addEventListener('visibilitychange',sync);
    const motion=()=>{paused=media.matches;sync();};
    if(media.addEventListener)media.addEventListener('change',motion);
    if(window.ResizeObserver)new ResizeObserver(()=>{paint();sync();}).observe(canvas);
    else window.addEventListener('resize',paint);
    if(window.IntersectionObserver)new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;sync();}).observe(canvas);
    paint();sync();
  }
  window.PolyNetworkOrb={mount,frame,project,nodes,links,loopMs:LOOP,setTelemetry(snap,history){telemetry={snap:snap||{},history:(history||[]).slice(-90)};}};
})();
