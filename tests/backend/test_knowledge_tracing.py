"""Retrieval diagnostics use isolated data and no model/network dependencies."""
import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

from server.core.knowledge.index import LanceDBKnowledgeRetriever, KnowledgeUnavailableError
from server.core.knowledge.tracing import KnowledgeTraceStore, knowledge_trace_context


class Embedder:
    dimension = 2
    fingerprint = "fake-embedding"

    def close(self):
        pass

    def encode_query(self, text):
        return [1., 0.]


def row(key, text_hash=None):
    return dict(chunk_id=key, text_hash=text_hash or key, book_id="book", ordinal=0,
                title="Book", author="Author", heading_path="Chapter", source_locator=key,
                source_uri="", text="Full passage " + key)


class Table:
    def __init__(self, fail=False):
        self.fail = fail

    def search(self, query, **kwargs):
        self.kind = kwargs['query_type']
        if self.fail and self.kind == 'fts':
            raise RuntimeError("private detail")
        return self

    def distance_type(self, value): return self
    def select(self, value): return self
    def limit(self, value): return self

    def to_list(self):
        if self.kind == 'vector':
            return [{**row('a'), '_distance': .1}, row('b', 'a'), row('c')]
        return [{**row('a'), '_score': 5.}, row('d')]


class KnowledgeTracingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = KnowledgeTraceStore(self.tmp.name)
        self.retriever = LanceDBKnowledgeRetriever(self.tmp.name, Embedder(), trace_store=self.store)
        self.retriever._open = lambda: (Table(), 'index-fingerprint')

    def records(self):
        return [json.loads(p.read_text()) for p in sorted(self.store.root.glob('*.json'))]

    def test_candidates_fusion_and_final_output(self):
        with knowledge_trace_context('agent', run_id='run', session_id='session'):
            result = self.retriever.search(' 中文 ', limit=1)
        trace, = self.records()
        self.assertEqual(trace['query'], ' 中文 ')
        self.assertEqual(trace['expanded_query'], '中文')
        self.assertEqual(trace['context']['run_id'], 'run')
        self.assertEqual(trace['dense'][0]['score'], .1)
        self.assertEqual(trace['lexical'][0]['score'], 5.)
        self.assertIsNone(trace['dense'][1]['score'])
        self.assertEqual(trace['final_passages'][0]['passage_id'], result.passages[0].passage_id)
        decisions = {x['chunk_id']: x['decision'] for x in trace['fusion']}
        self.assertEqual(decisions, dict(a='selected', b='duplicate_text', c='limit', d='limit'))
        self.assertAlmostEqual(trace['fusion'][0]['rrf_score'], 2 / 61)
        self.assertTrue({'embedding','lock_wait','open_index','dense','lexical','fusion','total'} <= trace['timings_ms'].keys())

    def test_failures_keep_completed_stages_and_hide_exception_text(self):
        self.retriever._open = lambda: (Table(fail=True), 'index')
        with self.assertRaises(KnowledgeUnavailableError):
            self.retriever.search('test')
        trace, = self.records()
        self.assertEqual(trace['stage'], 'lexical')
        self.assertEqual(len(trace['dense']), 3)
        self.assertNotIn('private detail', json.dumps(trace))

    def test_embedding_and_index_failures(self):
        for stage in ('embedding', 'open_index'):
            with self.subTest(stage=stage):
                target = self.retriever.embedder if stage == 'embedding' else self.retriever
                attribute = 'encode_query' if stage == 'embedding' else '_open'
                with patch.object(target, attribute, side_effect=ValueError('private')):
                    with self.assertRaises(ValueError): self.retriever.search('query')
                self.assertEqual(self.records()[-1]['stage'], stage)

    def test_empty_result(self):
        with patch.object(Table, 'to_list', return_value=[]):
            self.assertEqual(self.retriever.search('query').status, 'no_match')
        self.assertEqual(self.records()[0]['final_passages'], [])

    def test_disabled_and_write_failure_do_not_change_result(self):
        self.retriever.trace_store = None
        expected = self.retriever.search('query')
        self.assertFalse(self.store.root.exists())
        self.retriever.trace_store = self.store
        with patch('server.core.knowledge.tracing.os.replace', side_effect=OSError('private')):
            with self.assertLogs('chesscoach.agent', level='WARNING'):
                self.assertEqual(self.retriever.search('query'), expected)
        self.assertEqual(list(self.store.root.iterdir()), [])

    def test_concurrent_context_and_retention(self):
        async def run():
            async def one(i):
                with knowledge_trace_context('agent', run_id=str(i)):
                    await asyncio.to_thread(self.retriever.search, 'query')
            await asyncio.wait_for(asyncio.gather(*(one(i) for i in range(8))), timeout=5)
        asyncio.run(run())
        self.assertEqual({r['context']['run_id'] for r in self.records()}, {str(i) for i in range(8)})
        self.store.max_records = 3
        self.retriever.search('query')
        self.assertEqual(len(self.records()), 3)
        self.assertFalse(list(self.store.root.glob('*.tmp')))

    def test_cli_debug_controls_trace(self):
        from contextlib import redirect_stdout
        import io
        from server import config, knowledge
        for debug in (False, True):
            with self.subTest(debug=debug), patch.object(config, "DEBUG", debug), patch.object(knowledge, "_embedder", return_value=Embedder()), patch.object(
                LanceDBKnowledgeRetriever, "_open", return_value=(Table(), "index")
            ), redirect_stdout(io.StringIO()):
                knowledge._print_search(self.tmp.name, "query", [], 2)
            self.assertEqual(len(self.records()), int(debug))
        self.assertEqual(self.records()[0]['context'], {'entrypoint': 'cli'})
