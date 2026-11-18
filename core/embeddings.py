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
    """将标签定义转成用于向量化的语义文档。"""
    ctx = label['cat'] if label.get('sub') in ('', '通用', None) else '%s·%s' % (label['cat'], label['sub'])
    hits = '、'.join(h for h in (label.get('hits') or []) if h)
    text = '标签：%s；类别：%s。' % (label['label'], ctx)
    if hits:
        text += '常见表达：%s。' % hits
    return text


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
            else:
                missing.append(text)
                missing_keys.append(key)
        for start in range(0, len(missing), self.batch_size):
            batch = missing[start:start + self.batch_size]
            keys = missing_keys[start:start + self.batch_size]
            for key, vector in zip(keys, self._post(batch)):
                vectors[key] = vector
                result[key] = vector
        if missing:
            self.save()
        return [result[cache_key(self.model, text)] for text in texts]

    def save(self):
        if not self.cache_path:
            return
        parent = os.path.dirname(os.path.abspath(self.cache_path))
        os.makedirs(parent, exist_ok=True)
        tmp = self.cache_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.cache, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, self.cache_path)
