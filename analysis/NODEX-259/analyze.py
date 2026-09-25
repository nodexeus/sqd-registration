"""Reproduce network APR comparisons from the frozen GraphQL capture.

Dependencies: numpy scipy matplotlib. All payouts are worker rewards; delegator
rewards remain separate. Calendar windows select commitment period end times,
not wallet claims or transaction times. Missing payouts contribute zero.
"""
import collections
import csv
import datetime as dt
import json
from pathlib import Path
import statistics
import numpy as np
from scipy.stats import pearsonr, spearmanr
from collect import OUT, read

ROOT = Path(__file__).parent
E18 = 10**18
DAY = 86400

def ts(s):
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()

def all_pages(entity):
    paths = sorted(OUT.glob(entity + '-*.json.gz'))
    assert paths and read(paths[-1]) == [], 'Collection incomplete'
    rows = [r for p in paths for r in read(p)]
    assert len({r['id'] for r in rows}) == len(rows)
    return rows

def correlations(rows, field, apr='apr'):
    pairs = [(r[apr], r[field]) for r in rows if r[apr] is not None and r[field] is not None]
    if len(pairs) < 3:
        return {'n':len(pairs)}
    a,b = np.array(pairs).T
    inversions = comparable = 0
    for i in range(len(a)):
        diff_a = a[i+1:] - a[i]
        diff_b = b[i+1:] - b[i]
        comparable += int(np.count_nonzero((diff_a != 0) & (diff_b != 0)))
        inversions += int(np.count_nonzero(diff_a * diff_b < 0))
    return {'n':len(pairs), 'pearson':float(pearsonr(a,b).statistic),
            'spearman':float(spearmanr(a,b).statistic),
            'reversed_pairs':inversions, 'comparable_pairs':comparable,
            'reversed_pct': 100*inversions/comparable if comparable else None}

def main():
    meta = json.loads((OUT/'metadata.json').read_text())
    workers = all_pages('workers')
    commits = all_pages('commitments')
    assert len(workers) == meta['counts']['workersConnection']['totalCount']
    lookup = {w['id']:w for w in workers}
    # Complete UTC days avoid comparing today's incomplete rotation to full days.
    end_dt = dt.datetime.fromisoformat(meta['captured_at'].replace('Z','+00:00')).replace(hour=0,minute=0,second=0,microsecond=0)
    end = end_dt.timestamp()
    hist = collections.defaultdict(list)
    for c in commits:
        a,b = ts(c['from']),ts(c['to'])
        assert b > a
        assert len({p['workerId'] for p in c['recipients']}) == len(c['recipients'])
        for p in c['recipients']:
            assert p['workerId'] in lookup
            hist[p['workerId']].append((a,b,int(p['workerReward']),int(p['stakerReward']),p['workerApr'],c['id']))
    rows=[]
    for w in workers:
        bond=int(w['bond'])/E18
        life=(int(w['claimedReward'])+int(w['claimableReward']))/E18
        age=(end-ts(w['createdAt']))/DAY
        r={k:w[k] for k in ('id','peerId','name','ownerId','status','apr','online','jailed','createdAt','uptime90Days')}
        r.update(bond_sqd=bond,delegated_sqd=int(w['totalDelegation'])/E18,
                 capped_delegated_sqd=int(w['capedDelegation'])/E18,
                 age_days=age,lifetime_sqd=life,display_apr=0 if w['status']=='ACTIVE' and not w['jailed'] and not w['online'] else w['apr'])
        h=sorted(hist[w['id']],key=lambda x:x[1])
        r['last_period_apr']=h[-1][4] if h else None
        r['last_period_sqd']=h[-1][2]/E18 if h else None
        r['last_period_end']=dt.datetime.fromtimestamp(h[-1][1],dt.timezone.utc).isoformat() if h else None
        for days in (7,30,90):
            subset=[p for p in h if end-days*DAY < p[1] <= end]
            amount=sum(p[2] for p in subset)
            positive=[p for p in subset if p[2]>0]
            r[f'sqd_{days}d']=amount/E18
            r[f'staker_sqd_{days}d']=sum(p[3] for p in subset)/E18
            r[f'paid_{days}d']=len(positive)
            r[f'zero_{days}d']=len(subset)-len(positive)
            r[f'periods_{days}d']=len(subset)
            r[f'covered_days_{days}d']=sum(p[1]-max(p[0],ts(w['createdAt'])) for p in subset)/DAY
            r[f'positive_apr_{days}d']=statistics.mean(p[4] for p in positive) if positive else None
            r[f'realized_apr_{days}d']=amount/E18/bond*365/days*100 if bond>0 else None
        rows.append(r)
    rows.sort(key=lambda r:int(r['id']))
    with (ROOT/'workers.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]),lineterminator="\n");writer.writeheader();writer.writerows(rows)
    active=[r for r in rows if r['status']=='ACTIVE' and r['apr'] is not None]
    mature=[r for r in active if r['age_days']>=90]
    cohorts={'all_active_with_apr':active,'active_age_at_least_90d':mature,
             'active_online_age_at_least_90d':[r for r in mature if r['online'] and not r['jailed']],
             'active_age90_paid_at_least_260_times':[r for r in mature if r['paid_90d']>=260]}
    summary={'captured_at':meta['captured_at'],'window_end':end_dt.isoformat(),'workers':len(rows),
             'status_counts':dict(collections.Counter(r['status'] for r in rows)),
             'commitments':len(commits),'recipient_records':sum(len(c['recipients']) for c in commits),
             'bonds_active':dict(collections.Counter(r['bond_sqd'] for r in active)),
             'cohorts':{},'top_apr':sorted(active,key=lambda r:r['apr'],reverse=True)[:10],
             'top_30d':sorted(active,key=lambda r:r['sqd_30d'],reverse=True)[:10]}
    for name,group in cohorts.items():
        summary['cohorts'][name]={field:correlations(group,field) for field in ['lifetime_sqd','sqd_7d','sqd_30d','sqd_90d']}
        summary['cohorts'][name]['display_vs_30d']=correlations(group,'sqd_30d','display_apr')
        summary['cohorts'][name]['positive90_vs_90d']=correlations(group,'sqd_90d','positive_apr_90d')
    # Same payout count removes the number-of-paid-periods effect.
    common=collections.Counter(r['paid_90d'] for r in mature).most_common(1)[0][0]
    equal=[r for r in mature if r['paid_90d']==common]
    summary['same_paid_count']={'count':common,'correlation':correlations(equal,'sqd_90d')}
    summary['network_totals']={str(d):{'worker_sqd':sum(r[f'sqd_{d}d'] for r in rows),'staker_sqd':sum(r[f'staker_sqd_{d}d'] for r in rows)} for d in (7,30,90)}
    summary['paid_count_distribution']=dict(sorted(collections.Counter(r['paid_90d'] for r in mature).items()))
    # Independent source check: exact integer amounts, both reward classes.
    chain=read(OUT/'chain.json.gz')
    matches=0;recipients=0
    by_end={c['toBlock']:c for c in commits}
    for event in chain['events']:
        if event['toBlock'] not in by_end:
            continue
        c=by_end[event['toBlock']]
        assert c['fromBlock']==event['toBlock']+1-(event['toBlock']-event['fromBlock']+1)*4
        actual={p['workerId']:(p['workerReward'],p['stakerReward']) for p in event['recipients']}
        indexed={p['workerId']:(p['workerReward'],p['stakerReward']) for p in c['recipients']}
        assert actual==indexed
        matches+=1;recipients+=len(actual)
    assert matches>0
    summary['chain_check']={'events_captured':len(chain['events']),'events_matched':matches,'recipient_pairs_matched':recipients,'start_block':chain['startBlock'],'end_block':chain['endBlock']}
    (ROOT/'summary.json').write_text(json.dumps(summary,indent=2))
    for name,stats in summary['cohorts'].items():
        print(name,json.dumps(stats))
    print('counts',summary['status_counts'],len(commits),summary['recipient_records'])
    print('chain',summary['chain_check'])
    print('same_count',summary['same_paid_count'])
    print('TOP APR')
    for r in summary['top_apr'][:6]:print(r['id'],r['name'],r['apr'],r['sqd_30d'],r['sqd_90d'],r['paid_90d'],r['age_days'])
    print('TOP MONTH')
    for r in summary['top_30d'][:6]:print(r['id'],r['name'],r['apr'],r['sqd_30d'],r['sqd_90d'],r['paid_90d'],r['age_days'])
    plot(active,mature)

def plot(active,mature):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False})
    fig,axs=plt.subplots(1,3,figsize=(15,4.8),layout='constrained')
    for ax,field,title,group in zip(axs,['lifetime_sqd','sqd_30d','sqd_90d'],['Lifetime rewards · active workers','Last 30 days · workers ≥90 days old','Last 90 days · workers ≥90 days old'],[active,mature,mature]):
        colors=[min(r['paid_90d'],270) for r in group]
        sc=ax.scatter([r['apr'] for r in group],[r[field] for r in group],c=colors,cmap='viridis',s=13,alpha=.65,vmin=0,vmax=270)
        ax.set(title=title,xlabel='GraphQL worker APR (%)',ylabel='Worker rewards (SQD)')
        ax.grid(alpha=.15)
    fig.colorbar(sc,ax=axs,shrink=.8,label='Positive payouts in 90 days')
    fig.suptitle('SQD worker APR versus actual distributed rewards',fontsize=16)
    fig.savefig(ROOT/'apr-vs-payouts.png',dpi=170)

if __name__=='__main__':main()
