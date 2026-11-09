"""Polymarket Data API v2 adapter (v1 was retired on 2026-10-24 and now answers 410 Gone).

v2 lives under /v2, wraps rows in {"data": [...], "pagination": {"next_cursor": ...}}, returns snake_case fields,
filters markets with `condition` (<= 20 ids) instead of `market`, and pages with `cursor` (offset is rejected).
Request parameters accept camelCase or snake_case. These helpers call v2 and hand back rows in the v1 field names
the rest of the bot already uses (proxyWallet, conditionId, asset, outcomeIndex, transactionHash, userName, ...).
Docs: https://docs.polymarket.com/migrate/data-api-v1-to-v2
"""
from __future__ import annotations
import asyncio

DATA='https://data-api.polymarket.com'
SPECIAL={'token_id':'asset'}
def camel(k):
    parts=str(k).split('_');return parts[0]+''.join(p[:1].upper()+p[1:] for p in parts[1:])
def v1(row):
    """One v2 row -> v1 field names (keeps every field; token_id is exposed as asset)."""
    if not isinstance(row,dict):return row
    return {SPECIAL.get(k,camel(k)):v for k,v in row.items()}
def rows_of(d):
    if isinstance(d,list):return d
    return (d or {}).get('data') or [] if isinstance(d,dict) else []
def next_cursor(d):
    return ((d or {}).get('pagination') or {}).get('next_cursor') if isinstance(d,dict) else None

async def paged(get,path,params,max_rows,pause=0.25):
    out=[];cursor=None
    while len(out)<max_rows:
        p=dict(params)
        if cursor:p['cursor']=cursor
        d=await get(DATA+path,p);rows=rows_of(d);out+=[v1(r) for r in rows];cursor=next_cursor(d)
        if not rows or not cursor:break
        await asyncio.sleep(pause)
    return out[:max_rows]

async def trades(get,*,market=None,user=None,limit=1000,taker_only=True,max_rows=None,start=None,end=None,pause=0.25):
    """Trades for a market (condition id) and/or a user, newest first, in v1 field names."""
    p={'limit':min(int(limit),1000),'taker_only':'true' if taker_only else 'false'}
    if market:p['condition']=market
    if user:p['user']=user
    if user and start is not None:p['start']=int(start)
    if user and end is not None:p['end']=int(end)
    return await paged(get,'/v2/trades',p,max_rows or limit,pause)

async def activity(get,*,user,type=None,limit=500,max_rows=None):
    p={'user':user,'limit':min(int(limit),1000)}
    if type:p['type']=type
    return await paged(get,'/v2/activity',p,max_rows or limit)

async def leaderboard(get,**params):
    d=await get(DATA+'/v2/leaderboard',params);return [v1(r) for r in rows_of(d)]
