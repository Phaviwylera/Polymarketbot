'use strict';
/* Procedural vector paths; no image/video assets. One complete loop = 40 seconds. */
(() => {
  const TAU=Math.PI*2, LOOP=40000, N=224;
  const palette=['#ed792d','#2d82d4'];
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
  function frame(ctx,w,h,phase){
    phase=((phase%1)+1)%1;
    const cx=w/2,cy=h*.49,R=Math.min(w*.30,h*.365),u=R/135;
    ctx.clearRect(0,0,w,h);ctx.fillStyle='#fbfcfe';ctx.fillRect(0,0,w,h);
    const aura=ctx.createRadialGradient(cx,cy,R*.2,cx,cy,R*1.65);
    aura.addColorStop(0,'rgba(225,235,248,.17)');aura.addColorStop(1,'rgba(255,255,255,0)');
    ctx.fillStyle=aura;ctx.fillRect(0,0,w,h);
    ctx.save();ctx.translate(cx,cy);
    // A restrained ground shadow gives the wire sphere a floating, three-dimensional silhouette.
    ctx.save();ctx.translate(0,R*1.18);ctx.scale(1,.12);
    const shadow=ctx.createRadialGradient(0,0,0,0,0,R*.76);
    shadow.addColorStop(0,'rgba(54,85,123,.10)');shadow.addColorStop(1,'rgba(54,85,123,0)');
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
      ctx.strokeStyle=a.color===b.color?(a.color===0?'#dc9b72':'#7caddb'):'#9eb5c9';
      ctx.globalAlpha=.08+.28*front;ctx.lineWidth=(.44+.23*front)*u;
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
    canvas.dataset.networkOrb='orange-blue-v1';
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
  window.PolyNetworkOrb={mount,frame,project,nodes,links,loopMs:LOOP};
})();
