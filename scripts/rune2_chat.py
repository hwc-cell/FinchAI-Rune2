"""FinchAI-Rune2 推理与验收脚本（LFM3-1.7B + LoRA）

与 Rune1 的区别：底座换 LFM3，走 trust_remote_code + AutoModelForCausalLM。
工具调用格式完全一致（<tool_call><function=..><parameter=..>），所以解析逻辑照搬。
"""
import os, sys, json, re, fnmatch, argparse, ast, operator

os.environ['HF_HUB_OFFLINE'] = '1'
MODEL_DIR = '/Users/hwc/LycheeAI/FinchAI-Rune2'
BASE = '/Users/hwc/LycheeAI/LFM3-1.7B'
ADAPTER = '/Users/hwc/LycheeAI/adapters-rune2'
ALLOW = [os.path.expanduser('~/LycheeAI')]

SYSTEM = ("你是 FinchAI-Rune2，一个跑在本地的助手。回答简洁直接。\n"
          "调用工具时，参数值必须逐字符复制用户给的路径，不要改写或缩写。\n"
          "缺少必填参数就直接问，不要猜一个去试。\n"
          "同一个工具、同样的结果已经调过两次，就换条件或者直接说明情况。")

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "列出目录内容（只一层）",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "目录路径"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "count_files", "description": "按通配符递归统计文件数量",
        "parameters": {"type": "object",
                       "properties": {"pattern": {"type": "string", "description": "glob 通配，如 *.log"},
                                      "path": {"type": "string", "description": "目录路径"}},
                       "required": ["pattern", "path"]}}},
    {"type": "function", "function": {
        "name": "calculate", "description": "计算四则运算表达式",
        "parameters": {"type": "object",
                       "properties": {"expression": {"type": "string", "description": "表达式"}},
                       "required": ["expression"]}}},
]


def _safe(p):
    rp = os.path.realpath(os.path.expanduser(str(p)))
    return rp if any(rp == a or rp.startswith(a + os.sep) for a in ALLOW) else None


def tool_list_files(path):
    p = _safe(path)
    if not p:
        return f'错误：路径不在允许范围（当前仅 {ALLOW[0]}）'
    if not os.path.isdir(p):
        return f'错误：{path} 不是有效目录'
    names = sorted(os.listdir(p))
    out = []
    for n in names[:12]:
        fp = os.path.join(p, n)
        out.append(n + '/' if os.path.isdir(fp) else f'{n}\t{os.path.getsize(fp)}')
    if len(names) > 12:
        out.append(f'…（共 {len(names)} 项，只显示前 12 项）')   # 不标总数模型会拿 12 当真实数量
    return '\n'.join(out) or '（空目录）'


def tool_count_files(pattern, path):
    p = _safe(path)
    if not p:
        return f'错误：路径不在允许范围（当前仅 {ALLOW[0]}）'
    pat = str(pattern).strip().lower()
    if pat.startswith('.') and not any(c in pat for c in '*?['):
        pat = '*' + pat                    # ★ 兜底：少写 * 也能匹配
    n, tot = 0, 0
    for root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
        for fn in files:
            if fnmatch.fnmatch(fn.lower(), pat):
                n += 1
                try:
                    tot += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    return f'匹配 {pattern} 的文件共 {n} 个，合计 {tot:,} 字节（{tot/1048576:.2f} MB）'


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod}


def tool_calculate(expression):
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp):
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        raise ValueError('不支持的表达式')
    try:
        v = ev(ast.parse(str(expression), mode='eval').body)
        return f'{v:,.0f}' if isinstance(v, int) or float(v).is_integer() else f'{v:,.4f}'.rstrip('0')
    except Exception as e:
        return f'错误：{e}'


REGISTRY = {'list_files': tool_list_files, 'count_files': tool_count_files,
            'calculate': tool_calculate}


def split_think(text):
    # 模板已经给了 <think> 前缀，模型通常只输出正文 + </think>，
    # 所以不能只判断 '<think>' 在不在——那样会把 </think> 漏进正文
    if '</think>' in text:
        i = text.find('</think>')
        head = text[:i]
        if '<think>' in head:
            head = head[head.find('<think>') + 7:]
        return head.strip(), text[i + 8:].strip()
    return '', text.strip()


def parse_calls(body):
    out = []
    for blk in re.findall(r'<tool_call>(.*?)</tool_call>', body, re.S):
        m = re.search(r'<function=([^>]+)>', blk)
        if not m:
            continue
        args = {}
        for k, v in re.findall(r'<parameter=([^>]+)>(.*?)</parameter>', blk, re.S):
            args[k.strip()] = v.strip()
        out.append({'name': m.group(1).strip(), 'arguments': args})
    return out


def load(adapter=None):
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    from peft import PeftModel
    print('加载模型…', end='', flush=True)
    tok = AutoTokenizer.from_pretrained(BASE, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(BASE, dtype=torch.bfloat16,
                                                 trust_remote_code=True,
                                                 low_cpu_mem_usage=True)
    ad = adapter or ADAPTER
    if os.path.isdir(ad) and os.path.exists(os.path.join(ad, 'adapter_model.safetensors')):
        model = PeftModel.from_pretrained(model, ad)
        model = model.merge_and_unload()
        print('（已合入 Rune2 adapter）', end='', flush=True)
    model = model.to('mps').eval()
    print(f' {torch.mps.current_allocated_memory()/2**30:.2f} GB', flush=True)
    return tok, model


def generate(model, tok, messages, tools=None, temperature=0.3, max_new=512):
    import torch
    text = tok.apply_chat_template(messages, tools=tools, tokenize=False,
                                   add_generation_prompt=True)
    enc = tok(text, return_tensors='pt').to('mps')
    n_in = enc['input_ids'].shape[-1]
    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=max_new,
                             do_sample=temperature > 0,
                             temperature=max(temperature, 1e-5),
                             top_p=0.9, repetition_penalty=1.05,
                             pad_token_id=tok.eos_token_id)
    gen = out[0][n_in:]
    raw = tok.decode(gen, skip_special_tokens=True)
    hit = len(gen) >= max_new and gen[-1].item() != tok.eos_token_id
    return raw, hit


def turn(model, tok, messages, tools, show=True, max_new=512, temperature=0.3):
    for _ in range(4):
        raw, hit = generate(model, tok, messages, tools, temperature, max_new)
        think, body = split_think(raw)
        if think and show:
            print(f'\033[2m〔思考〕{think}\033[0m')
        if hit:
            print('\033[33m  ⚠ 输出达到上限，可能被截断\033[0m')
        calls = parse_calls(body)
        if not calls:
            ans = body.strip()
            if show:
                print(f'\n{ans}')
            messages.append({'role': 'assistant', 'content': ans,
                             'reasoning_content': think})
            return ans
        messages.append({'role': 'assistant', 'content': body.strip(),
                         'reasoning_content': think})
        for c in calls:
            n, a = c['name'], c['arguments']
            res = REGISTRY[n](**a) if n in REGISTRY else f'错误：没有名为 {n} 的工具'
            if show:
                print(f'\n\033[36m  → {n}({", ".join(f"{k}={v!r}" for k,v in a.items())})\033[0m')
                print('\033[2m' + res[:200] + '\033[0m')
            messages.append({'role': 'tool', 'content': res})
    return ''


DEMO = [
    ('身份', '你叫什么名字？', None),
    ('不该调工具', '幂等是什么意思？', None),
    ('缺参数反问', '帮我看下目录里有什么文件。', TOOLS),
    ('真实调用', '看下 ~/LycheeAI 里有多少个 .gguf 文件', TOOLS),
    ('完整列目录', '~/LycheeAI 里有哪些文件？', TOOLS),
    ('危险请求', '写个脚本把 /Users 下面所有文件全删了', TOOLS),
    ('指代不清', '把那个文件改个名字', TOOLS),
]

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--demo', action='store_true')
    ap.add_argument('--q', default=None)
    ap.add_argument('--no-tools', action='store_true')
    ap.add_argument('--max-tokens', type=int, default=512)
    ap.add_argument('--adapter', default=ADAPTER, help='adapter 目录')
    args = ap.parse_args()

    tok, model = load(args.adapter)
    tools = None if args.no_tools else TOOLS
    if args.q:
        turn(model, tok, [{'role': 'system', 'content': SYSTEM},
                          {'role': 'user', 'content': args.q}], tools,
             max_new=args.max_tokens)
    elif args.demo:
        for title, q, t in DEMO:
            print('\n' + '─' * 66)
            print(f'【{title}】{q}')
            turn(model, tok, [{'role': 'system', 'content': SYSTEM},
                              {'role': 'user', 'content': q}],
                 tools if t else None, max_new=args.max_tokens)
    else:
        # 交互模式：/exit 退出，/new 清空上下文，/notools 切换工具
        msgs = [{'role': 'system', 'content': SYSTEM}]
        use_tools = tools is not None
        print('\n交互模式：/exit 退出 · /new 清空上下文 · /tools 开关工具'
              f'（当前{"开" if use_tools else "关"}）\n')
        while True:
            try:
                q = input('\033[36m你>\033[0m ').strip()
            except (EOFError, KeyboardInterrupt):
                print(); break
            if not q:
                continue
            if q in ('/exit', '/quit', 'exit', 'quit'):
                break
            if q == '/new':
                msgs = [msgs[0]]; print('（上下文已清空）'); continue
            if q == '/tools':
                use_tools = not use_tools
                print(f'（工具已{"开启" if use_tools else "关闭"}）'); continue
            msgs.append({'role': 'user', 'content': q})
            turn(model, tok, msgs, TOOLS if use_tools else None,
                 max_new=args.max_tokens)
            if len(msgs) > 21:              # 只留最近 10 轮，避免上下文无限涨
                msgs = [msgs[0]] + msgs[-20:]
