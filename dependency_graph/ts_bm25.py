"""Self-contained BM25 index over code-graph nodes (``bm25s`` + PyStemmer).

Replaces the upstream ``plugins/location_tools/retriever/bm25_retriever.py``,
which drags in the whole ``llama-index`` stack and an ``EpicSplitter`` file
chunker. Here the graph *is* the chunking: one BM25 document per file / class /
function node, its text built from the node's dotted name, skeleton and (capped)
source. Test files are excluded by default.

    idx = TsBM25Index.from_graph(graph)
    idx.retrieve("selection toolbar", k=8)                  # -> [node_id, ...]
    idx.retrieve("z order", k=8, search_scope="function")   # scope filter
    idx.save(".locagent/bm25"); TsBM25Index.load(".locagent/bm25")
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Sequence, Tuple, Union

import bm25s
import Stemmer

from dependency_graph.build_graph import (
    NODE_TYPE_CLASS,
    NODE_TYPE_FILE,
    NODE_TYPE_FUNCTION,
)
from dependency_graph.traverse_graph import is_test_file

_INDEXED_TYPES = (NODE_TYPE_FILE, NODE_TYPE_CLASS, NODE_TYPE_FUNCTION)
_CODE_CHAR_CAP = 4000          # per-doc cap on raw source contribution
_COMMENT_CHAR_CAP = 1500       # per-doc cap on the extracted comment field
_STEMMER_LANG = 'english'

_CAMEL_RE = re.compile(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')
_SEP_RE = re.compile(r'[_\-./:\\]+')

# `(?<![:/])` skips `http://` and `///` triple-slash directives; block form
# also catches `/** JSDoc */`, with the per-line leading `*` stripped after.
_LINE_COMMENT_RE = re.compile(r'(?<![:/])//[ \t]?(.*)')
_BLOCK_COMMENT_RE = re.compile(r'/\*+([\s\S]*?)\*/')
_JSDOC_STAR_RE = re.compile(r'^[ \t]*\*[ \t]?', re.M)


def _comments_for(code: str) -> str:
    """Pull comment / JSDoc prose out of an entity's source as its own weighted
    field so a natural-language ``graph_search`` can rank the entity by what its
    comments *say*. Value-level conventions ("``color === undefined`` means text
    box", "keep in sync with X") live only in comments -- no code edge carries
    them -- so without this they are invisible to the graph and only grep finds
    them."""
    if not code:
        return ''
    out: List[str] = []
    for m in _BLOCK_COMMENT_RE.finditer(code):
        out.append(_JSDOC_STAR_RE.sub('', m.group(1)))
    for m in _LINE_COMMENT_RE.finditer(code):
        out.append(m.group(1))
    text = ' '.join(s.strip() for s in out if s.strip())
    return text[:_COMMENT_CHAR_CAP]


def _split_identifiers(text: str) -> str:
    """Explode camelCase / PascalCase / snake / kebab / dotted identifiers into
    space-separated words so BM25 tokenisation sees ``handleDragMove`` as
    ``handle drag move`` (kept alongside the original joined form)."""
    text = _SEP_RE.sub(' ', text)
    text = _CAMEL_RE.sub(' ', text)
    return text


def _doc_for_node(nid: str, ndata: dict) -> str:
    ntype = ndata.get('type')
    parts: List[str] = [nid, _split_identifiers(nid)]
    code = ndata.get('code', '') or ''

    if ntype == NODE_TYPE_FILE:
        # for a file the raw `code` is the whole thing -- lean on a skeleton and
        # a capped head instead so one big file does not dominate the index.
        skel = ndata.get('skeleton') or ''
        if not skel:
            try:
                from plugins.location_tools.utils.compress_file_ts import get_skeleton
                skel = get_skeleton(code, language=ndata.get('language'))
            except Exception:
                skel = code[:_CODE_CHAR_CAP]
        parts.append(skel[:_CODE_CHAR_CAP])
    else:
        if ndata.get('skeleton'):
            parts.append(ndata['skeleton'])
        parts.append(code[:_CODE_CHAR_CAP])
        if ndata.get('is_component'):
            parts.append('react component jsx')

    # comment / JSDoc prose as its own field -- see _comments_for. Appended after
    # the capped source so a convention noted past char 4000 is still indexed.
    cmt = _comments_for(code)
    if cmt:
        parts.append(cmt)

    joined = '\n'.join(p for p in parts if p)
    return joined + '\n' + _split_identifiers(joined)


class TsBM25Index:
    def __init__(self, nids: List[str], types: List[str], files: List[str],
                 retriever: 'bm25s.BM25', stemmer):
        self.nids = nids
        self.types = types            # parallel to nids
        self.files = files            # parallel to nids (file part of the nid)
        self.retriever = retriever
        self.stemmer = stemmer

    # ------------------------------------------------------------------ build
    @classmethod
    def from_graph(cls, graph, *, include_tests: bool = False) -> 'TsBM25Index':
        nids: List[str] = []
        types: List[str] = []
        files: List[str] = []
        corpus: List[str] = []
        for nid, ndata in graph.nodes(data=True):
            if ndata.get('type') not in _INDEXED_TYPES:
                continue
            if not include_tests and is_test_file(nid):
                continue
            nids.append(nid)
            types.append(ndata['type'])
            files.append(nid.split(':')[0])
            corpus.append(_doc_for_node(nid, ndata))

        if not nids:
            raise ValueError('no indexable nodes in graph')

        stemmer = Stemmer.Stemmer(_STEMMER_LANG)
        corpus_tokens = bm25s.tokenize(corpus, stopwords='en', stemmer=stemmer,
                                       show_progress=False)
        retriever = bm25s.BM25()
        retriever.index(corpus_tokens, show_progress=False)
        return cls(nids, types, files, retriever, stemmer)

    # --------------------------------------------------------------- retrieve
    def retrieve(
        self,
        query: str,
        k: int = 10,
        search_scope: str = 'all',          # 'all' | 'file' | 'class' | 'function'
        include_files: Optional[Sequence[str]] = None,
        return_scores: bool = False,
    ) -> Union[List[str], List[Tuple[str, float]]]:
        if not query or not query.strip():
            return []
        n = len(self.nids)
        # when a post-filter is active, rank the whole corpus so the filter does
        # not starve on an over-fetch window full of out-of-scope hits.
        if search_scope != 'all' or include_files:
            want = n
        else:
            want = min(max(k * 8, 50), n)

        q_tokens = bm25s.tokenize(query, stemmer=self.stemmer, show_progress=False)
        idx_arr, score_arr = self.retriever.retrieve(
            q_tokens, k=want, show_progress=False)

        inc = set(include_files) if include_files else None
        out: List[Tuple[str, float]] = []
        for i, sc in zip(idx_arr[0], score_arr[0]):
            i = int(i)
            if search_scope != 'all' and self.types[i] != search_scope:
                continue
            if inc is not None and self.files[i] not in inc:
                continue
            out.append((self.nids[i], float(sc)))
            if len(out) >= k:
                break

        return out if return_scores else [nid for nid, _ in out]

    # ----------------------------------------------------------- persistence
    def save(self, dir_path: str) -> None:
        os.makedirs(dir_path, exist_ok=True)
        self.retriever.save(dir_path)
        with open(os.path.join(dir_path, 'ts_index.json'), 'w', encoding='utf-8') as fh:
            json.dump({'nids': self.nids, 'types': self.types, 'files': self.files,
                       'stemmer_lang': _STEMMER_LANG}, fh)

    @classmethod
    def load(cls, dir_path: str) -> 'TsBM25Index':
        retriever = bm25s.BM25.load(dir_path, mmap=False)
        with open(os.path.join(dir_path, 'ts_index.json'), 'r', encoding='utf-8') as fh:
            meta = json.load(fh)
        stemmer = Stemmer.Stemmer(meta.get('stemmer_lang', _STEMMER_LANG))
        return cls(meta['nids'], meta['types'], meta['files'], retriever, stemmer)


if __name__ == '__main__':
    import argparse
    import pickle
    import time

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--graph', required=True, help='pickled graph from ts_build_graph')
    ap.add_argument('--query', action='append', default=[], help='(repeatable) test query')
    ap.add_argument('-k', type=int, default=8)
    ap.add_argument('--save', help='directory to persist the index into')
    args = ap.parse_args()

    with open(args.graph, 'rb') as fh:
        g = pickle.load(fh)
    t0 = time.time()
    idx = TsBM25Index.from_graph(g)
    print(f'indexed {len(idx.nids)} nodes in {time.time() - t0:.2f}s')
    for q in args.query:
        print(f'\n{q!r}')
        for nid, sc in idx.retrieve(q, k=args.k, return_scores=True):
            print(f'  {sc:6.2f}  {nid}')
    if args.save:
        idx.save(args.save)
        print(f'\nsaved to {args.save}')
