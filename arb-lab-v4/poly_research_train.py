"""Offline or child-process training; historical STATE rows are never fake labels."""
import argparse,json,os,zipfile
from pathlib import Path
from poly_research_model import train

def load_rows(paths):
    rows=[]
    def accept(record):
        if record.get('kind')=='SHADOW_OUTCOME':rows.append(record['row'])
        elif 'netReturnPerShare' in record:rows.append(record)
    for path in paths:
        p=Path(path)
        if zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                for name in z.namelist():
                    if name.endswith('.ndjson'):
                        with z.open(name) as stream:
                            for line in stream:accept(json.loads(line))
        elif p.suffix=='.json':
            data=json.loads(p.read_text())
            for r in data if isinstance(data,list) else data.get('rows',[]):accept(r)
        else:
            with p.open() as stream:
                for line in stream:accept(json.loads(line))
    return rows

def main():
    p=argparse.ArgumentParser();p.add_argument('inputs',nargs='+');p.add_argument('--output',required=True);p.add_argument('--xgboost',action='store_true');a=p.parse_args()
    try:os.nice(10)
    except (AttributeError,OSError):pass
    artifact=train(load_rows(a.inputs),a.xgboost);target=Path(a.output);target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_suffix('.tmp');temp.write_text(json.dumps(artifact,indent=2,allow_nan=False));temp.replace(target)
    print(json.dumps(artifact['report']))
if __name__=='__main__':main()
