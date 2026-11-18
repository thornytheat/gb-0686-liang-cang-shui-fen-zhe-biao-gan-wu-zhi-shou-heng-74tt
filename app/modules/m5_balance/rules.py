from decimal import Decimal as D
from collections import defaultdict


def balances(rows, std):
    """按仓汇总净重、干物质与折标重。

    输入可以是 Decimal，也可以是台账读出的字符串；统一转 Decimal 累加，
    保证微量累加不产生浮点误差。
    """
    bins = defaultdict(list)
    for row in rows:
        bins[row['bin']].append(row)
    result = []
    for name, group in sorted(bins.items()):
        net = sum((D(str(r['net_kg'])) for r in group), D(0))
        dry = sum((D(str(r['dry_kg'])) for r in group), D(0))
        standard = sum((D(str(r['standard_kg'])) for r in group), D(0))
        delta = dry - standard * (1 - std / D(100))
        result.append({'bin': name, 'count': len(group), 'net_kg': net, 'dry_kg': dry,
                       'standard_kg': standard, 'delta_kg': delta})
    return result
