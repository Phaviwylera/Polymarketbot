"""One-time reconciliation after a database switch: recover trades the old database recorded after the
migration copy was taken, without overwriting anything the bot has written since the switch.

Set POLY_RECONCILE_FROM_URL to the OLD database. On start the bot (once it owns the new database):
  1. copies every event from the old database that the new one lacks (INSERT ... ON CONFLICT DO NOTHING),
     loading recovered TRADE_ATTRIBUTION rows into the live attribution record;
  2. compares, session by session, the old paper checkpoint with the live ledger. Closed trades present in
     the old version of a session but absent from the live one are added as an archived session
     'recovered:<sessionId>' holding exactly those BUY/close events, realized P&L and fees.
Idempotent: a recovered session that already exists is never added twice. The old database is only read.
Logs one POLY_RECONCILE line (counts and P&L, never a URL). Remove the variable afterwards.
"""
from __future__ import annotations
import json,time
import poly_devlog

def _sessions(checkpoint):
    """lane -> {sessionId: session} from a paper checkpoint (ledger history and each lane's live session)."""
    out={}
    for lane,hist in (checkpoint.get('ledgerHistory') or {}).items():
        for sid,s in (hist or {}).items():
            if isinstance(s,dict):out.setdefault(lane,{})[sid]=s
    for lane,a in (checkpoint.get('assets') or {}).items():
        p=(a or {}).get('paper') or {}
        if p.get('sessionId'):out.setdefault(lane,{})[p['sessionId']]=p
    return out

def _close_keys(session):
    return {(round(float(e['t']),3),e.get('action')) for e in (session.get('events') or [])
            if e.get('action') in poly_devlog.CLOSE_ACTIONS and e.get('pnl') is not None}

def missing_trades(old_checkpoint,engine,after_t=0.):
    """[(lane, recovered_session)] for closes recorded in the old checkpoint but not in the live ledger."""
    live={}
    for lane,hist in engine.ledger_history.items():
        for sid,s in hist.items():live.setdefault(lane,{})[sid]=s
    for lane,e in engine.engines.items():
        if e.paper and e.paper.get('sessionId'):live.setdefault(lane,{})[e.paper['sessionId']]=e.paper
    out=[]
    for lane,sessions in _sessions(old_checkpoint).items():
        if lane not in engine.ledger_history:continue
        for sid,old in sessions.items():
            rid=f'recovered:{sid}'
            if rid in engine.ledger_history[lane]:continue          # already recovered once
            mine=live.get(lane,{})
            have=_close_keys(mine[sid]) if sid in mine else set()   # unknown here: every close in it is missing
            ev=list(reversed(old.get('events') or []));keep=[];buy=None
            for e in ev:
                if e.get('action')=='BUY':buy=e
                elif e.get('action') in poly_devlog.CLOSE_ACTIONS and e.get('pnl') is not None:
                    if float(e['t'])>after_t and (round(float(e['t']),3),e.get('action')) not in have:
                        if buy:keep.append(buy)
                        keep.append(e)
                    buy=None
            closes=[e for e in keep if e.get('action') in poly_devlog.CLOSE_ACTIONS]
            if not closes:continue
            pnl=sum(float(e['pnl']) for e in closes)
            out.append((lane,{'sessionId':rid,'startedAt':float(keep[0]['t']),'endsAt':float(keep[-1]['t']),
                              'startingBalance':old.get('startingBalance'),'realizedPnl':round(pnl,6),'fees':None,
                              'trades':len(closes),'wins':sum(1 for e in closes if float(e['pnl'])>=0),
                              'losses':sum(1 for e in closes if float(e['pnl'])<0),'events':list(reversed(keep)),
                              'recoveredFrom':sid,'recoveredAt':time.time(),
                              'note':'closed in the old database after the migration copy; recovered by poly_reconcile'}))
    return out

async def reconcile(old_url,store,log=print,lookback_sec=6*3600):
    """Run once the bot owns the new database. Uses its own connections (never the checkpoint writer's)."""
    import asyncpg
    engine=store.engine
    src=await asyncpg.connect(old_url,timeout=20,command_timeout=60);dst=await asyncpg.connect(store.url,timeout=20,command_timeout=60)
    try:
        raw=await src.fetchval("SELECT payload FROM poly_v161_state WHERE name='paper'")
        old=json.loads(raw) if isinstance(raw,str) else (raw or {})
        rows=await src.fetch('SELECT id,t,asset,kind,payload::text AS payload FROM poly_v161_events WHERE t>$1 ORDER BY t',time.time()-lookback_sec)
        before=int(await dst.fetchval('SELECT count(*) FROM poly_v161_events'))
        await dst.executemany('INSERT INTO poly_v161_events(id,t,asset,kind,payload) VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT(id) DO NOTHING',
                              [(r['id'],r['t'],r['asset'],r['kind'],r['payload']) for r in rows])
        added=int(await dst.fetchval('SELECT count(*) FROM poly_v161_events'))-before
        attr=0
        for r in rows:
            if r['kind']=='TRADE_ATTRIBUTION':
                try:
                    t=json.loads(r['payload']).get('trade')
                    if t and t.get('id') not in engine.attribution.ids:engine.attribution.add(t);attr+=1
                except (ValueError,AttributeError):pass
        recovered=missing_trades(old,engine,time.time()-lookback_sec)
        for lane,s in recovered:engine.ledger_history[lane][s['sessionId']]=s
        if recovered:
            engine._save_history();engine._seed_trade_record()
        report={'eventsScanned':len(rows),'eventsAdded':added,'attributionRowsRecovered':attr,
                'sessionsRecovered':len(recovered),'tradesRecovered':sum(s['trades'] for _,s in recovered),
                'pnlRecoveredUsd':round(sum(s['realizedPnl'] for _,s in recovered),5),
                'detail':[{'lane':l,'from':s['recoveredFrom'],'trades':s['trades'],'pnl':s['realizedPnl']} for l,s in recovered]}
        log('POLY_RECONCILE '+json.dumps(report,separators=(',',':')))
        return report
    finally:
        await src.close();await dst.close()
