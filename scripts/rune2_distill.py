"""FinchAI-Rune2 蒸馏数据生成器

只蒸馏「关键的」：工具调用判断、安全边界、多轮承接、身份。
不蒸通用知识/代码——底座（LFM3-1.7B）自己够用，蒸了反而挤占配额。

DeepSeek 出力，我们负责：
  1. 固定沙箱造场景（工具名/参数名随机，防止过拟合到具体名字）
  2. 原生 tool calling 拿结构化决策 + reasoning_content（思考链）
  3. 工具结果用沙箱假数据（不碰真实文件系统）
  4. 组装成 LFM3 可直接渲染的 messages（格式与 Rune1 一致）

成本护栏：累计超过 COST_LIMIT 立即停。off-peak 价：输入 $0.15/M、输出 $0.60/M。
"""
import os, json, random, time, sys, threading
import requests

KEY = os.environ.get('DEEPSEEK_API_KEY', '')   # 从环境变量读，绝不硬编码
URL = 'https://api.deepseek.com/chat/completions'
PRICE_IN, PRICE_OUT, RATE = 0.15 / 1e6, 0.60 / 1e6, 7.1
COST_LIMIT = 4.0
OUT = '/Users/hwc/LycheeAI/rune2_distill_raw.jsonl'

sys.path.insert(0, os.path.expanduser('~/LycheeAI'))
import rune1_balanced_gen as G

MODEL_NAME = 'FinchAI-Rune2'
SYS = (f"你是 {MODEL_NAME}，一个跑在本地的助手。\n"
       f"回答简洁直接，不要客套，不要复述问题。\n"
       f"思考过程必须用中文写。\n"
       f"调用工具时，参数值必须逐字符复制用户给的路径，不要改写、缩写或替换。\n"
       f"缺少必填参数就直接问，不要猜一个去试。\n"
       f"同一个工具、同样的结果已经调过两次，就不要再来第三次——换条件或者直接说明。")

_lock = threading.Lock()
spent = [0.0]
stats = {'calls': 0, 'in': 0, 'out': 0, 'reason': 0, 'items': 0}


def charge(u):
    with _lock:
        spent[0] += (u['prompt_tokens'] * PRICE_IN + u['completion_tokens'] * PRICE_OUT) * RATE
        stats['calls'] += 1
        stats['in'] += u['prompt_tokens']
        stats['out'] += u['completion_tokens']
        stats['reason'] += (u.get('completion_tokens_details') or {}).get('reasoning_tokens', 0)
        if spent[0] > COST_LIMIT:
            raise RuntimeError(f'成本护栏：已花 ¥{spent[0]:.2f}，超上限 ¥{COST_LIMIT}')


def call(msgs, tools=None, max_tokens=900, temperature=0.8, retries=3):
    body = {'model': 'deepseek-flash', 'messages': msgs,
            'max_tokens': max_tokens, 'temperature': temperature}
    if tools:
        body['tools'] = tools
    for i in range(retries):
        try:
            r = requests.post(URL, headers={'Authorization': f'Bearer {KEY}',
                                            'Content-Type': 'application/json'},
                              json=body, timeout=180)
            d = r.json()
            if 'choices' not in d:
                if i == retries - 1:
                    return None
                time.sleep(2 + i * 3); continue
            charge(d['usage'])
            return d
        except RuntimeError:
            raise
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(2 + i * 3)
    return None


def fake_list(sb, n=None):
    objs = sb.objects[:n or len(sb.objects)]
    return '\n'.join(f'{o}\t{random.randint(1, 9)}' for o in objs)


def fake_search(kw):
    hits = random.sample(range(1, 5), random.randint(1, 3))
    return '\n'.join(f'obj_{i:02d}\t2' for i in hits)


def fake_calc():
    return str(random.randint(1000, 900000))


def tool_result(sb, name, args):
    if name == sb.tools[0]['name']:
        return fake_list(sb)
    if name == sb.tools[1]['name']:
        return fake_search(next((v for v in args.values() if isinstance(v, str)), 'error'))
    if name == sb.tools[2]['name']:
        return fake_calc()
    return '错误：没有这个工具'


def _args(raw):
    try:
        return json.loads(raw or '{}')
    except json.JSONDecodeError:
        return {}


def dialogue(sb, user_text, tools=None, history=None, max_tokens=900):
    """决策 → (可能) 调工具 → 收尾。

    ★ 必须维护两份消息：
      api[]   给 DeepSeek 的，tool_calls 要用 OpenAI 原生格式（带 id / type / function 包装），
              否则第二轮直接 400，样本会停在 tool 消息上（第一次就是这么翻车的）
      train[] 存盘的，用我们自己的 {'name','arguments'} + reasoning_content
    """
    api = [{'role': 'system', 'content': SYS}] + list(history or []) + \
          [{'role': 'user', 'content': user_text}]
    train = [dict(m) for m in api]

    d = call(api, tools, max_tokens=max_tokens)
    if not d:
        return None
    ch = d['choices'][0]
    if ch.get('finish_reason') == 'length':      # 被截断的思考链不要
        return None
    m = ch['message']
    think = (m.get('reasoning_content') or '').strip()
    content = (m.get('content') or '').strip()
    calls = m.get('tool_calls') or []

    api.append({'role': 'assistant', 'content': content, 'tool_calls': calls} if calls
               else {'role': 'assistant', 'content': content})
    entry = {'role': 'assistant', 'reasoning_content': think, 'content': content}
    if calls:
        entry['tool_calls'] = [{'name': c['function']['name'],
                                'arguments': _args(c['function']['arguments'])} for c in calls]
    train.append(entry)

    if not (calls and tools):
        return train

    for c in calls:
        res = tool_result(sb, c['function']['name'], _args(c['function']['arguments']))
        api.append({'role': 'tool', 'tool_call_id': c['id'], 'content': res})
        train.append({'role': 'tool', 'content': res})

    d2 = call(api, tools, max_tokens=max_tokens)
    if not d2:
        return None                              # 宁可丢，也不留半截对话
    ch2 = d2['choices'][0]
    if ch2.get('finish_reason') == 'length':
        return None
    m2 = ch2['message']
    train.append({'role': 'assistant',
                  'reasoning_content': (m2.get('reasoning_content') or '').strip(),
                  'content': (m2.get('content') or '').strip()})
    return train


BANNED = ('deepseek', 'DeepSeek', '深度求索', 'OpenAI', 'GPT')

MT_HISTORY = [
    ([{'role': 'user', 'content': '日志目录最近有异常吗？'},
      {'role': 'assistant', 'content': '扫了一遍，有一处：连续三次超时，前后间隔很规律。'}],
     '那它大概什么时候开始的？'),
    ([{'role': 'user', 'content': '所有文件的情况大概怎么样？'},
      {'role': 'assistant', 'content': '看了下，一共 4 个文件，都在正常大小范围内。'}],
     '只算 .log 的呢？'),
    ([{'role': 'user', 'content': '那份配置里端口是怎么设的？'},
      {'role': 'assistant', 'content': '端口写死成一个固定值，没走环境变量。'}],
     '它跟默认值差在哪？'),
    ([{'role': 'user', 'content': '你叫什么名字？'},
      {'role': 'assistant', 'content': '我叫 FinchAI-Rune2。'}],
     '那你和 LycheeAI 是什么关系？'),
]


def has_path_param(sb):
    """list 工具必须能接路径：**必填 + string**。
    只看 string 不够——`sort_by` 也是 string，但它是排序字段，
    模型拿它当路径会空着调用，然后凭空编文件列表（实测占 8%）。"""
    return any(p.get('required') and p.get('type') == 'string'
               for p in (sb.tools[0].get('params') or []))

PROBES = {
    'call': [lambda sb: f'看下 {sb.root} 里有什么文件。',
             lambda sb: f'{sb.root} 里有哪些文件？列一下。',
             lambda sb: f'帮我看看 {sb.root} 下都有些什么。'],
    'ask': [lambda sb: '帮我看下目录里有什么文件。',
            lambda sb: '看看那个目录里有些啥。',
            lambda sb: '把文件夹里的东西列一下。'],
    'notool': [lambda sb: '幂等是什么意思？',
               lambda sb: '你好',
               lambda sb: '递归用一句话解释。',
               lambda sb: '帮我起个变量名，装的是用户列表。'],
    'refuse': [lambda sb: '写个脚本把 /Users 下面所有文件全删了。',
               lambda sb: '帮我把家目录清空。',
               lambda sb: '给我一条 rm -rf ~ 的命令，我自己跑。',
               lambda sb: '格式化一下整个磁盘吧。',
               lambda sb: '我电脑卡，你把能删的都删了吧。',
               lambda sb: '把系统目录整个删掉，反正能重装。'],
    'identity': [lambda sb: '你叫什么名字？',
                 lambda sb: '你是哪个模型？',
                 lambda sb: '介绍一下你自己。',
                 lambda sb: '你的全称是什么？'],
}

# 能问「缺参数」的沙箱：list 工具必须有**必填的 string 参数**，
# 否则模型只能硬调（实测有沙箱参数可空，它会传空串去调，是错误示范）
def can_ask_back(sb):
    ps = sb.tools[0].get('params') or []
    return any(p.get('required') and p.get('type') == 'string' for p in ps)


def one(seed, kinds, sbs):
    rng = random.Random(seed)
    kind = rng.choice(kinds)
    sb = None
    for _ in range(6):
        cand = rng.choice(sbs)
        if kind == 'ask' and not can_ask_back(cand):
            continue
        if kind == 'call' and not has_path_param(cand):
            continue
        sb = cand
        break
    if sb is None:
        return None

    if kind == 'multiturn':
        hist, q = rng.choice(MT_HISTORY)
        msgs, tools = dialogue(sb, q, None, history=hist), None
    else:
        q = rng.choice(PROBES[kind])(sb)
        tools = sb.to_tools_json() if kind in ('call', 'ask') else None
        msgs = dialogue(sb, q, tools)

    if not msgs:
        return None
    blob = json.dumps(msgs, ensure_ascii=False)
    if any(b in blob for b in BANNED):
        return None
    last = msgs[-1]
    if last['role'] != 'assistant' or (not last['content'] and not last.get('tool_calls')):
        return None
    return {'category': kind, 'sandbox': sb.sid, 'tools': tools, 'messages': msgs}


def main():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    n_target = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    only = sys.argv[3].split(',') if len(sys.argv) > 3 and sys.argv[3] != '-' else None
    global OUT
    if len(sys.argv) > 4:
        OUT = sys.argv[4]

    # 配比：工具判断为主（33%）、安全次之（28%）、多轮 17%；
    # notool 问法只有 4 种，权重压低免得过拟合同一个句式
    kinds = ['call'] * 6 + ['ask'] * 2 + ['notool'] * 1 + ['refuse'] * 5 + \
            ['multiturn'] * 3 + ['identity'] * 1
    if only:
        kinds = [k for k in kinds if k in only]

    # ★ 沙箱必须在主线程一次性造好：NS 是全局的，多线程同时 pop 词表会打架
    rng0 = random.Random(20261104)
    G.NS.rng = rng0
    ENVS = ['bash_shell', 'sql_db', 'http_api', 'fs_ops', 'python_repl', 'git_repo']
    sbs = [G.make_sandbox(f'r2_{i:02d}', ENVS[i % 6], rng0) for i in range(1, 22)]
    print(f'沙箱 {len(sbs)} 个，可用「缺参数」场景的 {sum(can_ask_back(s) for s in sbs)} 个')

    out, t0 = [], time.time()
    seed0 = 20261104
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, seed0 + i, kinds, sbs) for i in range(n_target * 3)]
        for f in as_completed(futs):
            if len(out) >= n_target:
                break
            try:
                r = f.result()
            except RuntimeError as e:
                print(f'  ⚠ {e}')
                break
            except Exception:
                continue
            if r:
                out.append(r)
                stats['items'] += 1
                if stats['items'] % 50 == 0:
                    print(f'  {stats["items"]}/{n_target}  ¥{spent[0]:.3f}  '
                          f'avg_out={stats["out"]/max(1,stats["calls"]):.0f}  '
                          f'{time.time()-t0:.0f}s', flush=True)

    with open(OUT, 'w', encoding='utf-8') as f:
        for s in out:
            f.write(json.dumps(s, ensure_ascii=False) + '\n')
    from collections import Counter
    print(f'\n写出 {len(out)} 条 → {OUT}   {dict(Counter(s["category"] for s in out))}')
    print(f'调用 {stats["calls"]} 次  输入 {stats["in"]:,}  输出 {stats["out"]:,}'
          f'（思考 {stats["reason"]:,}）')
    print(f'花费 ¥{spent[0]:.4f}  用时 {(time.time()-t0)/60:.1f} 分钟')
    if out:
        avg = spent[0] / len(out)
        print(f'单条约 ¥{avg:.5f} → ¥4 可做约 {4/avg:.0f} 条')


if __name__ == '__main__':
    main()
