'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const scope={window:{},document:{hidden:false},console};vm.createContext(scope);
vm.runInContext(fs.readFileSync(__dirname+'/poly-orb.js','utf8'),scope);
const {nodes,links,route,arc,project}=scope.window.PolyNetworkOrb;
const edge=new Set(links.map(([a,b])=>[Math.min(a,b),Math.max(a,b)].join(':')));
for(let asset=0;asset<4;asset++)for(let id=0;id<40;id++){
 const path=route(asset,id);for(let k=1;k<path.length;k++)assert(edge.has([Math.min(path[k-1],path[k]),Math.max(path[k-1],path[k])].join(':')));
}
for(const [a,b] of links){for(const t of [0,.25,.5,.75,1]){
 const start={x:nodes[a].x*1.018,y:nodes[a].y*1.018,z:nodes[a].z*1.018},p=arc(start,nodes[b],t);
 assert(Number.isFinite(project(p,.37).x));
 if(t===0)assert(Math.hypot(p.x-start.x,p.y-start.y,p.z-start.z)<1e-12);
 if(t===1)assert(Math.hypot(p.x-nodes[b].x,p.y-nodes[b].y,p.z-nodes[b].z)<1e-12);
}}
console.log('Orb geometry: 160 connected routes and all projected edge endpoints verified');
