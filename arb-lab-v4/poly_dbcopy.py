"""Copy the bot's Postgres data to a second database, verify it, and prove it can be restored from.

Used for both the periodic backup (POLY_BACKUP_URL) and the migration off the expiring free
database: point POLY_BACKUP_URL at the new database, let a copy verify, then switch
POLY_DATABASE_URL to it. The old database is never modified. Connection strings are never
logged; only table names, counts and checksums are.

Verification after every copy:
  * row counts per state row and per event kind match the source,
  * event checksums match (count, sum and max of t per kind),
  * restore check: the same reads a restart performs (paper/edge/model state, labels and trade
    attribution) succeed on the target and parse, and the paper checkpoint's portfolio totals match.
"""
from __future__ import annotations
import asyncio,json,time

BATCH=2000

async def _connect(url):
    import asyncpg
    return await asyncpg.connect(url,timeout=20,command_timeout=60)

async def _summary(conn,cutoff=None):
    """Exact per-kind checksums. sum(t) is taken as NUMERIC (float sums of ~30k epoch timestamps differ in
    the last digits with row order) and only over events at or before `cutoff`, because the live bot keeps
    writing new events while the copy runs."""
    states={r['name']:float(r['updated_at']) for r in await conn.fetch('SELECT name,updated_at FROM poly_v161_state')}
    q='SELECT kind,count(*) n,sum(t::numeric) s,max(t) m FROM poly_v161_events'+(' WHERE t<=$1' if cutoff is not None else '')+' GROUP BY kind'
    rows=await conn.fetch(q,cutoff) if cutoff is not None else await conn.fetch(q)
    kinds={r['kind']:{'rows':int(r['n']),'sumT':str(round(r['s'] or 0,3)),'maxT':float(r['m'] or 0)} for r in rows}
    return {'stateRows':states,'events':kinds,'eventRows':sum(k['rows'] for k in kinds.values())}

async def _portfolio(conn):
    raw=await conn.fetchval("SELECT payload FROM poly_v161_state WHERE name='paper'")
    p=(json.loads(raw) if isinstance(raw,str) else raw or {}).get('portfolio') or {}
    return {k:p.get(k) for k in ('equity','realizedPnl','trades','lifetimeRealizedPnl','lifetimeTrades','epochStart','riskLocked','maxDrawdownUsd')}

async def restore_check(conn):
    """The reads DurableStore.promote() performs on boot, against the target."""
    out={}
    for name in ('paper','edge','model'):
        raw=await conn.fetchval('SELECT payload FROM poly_v161_state WHERE name=$1',name)
        if raw is None:out[name]='absent';continue
        obj=json.loads(raw) if isinstance(raw,str) else raw;out[name]=f'ok ({len(json.dumps(obj))} bytes)'
    for kind,limit in (('SHADOW_OUTCOME',12000),('TRADE_ATTRIBUTION',5000)):
        rows=await conn.fetch('SELECT payload FROM poly_v161_events WHERE kind=$1 ORDER BY t DESC LIMIT $2',kind,limit)
        for r in rows[:50]:json.loads(r['payload']) if isinstance(r['payload'],str) else r['payload']
        out[kind]=len(rows)
    return out

async def copy_database(source_url,target_url,schema,log=print):
    """Upsert every state row and insert every missing event into the target, then verify."""
    started=time.time();src=await _connect(source_url);dst=await _connect(target_url)
    try:
        await dst.execute(schema)
        cutoff=float(await src.fetchval('SELECT coalesce(max(t),0) FROM poly_v161_events'))   # verification horizon
        states=await src.fetch('SELECT name,updated_at,payload::text AS payload FROM poly_v161_state')
        async with dst.transaction():
            for r in states:
                await dst.execute('INSERT INTO poly_v161_state VALUES($1,$2,$3::jsonb) ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload',
                                  r['name'],r['updated_at'],r['payload'])
        copied=0;last_t=-1.;last_id=''
        while True:   # keyset pagination keeps memory flat and the source lightly loaded
            rows=await src.fetch('SELECT id,t,asset,kind,payload::text AS payload FROM poly_v161_events WHERE (t,id)>($1,$2) ORDER BY t,id LIMIT $3',last_t,last_id,BATCH)
            if not rows:break
            await dst.executemany('INSERT INTO poly_v161_events(id,t,asset,kind,payload) VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT(id) DO NOTHING',
                                  [(r['id'],r['t'],r['asset'],r['kind'],r['payload']) for r in rows])
            copied+=len(rows);last_t=float(rows[-1]['t']);last_id=rows[-1]['id']
            await asyncio.sleep(.05)
        # Mirror the bot's retention pruning: per kind, drop target rows older than the oldest the source keeps.
        pruned=0
        for r in await src.fetch('SELECT kind,min(t) m FROM poly_v161_events GROUP BY kind'):
            res=await dst.execute('DELETE FROM poly_v161_events WHERE kind=$1 AND t<$2',r['kind'],r['m'])
            pruned+=int(res.split()[-1]) if res.startswith('DELETE') else 0
        a=await _summary(src,cutoff);b=await _summary(dst,cutoff)
        mismatched=[k for k,v in a['events'].items() if b['events'].get(k)!=v]
        missing_state=[k for k in a['stateRows'] if k not in b['stateRows']]
        pa=await _portfolio(src);pb=await _portfolio(dst)
        restore=await restore_check(dst)
        ok=not mismatched and not missing_state and pa==pb and all(v!='absent' for k,v in restore.items() if k=='paper')
        report={'ok':ok,'at':time.time(),'verifiedUpToT':cutoff,'seconds':round(time.time()-started,1),'stateRows':len(a['stateRows']),'eventRows':a['eventRows'],
                'eventRowsTarget':b['eventRows'],'copiedEventRows':copied,'prunedToMatchSource':pruned,'mismatchedKinds':mismatched,'missingStateRows':missing_state,
                'portfolioMatches':pa==pb,'portfolio':pa,'restoreCheck':restore}
        log('POLY_BACKUP '+json.dumps(report,separators=(',',':'),default=str))
        return report
    finally:
        await src.close();await dst.close()
