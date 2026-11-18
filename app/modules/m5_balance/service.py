"""M5 仓批平衡：按收货事实核算，已入仓车次从台账累计。

同一车次重试只更新原入仓流水，不会在仓批里出现第二次。
"""
from decimal import Decimal as D

from app import ledger
from .rules import balances


def execute(ctx):
    current = ctx['records']
    trips = [r.get('trip') or r.get('id', '') for r in current]
    rows = list(current)
    conn = ctx.get('_conn')
    if conn is not None:
        # 本次重算的车次（含已退收）从台账中排除，避免重复累计或残留入仓。
        exclude = trips + list(ctx.get('_retracted') or [])
        for row in ledger.bin_totals(conn, exclude_trips=exclude):
            rows.append({**row, 'count': row['count'], '_ledger': True})
    ctx['balances'] = balances(rows, ctx['rules']['standard_moisture'])
    tolerance = ctx['rules']['bin_tolerance']
    for row in ctx['balances']:
        row['state'] = '通过' if abs(row['delta_kg']) <= tolerance else '差异'
    ctx['stages'].append({'title': '仓批守恒', 'rows': ctx['balances'], 'metrics': {
        '仓批': len(ctx['balances']),
        '干物质 kg': str(sum((r['dry_kg'] for r in ctx['balances']), D(0))),
        '差异仓': sum(r['state'] != '通过' for r in ctx['balances']),
    }})
    return ctx
