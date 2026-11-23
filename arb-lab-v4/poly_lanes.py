"""Which strategy lanes are retired, and why (owner decision 2026-11-23: "remove failed strategies").

A retired lane does not start, whatever the server environment says, and drops off the dashboards and the status
report. Its code, tests and saved history stay in the repo, so a result can still be audited. To bring one back,
remove it here (a deliberate code change, reviewed like any other). Final results are in ops/experiments.md.
"""
RETIRED={
    'maker':     {'code':'M1','why':'slow maker on 5m/15m crypto: about -10c per $ (adverse selection), failed its test'},
    'fastmaker': {'code':'M2','why':'fast maker: about -17c per $, failed its test'},
    'xmarket':   {'code':'X1','why':'Deribit options gap: about -33% per event, failed its test'},
    'pairacc':   {'code':'R1','why':'pair accumulator (GoodFallen replica) and its $200 wallet: about -25c per $; wallet stopped at its $120 floor'},
    'eventmaker':{'code':'M3','why':'slow-market maker: failed its clean re-test (235 market-days, bad case -$0.13 per market-day)'},
    'jev':       {'code':'H2','why':'Jev LLM forecaster: about -32c per $, worse than the price; its OpenRouter credit is exhausted (402)'},
    'threshold': {'code':'TH','why':'old fixed-threshold lane: lost money, superseded'},
}
RETIRED_AT='2026-11-23'
import os
def retired(name):
    # POLY_LANES_TESTING=1 (set only by conftest.py) lets the retired lanes' own unit tests keep exercising their code
    return name in RETIRED and os.getenv('POLY_LANES_TESTING')!='1'
def summary():return {'at':RETIRED_AT,'lanes':{k:dict(v) for k,v in RETIRED.items()}}
