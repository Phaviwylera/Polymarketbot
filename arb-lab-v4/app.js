'use strict';
/* ARB/LAB browser-only signing vault.
   Opportunity execution uses a Jito all-or-nothing bundle when available.
   The server never receives seed phrases, secret keys or vault passphrases. */
const SOL='So11111111111111111111111111111111111111112';
const USDC='EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';
const VAULT_STORAGE='arblab_one_encrypted_vault_v1',EVENT_STORAGE='arblab_one_events_v1',LIMIT_STORAGE='arblab_one_limit_v1',MARKET_STORAGE='arblab_market_history_v1';
const RESERVE=3_000_000, EST_FEE=180_000, MIN_PROFIT=30_000, BPS_BUFFER=100, MAX_ATTEMPTS=3, JITO_TIP_LAMPORTS=1000, STRICT_SLIPPAGE_BPS=1;
const $=id=>document.getElementById(id);
let wallet=null, walletPub='', phantomAddress='', vault=null, active=false, inflight=false, loop=null, stats={sol:0,usdc:0}, attemptToday=0, lastCandidate=null, runningScan=false;
let marketLoop=null,countdownLoop=null,nextMarketScanAt=0,runningMarketScan=false,marketHistory=[];
let events=[];
try{events=JSON.parse(localStorage.getItem(EVENT_STORAGE)||'[]');vault=JSON.parse(localStorage.getItem(VAULT_STORAGE)||'null');marketHistory=JSON.parse(localStorage.getItem(MARKET_STORAGE)||'[]')}catch{events=[];vault=null;marketHistory=[]}

function toast(message,bad=false){const e=$('toast');e.textContent=message;e.className=bad?'bad':'';e.style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>e.style.display='none',9500)}
function log(kind,msg,signature=''){const e={time:new Date().toISOString(),kind,msg,signature};events.unshift(e);events=events.slice(0,90);try{localStorage.setItem(EVENT_STORAGE,JSON.stringify(events))}catch{};renderEvents()}
function renderEvents(){const out=$('activity');out.replaceChildren();if(!events.length){const d=document.createElement('div');d.className='empty';d.textContent='No events yet. Scan a route to begin.';out.append(d);return}for(const evt of events.slice(0,25)){const row=document.createElement('div');row.className='event';const ico=document.createElement('div');ico.className='eventico';ico.textContent=evt.kind==='error'?'!':evt.kind==='success'?'✓':evt.kind==='scan'?'◇':'↗';const b=document.createElement('div');const m=document.createElement('div');m.className='message';m.textContent=evt.msg;const t=document.createElement('div');t.className='time';t.textContent=(evt.time||'').replace('T',' ').slice(0,19)+' UTC / '+evt.kind;b.append(m,t);if(evt.signature){const a=document.createElement('a');a.href='https://solscan.io/tx/'+encodeURIComponent(evt.signature);a.target='_blank';a.rel='noopener noreferrer';a.textContent='Verify transaction on Solscan ↗';b.append(a)}row.append(ico,b);out.append(row)}}
function fmt(n,places=6){return Number(n).toLocaleString('en-US',{maximumFractionDigits:places,minimumFractionDigits:places})}
function loadAttempts(){let d;try{d=JSON.parse(localStorage.getItem(LIMIT_STORAGE)||'null')}catch{}const day=new Date().toISOString().slice(0,10);if(!d||d.day!==day)d={day,count:0};attemptToday=d.count;$('cycleCount').innerHTML=`${attemptToday} <span>/ ${MAX_ATTEMPTS}</span>`;return d}
function markAttempt(){const d=loadAttempts();d.count++;localStorage.setItem(LIMIT_STORAGE,JSON.stringify(d));loadAttempts()}
function renderVault(){$('createVault').hidden=!!vault;$('unlockVault').hidden=!vault;const pub=vault?.address||'';$('botAddress').textContent=pub||'Create your bot wallet above';$('vaultTag').textContent=wallet?'UNLOCKED':vault?'ENCRYPTED · LOCKED':'VAULT MISSING';$('vaultTag').classList.toggle('amber',!wallet);$('copyBtn').disabled=!pub;$('lockBtn').disabled=!wallet; if(walletPub) $('networkState').textContent='WALLET '+walletPub.slice(0,4)+'…'+walletPub.slice(-4);renderMode()}
function renderMode(){const title=active?'● LIVE BUNDLE ARMED':inflight?'● BUNDLE SETTLING':'● DISARMED';$('modeBadge').textContent=title;$('modeBadge').classList.toggle('armed',active);$('sidebarStatus').textContent=active?'JITO BUNDLE ARMED':inflight?'BUNDLE SETTLING':'DISARMED';const pd=$('presenceDot');if(pd)pd.style.background=active?'#77ff9b':'#ffc44d';$('startBtn').disabled=active||inflight||!wallet;$('stopBtn').disabled=!active||inflight;$('withdrawBtn').disabled=active||inflight;$('recoverUsdc').disabled=active||inflight||!wallet;$('scanBtn').disabled=runningMarketScan;const ae=$('autoExecState');if(ae){ae.textContent=active?'ARMED':inflight?'SETTLING':'LOCKED';ae.className=active?'green':inflight?'amberText':''}}
const enc=new TextEncoder();
function b64(array){let binary='';const v=new Uint8Array(array);for(let i=0;i<v.length;i+=16384)binary+=String.fromCharCode(...v.slice(i,i+16384));return btoa(binary)}
function unb64(s){return Uint8Array.from(atob(s),c=>c.charCodeAt(0))}
async function keyFor(pass,salt){const raw=await crypto.subtle.importKey('raw',enc.encode(pass),'PBKDF2',false,['deriveKey']);return crypto.subtle.deriveKey({name:'PBKDF2',salt,iterations:310000,hash:'SHA-256'},raw,{name:'AES-GCM',length:256},false,['encrypt','decrypt'])}
async function encryptSecret(secret,pass){const salt=crypto.getRandomValues(new Uint8Array(16));const iv=crypto.getRandomValues(new Uint8Array(12));const key=await keyFor(pass,salt);const cipher=await crypto.subtle.encrypt({name:'AES-GCM',iv},key,secret);return {version:1,algorithm:'AES-256-GCM / PBKDF2-SHA256',salt:b64(salt),iv:b64(iv),cipher:b64(cipher)}}
async function decryptSecret(obj,pass){const key=await keyFor(pass,unb64(obj.salt));return new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:unb64(obj.iv)},key,unb64(obj.cipher)))}
function sdk(){if(!window.solanaWeb3?.Keypair)throw new Error('Solana signing library has not loaded. Check the internet connection and reload this site.');return window.solanaWeb3}
function saveBackup(){if(!vault)throw Error('Create a wallet before requesting a backup');const data=JSON.stringify(vault,null,2);const blob=new Blob([data],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='ARB-LAB-ENCRYPTED-BOT-WALLET-BACKUP.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);log('system','Encrypted wallet backup requested. Store it safely off this computer; losing password makes recovery impossible.')}
async function createVault(){try{if(vault)throw Error('A bot wallet exists already. Do not overwrite a funded wallet.');const pass=$('vaultPass').value; if(pass.length<12||pass!==$('vaultPass2').value)throw Error('Use a matching, unique passphrase at least 12 characters long.');const kp=sdk().Keypair.generate();const locked=await encryptSecret(kp.secretKey,pass);vault={version:1,address:kp.publicKey.toBase58(),...locked};localStorage.setItem(VAULT_STORAGE,JSON.stringify(vault));wallet=kp;walletPub=vault.address;$('vaultPass').value='';$('vaultPass2').value='';renderVault();saveBackup();toast('Bot wallet created. BACK UP the encrypted JSON file and remember your passphrase BEFORE sending SOL.');log('success','New dedicated bot wallet created in this browser. No real funds transferred.');await refresh()}catch(e){toast(e.message,true)}}
async function unlockVault(){try{if(!vault)throw Error('No vault. Import an encrypted backup or create a new one.');const pass=$('unlockPass').value;if(!pass)throw Error('Enter your wallet passphrase.');const bytes=await decryptSecret(vault,pass);const kp=sdk().Keypair.fromSecretKey(bytes);if(kp.publicKey.toBase58()!==vault.address)throw Error('Decryption produced a mismatched wallet. Stop.');wallet=kp;walletPub=vault.address;$('unlockPass').value='';toast('Dedicated bot wallet unlocked for this tab only.');renderVault();await refresh()}catch(e){toast('Unlock failed — wrong passphrase, damaged backup, or missing signing library.',true)}}
function lockVault(){disarm('Lock requested');wallet=null;walletPub='';renderVault();toast('Wallet locked. Automated trading stopped.')}
async function importVault(e){try{if(active||inflight)throw Error('Stop trading first');const file=e.target.files?.[0];if(!file)return;const item=JSON.parse(await file.text());if(item.version!==1||!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(item.address)||!item.cipher||!item.iv||!item.salt)throw Error('Invalid encrypted ARB/LAB wallet backup');if(vault&&!confirm('An encrypted wallet already exists in this browser. Replacing it may hide a funded wallet. Continue ONLY if you saved the old encrypted backup?'))return;wallet=null;walletPub='';vault=item;localStorage.setItem(VAULT_STORAGE,JSON.stringify(vault));renderVault();toast('Encrypted backup imported. Enter its passphrase to unlock.');log('system','Encrypted vault backup imported. No private key uploaded to the server.')}catch(err){toast(err.message,true)}finally{e.target.value=''}}
function apiKey(){return $('jupKey')?.value.trim()||''}
async function http(path,data,withKey=false){const headers={'Content-Type':'application/json'};if(withKey){const key=apiKey();if(key)headers['X-Jupiter-Api-Key']=key;}let res;try{res=await fetch(path,{method:'POST',headers,body:JSON.stringify(data),cache:'no-store'})}catch{throw Error('Network unavailable. No transaction confirmed. Stop and verify balances.')}let out;try{out=await res.json()}catch{throw Error('Provider returned an invalid response. Verify balances before trying again.')}if(!res.ok)throw Error(typeof out.detail==='string'?out.detail:'API error ('+res.status+')');return out}
const rpc=(method,params)=>http('/api/rpc',{method,params});
const order=(from,to,amount,taker=null,slippageBps=null)=>http('/api/order',{inputMint:from,outputMint:to,amount:String(amount),...(taker?{taker}:{}),...(slippageBps!==null?{slippageBps}:{})},true);
async function balances(){if(!vault)return {sol:0,usdc:0,usdcAccountExists:false};const pub=vault.address;const [sol,usdc]=await Promise.all([rpc('getBalance',[pub,{commitment:'confirmed'}]),rpc('getTokenAccountsByOwner',[pub,{mint:USDC},{encoding:'jsonParsed',commitment:'confirmed'}])]);const accounts=usdc.value||[];const v={sol:Number(sol.value)||0,usdc:accounts.reduce((n,a)=>n+Number(a.account?.data?.parsed?.info?.tokenAmount?.amount||0),0),usdcAccountExists:accounts.length>0};if(!Number.isSafeInteger(v.sol)||!Number.isSafeInteger(v.usdc))throw Error('Unexpected chain balance');return v}
async function refresh(){try{if(!vault){$('solBal').textContent='0.000000';$('usdcBal').textContent='0.0000';return}stats=await balances();$('solBal').textContent=fmt(stats.sol/1e9,6);$('usdcBal').textContent=fmt(stats.usdc/1e6,4);$('networkState').textContent='RPC: CONNECTED'}catch(e){$('networkState').textContent='RPC: UNAVAILABLE';toast('Cannot verify wallet balances: '+e.message,true)}}
async function getCandidate(){if(!vault)throw Error('Create your bot wallet first.');const values=await balances();stats=values;$('solBal').textContent=fmt(stats.sol/1e9,6);$('usdcBal').textContent=fmt(stats.usdc/1e6,4);const available=values.sol-RESERVE;if(available<160000)throw Error('Insufficient SOL after 0.003 SOL network/account reserve. With $2, you may need more for viable transactions.');let spend=Math.min(Math.floor(available*.4),15000000);if(spend<1000)throw Error('Spend amount below routing minimum');let leg1=await order(SOL,USDC,spend,vault.address);if(Number(leg1.outAmount)>1_000_000){spend=Math.max(1000,Math.floor(spend*990000/Number(leg1.outAmount)));leg1=await order(SOL,USDC,spend,vault.address)}const rawUsdc=Number(leg1.outAmount);if(!Number.isSafeInteger(rawUsdc)||rawUsdc<1000||rawUsdc>1000000)throw Error('No eligible SOL→USDC route within the $1 quoted-spend cap');let leg2=await order(USDC,SOL,rawUsdc,vault.address);const returned=Number(leg2.outAmount);if(!Number.isSafeInteger(returned)||returned<=0)throw Error('No valid USDC→SOL return quote');const buffer=Math.ceil(returned*BPS_BUFFER/10000);const net=returned-spend-EST_FEE-buffer;return {spend,rawUsdc,returned,buffer,net,eligible:net>MIN_PROFIT,at:new Date().toISOString()}}
function showCandidate(c){$('qSpend').textContent=fmt(c.spend/1e9,9)+' SOL';$('qReturn').textContent=fmt(c.returned/1e9,9)+' SOL';$('qNet').textContent=(c.net>0?'+':'')+fmt(c.net/1e9,9)+' SOL';$('qNet').style.color=c.eligible?'var(--green)':'var(--red)';$('scanHint').textContent=c.eligible?'EXECUTION REQUOTE PASSED — not a guarantee.':'Execution requote below threshold — no real trade.'}
async function scan(manual=true){if(runningScan||inflight)return null;runningScan=true;renderMode();try{const c=await getCandidate();lastCandidate=c;showCandidate(c);if(manual)log('scan',(c.eligible?'Eligible estimated quote: ':'No trade: ')+fmt(c.net/1e9,9)+' SOL estimated net (after allowances).');return c}catch(e){$('scanHint').textContent='Scan unavailable: '+e.message;if(manual)log('error','Scan failed: '+e.message);if(manual)toast(e.message,true);return null}finally{runningScan=false;renderMode()}}

function money(v,places=5){const n=Number(v)||0;return (n<0?'-$':'+$')+Math.abs(n).toFixed(places)}
function plainMoney(v,places=5){return '$'+(Number(v)||0).toFixed(places)}
function verdictClass(v){return v==='EXECUTE-CANDIDATE'?'green':v==='WATCH'?'amberText':'red'}
function saveMarketHistory(){try{localStorage.setItem(MARKET_STORAGE,JSON.stringify(marketHistory.slice(0,24)))}catch{}}

function renderMarketHistory(){
  const body=$('historyBody');if(!body)return;
  body.replaceChildren();
  if(!marketHistory.length){const e=document.createElement('div');e.className='history-empty';e.textContent='Collecting live scans…';body.append(e);drawPnlChart();return}
  for(const h of marketHistory.slice(0,12)){
    const row=document.createElement('div');row.className='history-row';
    const time=document.createElement('span');time.textContent=new Date(h.timestamp*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const pnl=document.createElement('b');pnl.textContent=money(h.conservativeProfitUsd,5);pnl.className=h.conservativeProfitUsd>=0?'pos':'neg';
    const edge=document.createElement('span');edge.textContent=(h.conservativeEdgeBps>=0?'+':'')+Number(h.conservativeEdgeBps).toFixed(1)+'bp';
    const conf=document.createElement('span');conf.textContent=Number(h.confidence).toFixed(0)+'%';
    const state=document.createElement('b');state.textContent=h.verdict;state.className=h.verdict==='EXECUTE-CANDIDATE'?'pos':h.verdict==='WATCH'?'watch':'neg';
    row.append(time,pnl,edge,conf,state);body.append(row)
  }
  drawPnlChart()
}

function drawPnlChart(){
  const c=$('pnlCanvas');if(!c)return;
  const rect=c.getBoundingClientRect(),dpr=Math.max(1,Math.min(2,window.devicePixelRatio||1));
  c.width=Math.max(300,Math.floor(rect.width*dpr));c.height=Math.floor(150*dpr);
  const x=c.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);const w=rect.width,h=150;x.clearRect(0,0,w,h);
  x.strokeStyle='#14201e';x.lineWidth=1;
  for(let i=1;i<5;i++){x.beginPath();x.moveTo(0,i*h/5);x.lineTo(w,i*h/5);x.stroke()}
  const pts=marketHistory.slice(0,18).reverse();if(pts.length<2)return;
  const vals=pts.map(p=>Number(p.conservativeProfitUsd)||0),max=Math.max(.001,...vals.map(Math.abs)),zero=h/2;
  x.strokeStyle='#33423d';x.beginPath();x.moveTo(0,zero);x.lineTo(w,zero);x.stroke();
  x.lineWidth=1.5;x.strokeStyle=vals[vals.length-1]>=0?'#77ff9b':'#ff5f72';x.beginPath();
  pts.forEach((p,i)=>{const px=8+i*(w-16)/(pts.length-1),py=zero-(Number(p.conservativeProfitUsd)||0)/max*(h*.38);if(i)x.lineTo(px,py);else x.moveTo(px,py)});x.stroke();
  pts.forEach((p,i)=>{const px=8+i*(w-16)/(pts.length-1),py=zero-(Number(p.conservativeProfitUsd)||0)/max*(h*.38);x.fillStyle=(Number(p.conservativeProfitUsd)||0)>=0?'#77ff9b':'#ff5f72';x.fillRect(px-1.5,py-1.5,3,3)})
}

function renderMarket(d){
  const profit=Number(d.conservativeProfitUsd)||0;
  $('simProfit').textContent=money(profit,5);$('simProfit').style.color=profit>=0?'var(--green)':'var(--red)';
  $('simReturn').textContent=plainMoney(d.projectedReturnUsd,5);
  $('solPrice').textContent='$'+Number(d.solPriceUsd).toFixed(2);
  $('spreadBps').textContent=(Number(d.conservativeEdgeBps)>=0?'+':'')+Number(d.conservativeEdgeBps).toFixed(2)+' bps';
  $('lastScan').textContent=new Date(d.timestamp*1000).toLocaleTimeString();
  $('confidence').textContent=Number(d.confidence).toFixed(0)+'%';$('confidence').className=verdictClass(d.verdict);
  $('confidenceBar').style.width=Math.max(2,Math.min(100,Number(d.confidence)))+'%';
  $('qSpend').textContent=Number(d.startSolUi).toFixed(9)+' SOL';
  $('midUsdc').textContent='$'+Number(d.midUsdcUi).toFixed(6);
  $('qReturn').textContent=Number(d.returnSolUi).toFixed(9)+' SOL';
  $('rawProfit').textContent=money(d.rawProfitUsd,6);
  $('feeAllowance').textContent='-$'+Number(d.feeAllowanceUsd).toFixed(6);
  $('moveBuffer').textContent='-$'+Number(d.moveBufferUsd).toFixed(6);
  $('qNet').textContent=money(profit,6);$('qNet').className=profit>=0?'green':'red';
  $('marketState').textContent=d.verdict;$('marketState').className=verdictClass(d.verdict);
  $('scanStatus').textContent='LIVE · '+new Date(d.timestamp*1000).toLocaleTimeString();$('scanStatus').className=verdictClass(d.verdict);
  $('scanHint').textContent=d.verdict==='EXECUTE-CANDIDATE'?'Positive conservative quote detected. Real execution still re-quotes and requires a funded, unlocked, armed wallet.':d.verdict==='WATCH'?'Raw quote is positive but does not clear conservative allowances. No real trade.':'No fee-adjusted opportunity right now. Scanner remains active.';
  $('coreScore').textContent=Number(d.confidence).toFixed(0);$('freshness').textContent='LIVE < 60S';
  marketHistory.unshift(d);marketHistory=marketHistory.slice(0,24);saveMarketHistory();renderMarketHistory()
}

async function marketScan(manual=false){
  if(runningMarketScan)return null;
  runningMarketScan=true;renderMode();
  try{
    const d=await http('/api/hypothetical',{investmentUsd:2},false);
    renderMarket(d);
    if(manual)log('scan','$2 live market scan: '+money(d.conservativeProfitUsd,6)+' conservative P/L · '+d.verdict);
    return d
  }catch(e){
    $('scanStatus').textContent='SCAN ERROR';$('scanStatus').className='red';$('marketState').textContent='UNAVAILABLE';$('marketState').className='red';$('scanHint').textContent='Live market scan failed: '+e.message;
    if(manual)toast('Market scan failed: '+e.message,true);
    return null
  }finally{
    runningMarketScan=false;nextMarketScanAt=Date.now()+60000;renderMode()
  }
}

function updateClocks(){
  const now=new Date(),u=$('utcClock');if(u)u.textContent=now.toISOString().slice(11,19);
  const c=$('scanCountdown');if(c){const left=Math.max(0,Math.ceil((nextMarketScanAt-Date.now())/1000));c.textContent=String(Math.floor(left/60)).padStart(2,'0')+':'+String(left%60).padStart(2,'0')}
}
function startMarketFeed(){
  renderMarketHistory();nextMarketScanAt=Date.now();marketScan(false);
  clearInterval(marketLoop);marketLoop=setInterval(()=>marketScan(false),60000);
  clearInterval(countdownLoop);countdownLoop=setInterval(updateClocks,1000);updateClocks()
}

function startFlow(){
  const c=$('flowCanvas');if(!c)return;const ctx=c.getContext('2d');let t=0;
  function frame(){
    const r=c.getBoundingClientRect(),dpr=Math.max(1,Math.min(2,window.devicePixelRatio||1));
    if(c.width!==Math.floor(r.width*dpr)||c.height!==Math.floor(r.height*dpr)){c.width=Math.floor(r.width*dpr);c.height=Math.floor(r.height*dpr)}
    ctx.setTransform(dpr,0,0,dpr,0,0);const w=r.width,h=r.height,cx=w/2,cy=h/2;ctx.clearRect(0,0,w,h);t+=.008;
    const nodes=[],count=w<650?90:150;
    for(let i=0;i<count;i++){const a=i*.618033*6.283+t*(i%3===0?1:-.55),ring=.18+(i%11)/13,rx=w*(.13+ring*.34),ry=h*(.12+ring*.28),warp=Math.sin(t*2+i*.13)*8,x=cx+Math.cos(a)*rx+Math.sin(i)*warp,y=cy+Math.sin(a*1.03)*ry;nodes.push({x,y})}
    ctx.globalAlpha=.16;ctx.strokeStyle='#6fa7ff';ctx.lineWidth=.55;
    for(let i=0;i<nodes.length;i+=3){const a=nodes[i],b=nodes[(i*7+17)%nodes.length];ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.quadraticCurveTo(cx,cy,b.x,b.y);ctx.stroke()}
    ctx.globalAlpha=1;
    for(let i=0;i<nodes.length;i++){const n=nodes[i];ctx.fillStyle=i%5===0?'#ffc44d':i%7===0?'#77ff9b':'#6fa7ff';const z=i%9===0?2.4:1.2;ctx.fillRect(n.x-z/2,n.y-z/2,z,z)}
    ctx.strokeStyle='#273a35';ctx.lineWidth=1;ctx.beginPath();ctx.arc(cx,cy,Math.min(w,h)*.15,0,Math.PI*2);ctx.stroke();
    requestAnimationFrame(frame)
  }
  requestAnimationFrame(frame)
}

async function waitFinal(signature){if(!signature||typeof signature!=='string')throw Error('No valid transaction signature reported. Stop and inspect chain.');for(let i=0;i<16;i++){await new Promise(resolve=>setTimeout(resolve,2400));const rs=await rpc('getSignatureStatuses',[[signature],{searchTransactionHistory:true}]);const item=rs?.value?.[0];if(item?.err)throw Error('Transaction failed on-chain: '+JSON.stringify(item.err).slice(0,120));if(item&&(item.confirmationStatus==='confirmed'||item.confirmationStatus==='finalized'))return true}throw Error('Transaction confirmation uncertain. Do not retry blindly.')}
async function signOrderTransaction(q,label){
  if(!wallet)throw Error('Signing key locked');
  if(!q?.transaction)throw Error(label+': executable transaction unavailable');
  const s=sdk();let tx;
  try{
    tx=s.VersionedTransaction.deserialize(unb64(q.transaction));
    const signers=tx.message.staticAccountKeys.slice(0,tx.message.header.numRequiredSignatures);
    if(!signers.some(k=>k.equals(wallet.publicKey)))throw Error('transaction did not request bot wallet signature');
    tx.sign([wallet]);
    return b64(tx.serialize());
  }catch(e){throw Error(label+': signing failed: '+e.message)}
}

async function tradeLeg(from,to,amount,label){
  if(!wallet)throw Error('Signing key locked');
  if(!Number.isSafeInteger(amount)||amount<=0)throw Error('Invalid trade size');
  const q=await order(from,to,amount,wallet.publicKey.toBase58(),STRICT_SLIPPAGE_BPS);
  if(Number(q.inAmount)!==amount||!q.transaction||!q.requestId)throw Error(label+': executable order unavailable');
  const signed=await signOrderTransaction(q,label);
  const result=await http('/api/execute',{signedTransaction:signed,requestId:q.requestId},true);
  const signature=result.signature||'';
  if(signature)log('sent',label+' broadcast; awaiting confirmed finality.',signature);
  if(result.status!=='Success')throw Error(label+' did not confirm successful: '+String(result.error||result.status||'unknown'));
  await waitFinal(signature);log('success',label+' confirmed on Solana.',signature);return signature
}

async function getJitoTipAccount(){
  const r=await fetch('/api/jito/tip-accounts',{cache:'no-store'});
  const j=await r.json();
  if(!r.ok||!Array.isArray(j.accounts)||!j.accounts.length)throw Error('Jito tip accounts unavailable');
  const bytes=new Uint32Array(1);crypto.getRandomValues(bytes);
  return j.accounts[bytes[0]%j.accounts.length]
}

async function makeJitoTipTx(tipAccount){
  const s=sdk(),bh=await rpc('getLatestBlockhash',[{commitment:'processed'}]);
  const tx=new s.Transaction({feePayer:wallet.publicKey,recentBlockhash:bh.value.blockhash});
  tx.add(s.SystemProgram.transfer({fromPubkey:wallet.publicKey,toPubkey:new s.PublicKey(tipAccount),lamports:JITO_TIP_LAMPORTS}));
  tx.sign(wallet);return b64(tx.serialize())
}

async function waitJitoBundle(bundleId){
  for(let i=0;i<22;i++){
    await new Promise(r=>setTimeout(r,900));
    const st=await http('/api/jito/status',{bundleId});
    const item=st?.value?.[0];
    if(!item)continue;
    if(item.err && !('Ok' in item.err))throw Error('Jito bundle failed: '+JSON.stringify(item.err).slice(0,140));
    const cs=item.confirmation_status||item.confirmationStatus||'';
    if(cs==='confirmed'||cs==='finalized')return item;
  }
  throw Error('Jito bundle status uncertain; do not resubmit blindly')
}

async function attempt(c){
  if(!active||inflight||!wallet)return;
  loadAttempts();if(attemptToday>=MAX_ATTEMPTS){disarm('Daily attempt limit reached');return}
  const prev=await balances();
  if(!prev.usdcAccountExists){disarm('USDC token account missing; micro-arbitrage account-rent cost is too material to create automatically');return}
  if(prev.usdc>1000){disarm('Wallet already contains USDC; reconcile balances before a new bundle');return}
  if(prev.sol-c.spend<RESERVE){disarm('SOL reserve would be violated');return}

  inflight=true;renderMode();
  log('system','Building strict Jito atomic bundle: SOL → USDC → SOL + conditional bundle tip.');
  try{
    const q1=await order(SOL,USDC,c.spend,wallet.publicKey.toBase58(),STRICT_SLIPPAGE_BPS);
    if(Number(q1.inAmount)!==c.spend||!q1.transaction)throw Error('Atomic leg 1 order unavailable');
    const usdcIn=Number(q1.outAmount);
    if(!Number.isSafeInteger(usdcIn)||usdcIn<1000||usdcIn>1_000_000)throw Error('Atomic leg 1 output outside $1 safety cap');

    const q2=await order(USDC,SOL,usdcIn,wallet.publicKey.toBase58(),STRICT_SLIPPAGE_BPS);
    if(Number(q2.inAmount)!==usdcIn||!q2.transaction)throw Error('Atomic leg 2 order unavailable');
    const quotedReturn=Number(q2.outAmount);
    if(!Number.isSafeInteger(quotedReturn)||quotedReturn<=0)throw Error('Invalid atomic return quote');

    const worstReturn=Math.floor(quotedReturn*(1-STRICT_SLIPPAGE_BPS/10000));
    const worstNet=worstReturn-c.spend-EST_FEE-JITO_TIP_LAMPORTS;
    if(worstNet<=MIN_PROFIT)throw Error('Atomic worst-case quote no longer clears minimum profit: '+fmt(worstNet/1e9,9)+' SOL');

    const [tx1,tx2,tipAccount]=await Promise.all([
      signOrderTransaction(q1,'Atomic leg 1'),
      signOrderTransaction(q2,'Atomic leg 2'),
      getJitoTipAccount()
    ]);
    const tipTx=await makeJitoTipTx(tipAccount);
    markAttempt();
    const sent=await http('/api/jito/bundle',{transactions:[tx1,tx2,tipTx]});
    if(!sent.bundleId)throw Error('Jito did not return bundle id');
    log('sent','Jito bundle accepted: '+sent.bundleId.slice(0,18)+'…; waiting for atomic confirmation.');
    const landed=await waitJitoBundle(sent.bundleId);
    log('success','Jito atomic bundle confirmed in slot '+String(landed.slot||'unknown')+'.');

    await new Promise(r=>setTimeout(r,1200));
    const after=await balances();stats=after;const delta=after.sol-prev.sol;
    log(delta>0?'success':'error','Realized wallet SOL change '+fmt(delta/1e9,9)+' SOL after atomic bundle.');
    if(delta<=0)disarm('Bundle landed without positive wallet-level SOL delta');
    else toast('Atomic bundle landed. Realized change: +'+fmt(delta/1e9,9)+' SOL');
    await refresh();
  }catch(e){
    log('error','Atomic execution halted: '+e.message);
    disarm('Atomic bundle rejected / uncertain');
    toast('ATOMIC EXECUTION HALTED: '+e.message,true);
  }finally{inflight=false;renderMode()}
}
async function cycle(){if(!active||inflight)return;loadAttempts();if(attemptToday>=MAX_ATTEMPTS){disarm('Daily trade limit reached');return}const c=await scan(false);if(!c){disarm('Unable to verify profitable quote; fail-closed');return}if(!c.eligible)return;const repeated=await scan(false);if(!repeated||!repeated.eligible){log('scan','Price moved or second quote unavailable — trade rejected');return}if(Math.abs(repeated.spend-c.spend)>1000){log('scan','Quote size drifted; skip cycle');return}await attempt(repeated)}
function disarm(reason='Manually stopped'){active=false;clearInterval(loop);loop=null;log('system','Auto trading disabled: '+reason+'. Pending transaction may still settle.');renderMode()}
async function arm(){
  try{
    if(!wallet)throw Error('Unlock your encrypted bot wallet first.');
    if(active||inflight)throw Error('A transaction is already underway');
    loadAttempts();
    if(attemptToday>=MAX_ATTEMPTS)throw Error('Daily attempt cap already reached.');
    const bl=await balances();
    if(bl.sol<=RESERVE+180000)throw Error('Insufficient SOL after reserve. Scanner will keep running, but no real trade can execute.');
    if(!bl.usdcAccountExists)throw Error('Your bot wallet has no USDC token account. I will not create one automatically because its rent cost is material for a $2 wallet.');if(bl.usdc>0)throw Error('Wallet contains USDC already. Recover or swap it before automatic cycling.');
    if(!confirm('REAL MONEY: Once armed, qualifying opportunities can trigger a Jito all-or-nothing swap bundle without per-trade confirmation. Bundle landing is not guaranteed and profits are not guaranteed. Continue?'))return;
    const typed=prompt('To arm live trading, type exactly: I ACCEPT LIVE BUNDLE RISK');
    if(typed!=='I ACCEPT LIVE BUNDLE RISK')throw Error('Live trading was not authorized.');
    active=true;
    log('system','LIVE AUTO armed. Qualifying opportunities trigger a strict Jito atomic-bundle build; no sequential-swap fallback is allowed.');
    renderMode();
    await cycle();
  }catch(e){toast(e.message,true)}
}
async function withdraw(){try{if(active||inflight)throw Error('Stop automatic trading and let pending transactions settle first.');if(!wallet)throw Error('Unlock the bot wallet first.');if($('withdrawWord').value.trim()!=='WITHDRAW')throw Error('Type WITHDRAW to approve the transfer');const address=$('withdrawDest').value.trim();const s=sdk();const pub=new s.PublicKey(address);if(pub.toBase58()===wallet.publicKey.toBase58())throw Error('Destination cannot be the same bot wallet');if(!s.PublicKey.isOnCurve(pub.toBytes()))throw Error('Use a normal Phantom wallet address (not a program account)');const amount=Number($('withdrawAmount').value);const raw=Math.floor(amount*1e9);if(!Number.isSafeInteger(raw)||raw<10000)throw Error('Enter a valid SOL amount of at least 0.00001 SOL');const available=await balances();if(raw+15000>=available.sol)throw Error('Balance insufficient after preserving 0.000015 SOL for network fees');if(!confirm('Final check: Send '+fmt(raw/1e9,9)+' SOL to '+address+'? This cannot be reversed.'))return;inflight=true;renderMode();const bh=await rpc('getLatestBlockhash',[{commitment:'confirmed'}]);const tx=new s.Transaction({feePayer:wallet.publicKey,recentBlockhash:bh.value.blockhash});tx.add(s.SystemProgram.transfer({fromPubkey:wallet.publicKey,toPubkey:pub,lamports:raw}));tx.sign(wallet);const sig=await rpc('sendTransaction',[b64(tx.serialize()),{encoding:'base64',skipPreflight:false,preflightCommitment:'confirmed'}]);log('sent','Withdrawal sent to your Phantom address; waiting confirmation',String(sig));await waitFinal(String(sig));log('success','WITHDRAWAL CONFIRMED: '+fmt(raw/1e9,9)+' SOL transferred',String(sig));toast('Withdrawal confirmed! Open the Solscan link in activity.');$('withdrawWord').value='';await refresh()}catch(e){log('error','Withdrawal not confirmed: '+e.message);toast('Withdrawal status uncertain: '+e.message,true)}finally{inflight=false;renderMode()}}
async function recoverUsdc(){try{if(active||inflight)throw Error('Stop the bot and allow pending transactions to settle first');if(!wallet)throw Error('Unlock your encrypted bot wallet first');const b=await balances();if(b.usdc<1000)throw Error('No stranded USDC detected. There is nothing to recover.');if(b.usdc>1_000_000)throw Error('Recovery cap is $1 USDC. Stop and use an audited wallet tool for larger funds.');if(b.sol<300000)throw Error('Insufficient SOL for potential swap network and token-account costs.');if(!confirm('Recover '+fmt(b.usdc/1e6,6)+' USDC back to SOL? This is a NEW real swap and can lose money. Proceed?'))return;inflight=true;renderMode();await tradeLeg(USDC,SOL,b.usdc,'Manual recovery USDC → SOL');await refresh();toast('USDC recovery swap confirmed. Check balance before withdrawing SOL.')}catch(e){log('error','USDC recovery failed or uncertain: '+e.message);toast(e.message,true)}finally{inflight=false;renderMode()}}
async function phantom(){try{const p=window.phantom?.solana||window.solana;if(!p?.isPhantom)throw Error('Phantom not found. On mobile, open this website inside Phantom’s browser; otherwise paste your public address manually.');const result=await p.connect();phantomAddress=result.publicKey.toString();$('phantomAddr').textContent='Public Phantom address: '+phantomAddress;toast('Phantom public address connected. It is NOT granted spend permissions.')}catch(e){toast(e.message,true)}}
let opportunityExecutionQueued=false;
window.addEventListener('arb-lab-opportunity',async (ev)=>{
  if(!active||!wallet||inflight||opportunityExecutionQueued)return;
  opportunityExecutionQueued=true;
  try{
    log('scan','Server live feed flagged an execution candidate. Running strict funded-wallet re-quote now.');
    await cycle();
  }catch(e){
    log('error','Opportunity-triggered execution check failed: '+e.message);
    disarm('Execution check failed');
  }finally{
    opportunityExecutionQueued=false;
  }
});

function wire(){
  renderVault();renderEvents();loadAttempts();renderMarketHistory();
  $('createBtn').onclick=createVault;$('unlockBtn').onclick=unlockVault;$('lockBtn').onclick=lockVault;$('backupBtn').onclick=()=>{try{saveBackup()}catch(e){toast(e.message,true)}};$('importFile').onchange=importVault;
  $('copyBtn').onclick=async()=>{if(!vault)return;try{await navigator.clipboard.writeText(vault.address);toast('Bot deposit address copied. Verify before sending funds.')}catch{toast('Clipboard unavailable. Select and copy the displayed public address.',true)}};
  $('phantomBtn').onclick=phantom;$('refreshBtn').onclick=refresh;$('scanBtn').onclick=()=>{if(wallet)scan(true);else toast('Live market scanning is already continuous. Create/unlock/fund the bot wallet only when you want real execution.',false)};$('startBtn').onclick=arm;$('stopBtn').onclick=()=>disarm('User pressed Stop');
  $('usePhantom').onclick=()=>{if(!phantomAddress)return toast('Connect Phantom first, or paste its public receiving address.',true);$('withdrawDest').value=phantomAddress};
  $('withdrawBtn').onclick=withdraw;$('recoverUsdc').onclick=recoverUsdc;
  $('clearHistory').onclick=()=>{if(confirm('Clear only this browser’s execution log? Blockchain records remain unchanged.')){events=[];localStorage.removeItem(EVENT_STORAGE);renderEvents()}};
  window.addEventListener('resize',drawPnlChart);
  window.addEventListener('beforeunload',()=>{active=false;clearInterval(loop);clearInterval(marketLoop);clearInterval(countdownLoop)});
  setTimeout(refresh,500)
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',wire);else wire();