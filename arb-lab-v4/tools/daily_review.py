"""Daily health and results review for the evolution agent (read-only).

Compares the newest bot status (bot-status branch, status/latest.json) with the snapshot about 24 h earlier and prints
a markdown report plus a JSON block of findings. It flags:
  * server down / stale status, deploy lag (running revision != origin/main)
  * outages: a lane whose error counter grew a lot, or whose activity counter stopped moving
  * every lane's headline result and its 24 h change
Usage (inside a clone of the repo):  python arb-lab-v4/tools/daily_review.py [--hours 24]
Exit code 0 always; the findings decide what the agent does.
"""
from __future__ import annotations
import json,subprocess,sys,time

ERR_JUMP=200          # errors added in the window that count as an outage signal
STALE_MIN=45          # status older than this = server may be down
BOOT_GRACE_SEC=2*3600 # after a restart, a counter still at 0 this long after boot counts as stalled
# activity counters that must keep moving while a lane is enabled
ACTIVITY={'eventmaker':'polls','spotlead':'prints','pairacc':'bookEvents','fastmaker':'bookEvents','maker':'prints','kalshi':'scans',
          'eventarb':'scans','rivals':'runs','xmarket':'scans','events':'scans'}

def git(*a):return subprocess.run(['git',*a],capture_output=True,text=True).stdout
def snapshots(hours):
    git('fetch','-q','origin','bot-status:refs/remotes/origin/bot-status','main:refs/remotes/origin/main')
    shas=git('log','origin/bot-status','--format=%H %ct','--','status/latest.json').split('\n')
    rows=[(s.split()[0],int(s.split()[1])) for s in shas if s.strip()]
    if not rows:return None,None
    new=rows[0];target=new[1]-hours*3600
    old=next((r for r in rows if r[1]<=target),rows[-1])
    load=lambda sha:json.loads(git('show',f'{sha}:status/latest.json') or '{}')
    return load(new[0]),load(old[0])

def num(x):
    try:return float(x)
    except (TypeError,ValueError):return None

def review(new,old,main_head=None,now=None):
    """Pure: findings from two status snapshots. Returns (findings list, lane rows)."""
    now=now or time.time();f=[];rows=[]
    at=new.get('at') or new.get('generatedAtEpoch') or new.get('ts')
    if at is None and new.get('atUtc'):
        try:at=time.mktime(time.strptime(new['atUtc'][:19],'%Y-%m-%d %H:%M:%S'))-time.timezone
        except ValueError:at=None
    if at and (now-float(at))/60>STALE_MIN:f.append({'kind':'outage','severity':'high','what':f'status is {round((now-float(at))/60)} min old: server or reporter may be down'})
    rev=str(new.get('revision') or '')
    if main_head and rev and not main_head.startswith(rev) and not rev.startswith(main_head[:7]):
        f.append({'kind':'deploy','severity':'medium','what':f'server runs {rev}, main is {main_head[:7]}: auto-update may be failing (tests or pull)'})
    nl,ol=new.get('newLanes') or {},old.get('newLanes') or {}
    # Counters reset to 0 on every restart (each daily-agent push restarts the server). If the server restarted inside
    # the window, the old snapshot's counters belong to the previous boot: compare against 0 instead.
    up,at0=num(new.get('uptimeSec')),num(old.get('at'))
    restarted=up is not None and at and at0 is not None and up<float(at)-at0-60
    for lane,cur in sorted(nl.items()):
        if not isinstance(cur,dict):continue
        prev=ol.get(lane) or {}
        e1,e0=num(cur.get('errors')),(0. if restarted else num(prev.get('errors')))
        if e1 is not None and e0 is not None and e1-e0>=ERR_JUMP:
            f.append({'kind':'outage','severity':'high','lane':lane,'what':f'{lane}: {int(e1-e0)} new errors in the window; last: {str(cur.get("lastError"))[:160]}'})
        k=ACTIVITY.get(lane)
        c1=num(cur.get(k)) if k else None
        if restarted:c0=0. if up>=BOOT_GRACE_SEC else None
        else:c0=num(prev.get(k)) if k else None
        if k and cur.get('enabled',True) and c1 is not None and c0 is not None and c1==c0:
            f.append({'kind':'stalled','severity':'high','lane':lane,'what':f'{lane}: {k} did not move in the window ({cur.get(k)})'})
        rows.append({'lane':lane,'errors':cur.get('errors'),'lastError':str(cur.get('lastError'))[:120] if cur.get('lastError') else None,'headline':headline(lane,cur),'before':headline(lane,prev)})
    return f,rows

def headline(lane,x):
    if not x:return None
    for key in ('S2','M3','K1','N1','S1','M2','M1','X1','E1','R1'):
        if isinstance(x.get(key),dict):
            t=x[key];s=t.get('stressed') if isinstance(t.get('stressed'),dict) else t
            return {'test':key,'verdict':t.get('verdict'),'n':s.get('n') or t.get('marketDays') or t.get('trades'),'mean':s.get('mean') or t.get('avgPerMarketDay') or t.get('meanPerDollar'),'lower95':s.get('lower95') or t.get('lower95')}
    w=x.get('wallet') if isinstance(x.get('wallet'),dict) else None
    if w:return {'equity':w.get('equity'),'pnlUsd':w.get('pnlUsd'),'trades':w.get('trades')}
    return None

def main():
    hours=float(sys.argv[sys.argv.index('--hours')+1]) if '--hours' in sys.argv else 24.
    new,old=snapshots(hours)
    if not new:print('NO STATUS FOUND');return
    head=git('rev-parse','origin/main').strip()
    f,rows=review(new,old,head)
    print(f"# Daily review ({new.get('atUtc')} UTC, revision {new.get('revision')}, window {hours:.0f} h)\n")
    print('## Findings\n'+('\n'.join(f"- **{x['severity']} {x['kind']}**: {x['what']}" for x in f) or '- none'))
    print('\n## Lanes\n| lane | errors | headline now | 24 h ago |\n|---|---|---|---|')
    for r in rows:print(f"| {r['lane']} | {r['errors']} | {json.dumps(r['headline'])} | {json.dumps(r['before'])} |")
    print('\n```json\n'+json.dumps({'findings':f,'lanes':rows},default=str)+'\n```')

if __name__=='__main__':main()
