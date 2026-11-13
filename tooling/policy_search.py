"""条款切分与轻量 BM25。中文用相邻字对，保留完整的型号和错误码。"""
import math
import re
from collections import Counter, defaultdict

HEADING = re.compile(r"(?m)^(?:#{1,6}[ \t]+[^\n]+|第[零〇一二三四五六七八九十百千万\d]+条[^\n]*)")


def passages(text, chunk_text):
    starts = [m.start() for m in HEADING.finditer(text)]
    bounds = sorted(set([0, *starts, len(text)]))
    for i, (begin, end) in enumerate(zip(bounds, bounds[1:])):
        section = text[begin:end].strip()
        if not section:
            continue
        heading = section.splitlines()[0][:160] if begin in starts else ""
        exceptions = [line for line in section.splitlines() if re.search(r"例外|除外|不适用", line)]
        for chunk in chunk_text(section):
            evidence = chunk
            if heading:
                evidence = section if len(section) <= 3000 else "\n".join(
                    [heading, chunk, *(line[:800] for line in exceptions[:2] if line not in chunk)])
            yield chunk, heading, i, evidence


def terms(text):
    for block in re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*|[\u3400-\u9fff]+", text.lower()):
        if block[0].isascii() or len(block) == 1:
            yield block
        else:
            yield from (block[i:i+2] for i in range(len(block)-1))


class LexicalIndex:
    def __init__(self, rows):
        self.items = {}
        self.postings = defaultdict(list)
        self.length = {}
        for key, doc, meta in zip(rows["ids"], rows["documents"], rows["metadatas"]):
            counts = Counter(terms(meta.get("title", "") + " " + doc))
            self.items[key] = (doc, meta)
            self.length[key] = sum(counts.values())
            for token, freq in counts.items():
                self.postings[token].append((key, freq))
        self.avg = max(1, sum(self.length.values()) / max(1, len(self.items)))

    def rank(self, query, limit):
        scores = Counter()
        for token in set(terms(query)):
            hits = self.postings.get(token, ())
            idf = math.log1p((len(self.items) - len(hits) + 0.5) / (len(hits) + 0.5))
            for key, freq in hits:
                norm = 1.2 * (0.25 + 0.75 * self.length[key] / self.avg)
                scores[key] += idf * freq * 2.2 / (freq + norm)
        return [key for key, _ in scores.most_common(limit)]
