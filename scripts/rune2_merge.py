"""把蒸馏数据 + 自写数据合成训练集：调配比 → 课程排序 → 审计 → 落盘"""
import json, os, random, re, sys
from collections import Counter, defaultdict

BASE = os.path.expanduser('~/LycheeAI')
DIST = f'{BASE}/rune2_distill_raw.jsonl'
FIX = f'{BASE}/rune2_distill_fix.jsonl'
V2 = f'{BASE}/rune2_distill_v2.jsonl'
OUT = f'{BASE}/rune2_train.jsonl'
TOK_DIR = f'{BASE}/LFM3-1.7B'
MAX_LEN = 1024

sys.path.insert(0, BASE)
import rune2_fill

# 目标量：关键类（agent/refuse/multiturn/identity）尽量保留蒸馏数据，
# 无关紧要的（code/chat）用自写补齐；靠提高总量来均衡，而不是砍关键数据
TARGET = {'agent_call': 219, 'agent_ask': 90, 'agent_notool': 72,
          'refuse': 227, 'multiturn': 151, 'identity': 112, 'code': 200, 'chat': 129}


def bad_call(r):
    """list 工具被空着调用 = 模型没拿到路径还硬调，后面必然是编数据。
    判据必须是「必填 string 参数」，光有 string 不够（sort_by 也是 string）。"""
    if r['category'] != 'call':
        return False
    lt = r['tools'][0]['function']['name'] if r.get('tools') else None
    for m in r['messages']:
        for tc in (m.get('tool_calls') or []):
            if tc['name'] == lt and not (tc.get('arguments') or {}):
                return True
    return False


def key_of(r):
    c = r['category']
    if c in ('call', 'ask', 'notool'):
        return 'agent'
    return c


def sub_of(r):
    c = r['category']
    if c in ('call', 'ask', 'notool'):
        return f'agent_{c}'
    return c


def bad_mt(r):
    """多轮里回答「我不知道/上一轮是编的」= 历史不自洽，模型识破了。
    这类留着会教模型否认自己的上下文，实测占 72%。"""
    if r['category'] != 'multiturn':
        return False
    a = [m for m in r['messages'] if m['role'] == 'assistant'][-1]['content']
    return any(k in a for k in (
        '不知道', '没提到', '没有具体', '不清楚', '没说明', '凭空', '不算数',
        '没有依据', '不该', '我没读', '上一条', '刚才那句', '重新说',
        '没法确定', '无法判断', '没实际'))


def main():
    # 蒸馏数据：call/multiturn 用重跑过的 fix 版（修掉了空调用和「没查就下结论」的历史）
    base = [json.loads(l) for l in open(DIST, encoding='utf-8')
            if json.loads(l)['category'] not in ('call', 'multiturn')]
    fix = [json.loads(l) for l in open(FIX, encoding='utf-8')]
    n0 = len(fix)
    fix = [r for r in fix if not bad_call(r)]
    n1 = len(fix)
    fix = [r for r in fix if not bad_mt(r)]
    print(f'fix {n0} 条 → 剔空参数调用 {n0-n1} 条 → 剔不自信多轮 {n1-len(fix)} 条 → {len(fix)} 条')
    v2 = [json.loads(l) for l in open(V2, encoding='utf-8')]
    nv = len(v2); v2 = [r for r in v2 if not bad_call(r)]
    print(f'v2 {nv} 条 → 剔空参数调用 {nv-len(v2)} 条 → {len(v2)} 条')
    rows = base + fix + v2 + rune2_fill.build_fill()
    print(f'原始合计 {len(rows)} 条：{dict(Counter(sub_of(r) for r in rows))}')

    rng = random.Random(20261104)
    pool = defaultdict(list)
    for r in rows:
        pool[sub_of(r)].append(r)

    picked = []
    for k, n in TARGET.items():
        v = pool[k][:]
        rng.shuffle(v)
        take = v[:n]
        picked += take
        print(f'  {k:14s} 有 {len(v):>3}  取 {len(take):>3}')

    # 课程轮转：小组先耗尽 → 稀缺类型必然落在训练前期
    buckets = defaultdict(list)
    for r in picked:
        buckets[sub_of(r)].append(r)
    for v in buckets.values():
        rng.shuffle(v)
    ordered = []
    while any(buckets.values()):
        for k in sorted(buckets):
            if buckets[k]:
                ordered.append(buckets[k].pop(0))
    picked = ordered

    # ── 审计 ──
    print('\n审计')
    bad_args, bad_types, leak = [], [], []
    T = {'string': str, 'object': dict, 'array': list, 'number': (int, float), 'boolean': bool}

    def walk(v):
        if isinstance(v, dict):
            yield v
            for x in v.values():
                yield from walk(x)
        elif isinstance(v, list):
            for x in v:
                yield from walk(x)

    for r in picked:
        decl = {}
        for t in (r.get('tools') or []):
            f = t['function']
            decl[f['name']] = {k: (v or {}).get('type')
                               for k, v in ((f.get('parameters') or {}).get('properties') or {}).items()}
        for m in r['messages']:
            for tc in (m.get('tool_calls') or []):
                for k, v in (tc.get('arguments') or {}).items():
                    if k not in decl.get(tc['name'], {}):
                        bad_args.append((tc['name'], k))
                    else:
                        want = decl[tc['name']][k]
                        ok = T.get(want)
                        if ok and not isinstance(v, ok):
                            bad_types.append((tc['name'], k, want, type(v).__name__))
        blob = json.dumps(r, ensure_ascii=False).lower()
        if any(w in blob for w in ('deepseek', '深度求索', 'openai')):
            leak.append(r['category'])

    print(f'  参数名越界 {len(bad_args)} 处' + (' ✓' if not bad_args else f' → {bad_args[:3]}'))
    print(f'  参数类型不符 {len(bad_types)} 处' + (' ✓' if not bad_types else f' → {bad_types[:3]}'))
    print(f'  含上游模型名 {len(leak)} 条' + (' ✓' if not leak else f' → {leak[:5]}'))

    # ── token 长度（用 LFM3 自己的 tokenizer）──
    try:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(TOK_DIR, trust_remote_code=True)
        over, lens = [], []
        for r in picked:
            try:
                t = tok.apply_chat_template(r['messages'], tools=r.get('tools'),
                                            tokenize=False, add_generation_prompt=False)
                n = len(tok(t, add_special_tokens=False)['input_ids'])
            except Exception:
                continue
            lens.append(n)
            if n > MAX_LEN:
                over.append((r['category'], n))
        if lens:
            lens.sort()
            print(f'  token 中位 {lens[len(lens)//2]}  范围 {lens[0]}~{lens[-1]}  '
                  f'超 {MAX_LEN} 的 {len(over)} 条' + ('' if not over else f' → {over[:3]}'))
    except Exception as e:
        print(f'  tokenizer 不可用（{type(e).__name__}），跳过长度检查')

    with open(OUT, 'w', encoding='utf-8') as f:
        for r in picked:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    dist = Counter(key_of(r) for r in picked)
    n = len(picked)
    print(f'\n写出 {n} 条 → {OUT}')
    for k in sorted(dist):
        print(f'  {k:10s} {dist[k]:>4}  {dist[k]/n*100:>4.1f}%')
    print(f'  文件 {os.path.getsize(OUT)/1024:.0f} KB')


if __name__ == '__main__':
    main()
