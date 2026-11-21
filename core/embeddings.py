# -*- coding: utf-8 -*-
"""OpenAI 兼容向量接口 + 本地哈希缓存，供真正的 RAG 语义检索使用。"""
import hashlib
import json
import math
import os
import urllib.request
import urllib.error


class EmbeddingError(RuntimeError):
    pass


def cache_key(model, text):
    return hashlib.sha256((model + '\0' + text).encode('utf-8')).hexdigest()


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def label_document(label):
    """RAG 向量文本：只使用标签名和常见表达，暂不使用不可靠的类别层级。"""
    hits = '、'.join(h for h in (label.get('hits') or []) if h)
    text = '标签：%s。' % label['label']
    if hits:
        text += '常见表达：%s。' % hits
    return text




def load_embedding_config(platform_name=None, platforms_path=None):
    """从 platforms.json 加载 embedding 配置。
    
    Args:
        platform_name: embedding 平台名称（如 'dashscope'）
        platforms_path: platforms.json 路径，默认自动查找 core/platforms.json
    
    Returns:
        dict: {'base_url': ..., 'api_key': ..., 'model': ...}
        如果加载失败，返回空 dict
    """
    import os
    if not platform_name:
        return {}
    if platforms_path is None:
        platforms_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'platforms.json')
    try:
        with open(platforms_path, encoding='utf-8-sig') as f:
            data = json.load(f)
        emb_platforms = data.get('embedding_platforms', {})
        if platform_name not in emb_platforms:
            print('!! embedding 平台未找到: %s (可选: %s)' % (platform_name, ', '.join(emb_platforms.keys())))
            return {}
        cfg = emb_platforms[platform_name]
        key = cfg.get('api_key', '')
        if not key:
            print('!! embedding 平台 %s 的 api_key 为空，请先在 platforms.json 中填写' % platform_name)
            return {}
        return {
            'base_url': cfg.get('base_url', ''),
            'api_key': key,
            'model': cfg.get('model', 'text-embedding-v3'),
        }
    except Exception as e:
        print('!! 读取 platforms.json 失败: %s' % e)
        return {}

class EmbeddingService:
    def __init__(self, base, key, model, cache_path, batch_size=32, timeout=120):
        if not key:
            raise EmbeddingError('缺少向量 API key：请设置 EMBEDDING_API_KEY 或 OPENAI_API_KEY')
        self.base = (base or 'https://api.openai.com').rstrip('/')
        self.key = key
        self.model = model
        self.cache_path = cache_path
        self.batch_size = max(1, int(batch_size))
        self.timeout = timeout
        self.cache = {'model': model, 'vectors': {}}
        # 运行期统计只用于成本核算，不写进向量缓存。
        self.stats = {'requests': 0, 'texts_sent': 0, 'cache_hits': 0,
                      'input_tokens': 0, 'batches_without_usage': 0}
        self.last_usage = {}
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, encoding='utf-8') as f:
                old = json.load(f)
            if old.get('model') == model and isinstance(old.get('vectors'), dict):
                self.cache = old

    @property
    def url(self):
        return self.base + ('/embeddings' if self.base.endswith('/v1') else '/v1/embeddings')

    def _post(self, texts):
        body = json.dumps({'model': self.model, 'input': texts}, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(self.url, data=body, method='POST')
        req.add_header('Authorization', 'Bearer ' + self.key)
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', errors='replace')[:500]
            raise EmbeddingError('向量接口 HTTP %s: %s' % (e.code, detail))
        except Exception as e:
            raise EmbeddingError('向量接口调用失败: %s' % e)
        self.last_usage = data.get('usage') or {} if isinstance(data, dict) else {}
        rows = data.get('data') if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise EmbeddingError('向量接口响应 data 数量与输入不一致')
        rows = sorted(rows, key=lambda x: x.get('index', 0))
        vectors = [r.get('embedding') for r in rows]
        if any(not isinstance(v, list) or not v for v in vectors):
            raise EmbeddingError('向量接口响应缺少有效 embedding')
        dim = len(vectors[0])
        if any(len(v) != dim for v in vectors):
            raise EmbeddingError('向量维度不一致')
        return vectors

    def embed(self, texts):
        """按输入顺序返回向量；缓存键含模型和原文哈希，不保存原文。"""
        missing, missing_keys = [], []
        result = {}
        vectors = self.cache['vectors']
        for text in texts:
            key = cache_key(self.model, text)
            if key in vectors:
                result[key] = vectors[key]
                self.stats['cache_hits'] += 1
            else:
                missing.append(text)
                missing_keys.append(key)
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start:start + self.batch_size]
            keys = missing_keys[start:start + self.batch_size]
            self.last_usage = {}
            batch_vectors = self._post(batch)
            self.stats['requests'] += 1
            self.stats['texts_sent'] += len(batch)
            usage = self.last_usage or {}
            tokens = usage.get('total_tokens', usage.get('input_tokens', usage.get('prompt_tokens')))
            if isinstance(tokens, (int, float)):
                self.stats['input_tokens'] += int(tokens)
            else:
                self.stats['batches_without_usage'] += 1
            for key, vector in zip(keys, batch_vectors):
                vectors[key] = vector
                result[key] = vector
        if missing:
            self.save()
        return [result[cache_key(self.model, text)] for text in texts]

    def usage_snapshot(self):
        return dict(self.stats)

    def save(self):
        if not self.cache_path:
            return
        parent = os.path.dirname(os.path.abspath(self.cache_path))
        os.makedirs(parent, exist_ok=True)
        tmp = self.cache_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.cache, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, self.cache_path)
