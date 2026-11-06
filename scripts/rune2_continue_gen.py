"""FinchAI-Rune2 续训数据生成：专治多轮复读 / 极短输入 / 承接上文

三类，全部基于「上一轮已经答过」的上下文：
  short   —— 用户只回一个字（哦/嗯/好/？），正确做法是简短确认，不重复上文
  norepeat—— 上一轮答完 A，这一轮问别的，绝不能再说 A
  carry   —— 上一轮给了具体信息，追问要引用那个信息

★ API key 只从环境变量读，绝不落盘（上一版硬编码导致泄露）。
★ 成本不靠估算：调用前后各查一次余额，差额才是真花的钱。
"""
import os, json, time, sys, random, threading
import requests

KEY = os.environ.get('DEEPSEEK_API_KEY') or sys.exit('缺少环境变量 DEEPSEEK_API_KEY')
URL = 'https://api.deepseek.com/chat/completions'
BAL = 'https://api.deepseek.com/user/balance'
OUT = '/Users/hwc/LycheeAI/rune2_continue_raw.jsonl'
BUDGET = float(os.environ.get('BUDGET_CNY', '2.0'))      # 元

SYS = ("你是 FinchAI-Rune2，一个跑在本地的助手。回答简洁直接。\n"
       "**用户只回一两个字时，说明他在确认或收尾，用一两句话回应即可，"
       "绝对不要重复或重新解释上一条回复的内容。**\n"
       "针对当前这句话回答。")

_lock = threading.Lock()
stats = {'calls': 0, 'in': 0, 'out': 0, 'items': 0}


def balance():
    r = requests.get(BAL, headers={'Authorization': f'Bearer {KEY}'}, timeout=20)
    d = r.json()
    return float(d['balance_infos'][0]['total_balance'])


def call(msgs, max_tokens=300, temperature=0.7, retries=2):
    body = {'model': 'deepseek-flash', 'messages': msgs,
            'max_tokens': max_tokens, 'temperature': temperature}
    for i in range(retries):
        try:
            r = requests.post(URL, headers={'Authorization': f'Bearer {KEY}',
                                            'Content-Type': 'application/json'},
                              json=body, timeout=120)
            d = r.json()
            if 'choices' not in d:
                if i == retries - 1:
                    return None
                time.sleep(2)
                continue
            u = d['usage']
            with _lock:
                stats['calls'] += 1
                stats['in'] += u['prompt_tokens']
                stats['out'] += u['completion_tokens']
            return d
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(2)
    return None


# ── 场景池 ──
BASE_CASES = [
    ('short', [{'role': 'user', 'content': '递归是什么？'},
               {'role': 'assistant', 'content': '函数调用自身来拆解同类子问题，必须要有终止条件，否则栈溢出。'}],
     ['哦', '嗯', '好', '知道了', '这样啊', '收到']),
    ('short', [{'role': 'user', 'content': '说几个 emoji'},
               {'role': 'assistant', 'content': '😊 🥳 👍 🎉 📦 📄 💻 🖥️'}],
     ['哦', '嗯', '？', '好', '谢谢']),
    ('short', [{'role': 'user', 'content': '这个接口 QPS 能撑多少？'},
               {'role': 'assistant', 'content': '按现在的压测结果，单实例 800 左右，再高延迟会明显抬。'}],
     ['哦', '嗯', '行', '知道了']),
    ('norepeat', [{'role': 'user', 'content': '这个项目用什么语言写的？'},
                  {'role': 'assistant', 'content': 'Python，主要跑数据处理那部分。'}],
     ['那部署呢？', '有没有文档？', '测试覆盖率多少？', '谁在维护？']),
    ('norepeat', [{'role': 'user', 'content': '这台机器内存多大？'},
                  {'role': 'assistant', 'content': '16 GB，现在用了大概一半。'}],
     ['磁盘呢？', 'CPU 是什么型号？', '系统版本多少？', '能跑得起 7B 模型吗？']),
    ('carry', [{'role': 'user', 'content': '日志最近有异常吗？'},
               {'role': 'assistant', 'content': '有三条超时：10:02、10:05、10:08，都是调支付网关那次。'}],
     ['那什么时候开始的？', '间隔多久一次？', '是同一个下游吗？', '其他接口正常吗？']),
    ('carry', [{'role': 'user', 'content': '磁盘还剩多少？'},
               {'role': 'assistant', 'content': '/var 用了 92%，剩不到 5 GB，日志目录占了 60%。'}],
     ["哪个目录占得最多？", '现在还能撑多久？', '清理能腾多少？']),
    ('carry', [{'role': 'user', 'content': '接口延迟怎么样？'},
               {'role': 'assistant', 'content': '平均 120ms，P99 到 800ms，慢的集中在 /api/export。'}],
     ['P99 为什么这么高？', '其他接口呢？', '优化哪个先？']),
]

PROMPT = {
    'short': '用户只回了「{q}」。用一两句话简短回应，不要重复或解释刚才说的内容。',
    'norepeat': '承接上文继续回答「{q}」。只答新问题，不要复述上文已有的信息。',
    'carry': '基于上文回答「{q}」。要用上文里的具体信息，不要反问用户要路径或数据。',
}


def one(seed, max_tokens):
    rng = random.Random(seed)
    kind, hist, qs = rng.choice(BASE_CASES)
    q = rng.choice(qs)
    msgs = [{'role': 'system', 'content': SYS}] + hist + [
        {'role': 'user', 'content': q},
    ]
    d = call(msgs, max_tokens=max_tokens, temperature=0.7)
    if not d:
        return None
    m = d['choices'][0]['message']
    think = (m.get('reasoning_content') or '').strip()
    ans = (m.get('content') or '').strip()
    if not ans or len(ans) > 400:
        return None
    # 质量闸：short 类不能重复历史里的话
    if kind == 'short':
        prev = hist[-1]['content']
        if ans.strip() == prev.strip() or len(ans) > 120:
            return None
    return {'category': 'multiturn', 'kind': kind, 'tools': None,
            'messages': [{'role': 'system', 'content': SYS}] + hist + [
                {'role': 'user', 'content': q},
                {'role': 'assistant', 'reasoning_content': think, 'content': ans},
            ]}


def main():
    from concurrent.futures import ThreadPoolExecutor, as_completed
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    max_tokens = int(sys.argv[3]) if len(sys.argv) > 3 else 300

    b0 = balance()
    print(f'起始余额 ¥{b0:.2f}  目标 {n} 条  预算 ¥{BUDGET}', flush=True)

    out, t0 = [], time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, 20261106 + i, max_tokens) for i in range(n * 3)]
        for f in as_completed(futs):
            if len(out) >= n:
                break
            try:
                r = f.result()
            except Exception:
                continue
            if r:
                out.append(r)
                stats['items'] += 1
            if stats['items'] % 20 == 0 and stats['items']:
                print(f'  {stats["items"]}/{n}  {time.time()-t0:.0f}s', flush=True)

    b1 = balance()
    cost = b0 - b1

    with open(OUT, 'w', encoding='utf-8') as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    from collections import Counter
    print(f'\n写出 {len(out)} 条 → {OUT}')
    print(f'  分类：{dict(Counter(r["kind"] for r in out))}')
    print(f'调用 {stats["calls"]} 次  输入 {stats["in"]:,}  输出 {stats["out"]:,}')
    print(f'**实际花费 ¥{cost:.4f}**（余额 {b0:.2f} → {b1:.2f}）')
    if out:
        print(f'单条 ¥{cost/len(out):.5f} → 200 条约 ¥{cost/len(out)*200:.2f}')
    print(f'用时 {(time.time()-t0)/60:.1f} 分钟')


if __name__ == '__main__':
    main()
