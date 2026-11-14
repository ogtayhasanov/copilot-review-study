"""Safety tests for preserving the live REST checkpoint and complete pagination."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from collect_all_reviews import collect_one, normalize, open_db
from collect_reviews_graphql import (RestartPR, StopCollection, RetryBatch, build_query,
    collect, commit_page, cursor_for, decode_pages, initialize_fast, normalize_node, preflight)


def node(i, stamp="2026-09-05T01:00:00Z"):
    return {"fullDatabaseId": str(i), "url": f"https://github.com/example/project/pull/1#pullrequestreview-{i}",
            "submittedAt": stamp, "state": "COMMENTED", "body": "### 🟢 Approval recommended\nLooks good.",
            "author": {"login": "copilot-pull-request-reviewer", "__typename": "Bot"},
            "commit": {"oid": "a"*40}}


def response(jobs, values):
    _, aliases = build_query(jobs)
    data = {"rateLimit": {"cost": 1, "remaining": 4000}}
    for (ra, pa), (key, _) in aliases.items():
        data.setdefault(ra, {"nameWithOwner": key[0]})[pa] = values[key]
    return data, aliases


def connection(key, rows, more=False, end="cursor-1", total=None):
    return {"number": key[1], "reviews": {"nodes": rows, "totalCount": len(rows) if total is None else total,
            "pageInfo": {"hasNextPage": more, "endCursor": end}}}


class FakeGraphQL:
    def __init__(self, replies):
        self.replies, self.calls, self.last_cost, self.remaining = list(replies), 0, 1, 4000

    def fetch(self, query):
        self.calls += 1
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"reviews.sqlite"
        self.a, self.b = ("example/project", 1), ("example/project", 2)
        self.db = open_db(self.path, [self.a, self.b])
        initialize_fast(self.db, self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def page(self, key, rows, after=None, more=False, end="cursor-1", total=None):
        data, aliases = response([(key, after)], {key: connection(key, rows, more, end, total)})
        good, unavailable, retry = decode_pages(data, [], aliases)
        self.assertFalse(unavailable or retry)
        return good[0]

    def test_complete_pr_is_not_rewritten_and_backup_contains_original_data(self):
        with self.db:
            self.db.execute("UPDATE prs SET status='complete',source='github_rest',completed_at='frozen' WHERE repo=? AND pr=?", self.b)
        before = self.db.execute("SELECT * FROM prs WHERE repo=? AND pr=?", self.b).fetchone()
        with self.assertRaises(ValueError):
            commit_page(self.db, self.page(self.b, [node(99)]))
        commit_page(self.db, self.page(self.a, [node(1)]))
        self.assertEqual(self.db.execute("SELECT * FROM prs WHERE repo=? AND pr=?", self.b).fetchone(), before)
        self.assertTrue(self.path.with_name("reviews_before_fast.sqlite").exists())

    def test_second_page_resume_uses_saved_cursor_and_only_then_marks_complete(self):
        commit_page(self.db, self.page(self.a, [node(1)], more=True, end="opaque-A", total=2))
        self.assertEqual(cursor_for(self.db, self.a), "opaque-A")
        self.assertEqual(self.db.execute("SELECT status FROM prs WHERE repo=? AND pr=?", self.a).fetchone()[0], "running")
        self.db.close()
        self.db = open_db(self.path, [self.a,self.b])
        initialize_fast(self.db,self.path)
        self.assertEqual(cursor_for(self.db,self.a),"opaque-A")
        commit_page(self.db, self.page(self.a, [node(2)], after="opaque-A", end="opaque-B", total=2))
        self.assertEqual(self.db.execute("SELECT status FROM prs WHERE repo=? AND pr=?", self.a).fetchone()[0], "complete")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reviews WHERE repo=? AND pr=?", self.a).fetchone()[0], 2)

    def test_partial_rest_restart_does_not_change_other_prs(self):
        with self.db:
            self.db.execute("UPDATE prs SET status='running',source='github_rest',next_page=2 WHERE repo=? AND pr=?",self.a)
            self.db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(*self.a,'old',None,None,'COMMENTED',0,'User',None,'user',None,None))
            self.db.execute("UPDATE prs SET status='complete',source='github_rest' WHERE repo=? AND pr=?", self.b)
        commit_page(self.db,self.page(self.a,[node(900)]))
        self.assertEqual(self.db.execute("SELECT review_id FROM reviews WHERE repo=? AND pr=?", self.a).fetchall(), [('900',)])
        self.assertEqual(self.db.execute("SELECT status FROM prs WHERE repo=? AND pr=?",self.b).fetchone()[0], 'complete')

    def test_incomplete_count_and_overlapping_pages_never_become_complete(self):
        commit_page(self.db,self.page(self.a,[node(1)],more=True,end="A",total=3))
        with self.assertRaises(RestartPR):
            commit_page(self.db,self.page(self.a,[node(2)],after="A",end="B",total=3))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM reviews WHERE repo=? AND pr=?",self.a).fetchone()[0],1)
        with self.assertRaises(RestartPR):
            commit_page(self.db,self.page(self.a,[node(1)],after="A",end="B",total=1))
        self.assertEqual(self.db.execute("SELECT status FROM prs WHERE repo=? AND pr=?",self.a).fetchone()[0],"running")

    def test_partial_graphql_error_does_not_discard_healthy_alias(self):
        jobs=[(self.a,None),(self.b,None)]
        data,aliases=response(jobs,{self.a:connection(self.a,[node(1)]),self.b:None})
        errors=[{"type":"NOT_FOUND","path":["r0","p1"],"message":"not found"}]
        good,missing,retry=decode_pages(data,errors,aliases)
        self.assertEqual([p['key'] for p in good],[self.a])
        self.assertEqual(missing,[self.b])
        self.assertFalse(retry)

    def test_bigint_ids_and_url_fallback_are_exact(self):
        self.assertEqual(normalize_node(node(9007199254740993))['review_id'],'9007199254740993')
        n=node(5391382494);n['fullDatabaseId']=None
        self.assertEqual(normalize_node(n)['review_id'],'5391382494')
        n['url']='https://github.com/example/project'
        with self.assertRaises(ValueError):normalize_node(n)

    def test_timeout_split_requeues_prs_and_completes_in_smaller_batches(self):
        a,_=response([(self.a,None)],{self.a:connection(self.a,[node(1)])})
        b,_=response([(self.b,None)],{self.b:connection(self.b,[node(2)])})
        client=FakeGraphQL([RetryBatch('timeout'),(a,[]),(b,[])])
        with patch('collect_reviews_graphql.time.sleep'):
            self.assertEqual(collect(self.db,client,[self.a,self.b],20),2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM prs WHERE status='complete'").fetchone()[0],2)
        self.assertEqual(client.calls,3)

    def test_repeated_cursor_response_is_not_counted_as_complete(self):
        data,aliases=response([(self.a,'A')],{self.a:connection(self.a,[node(1)],True,'A',2)})
        good,missing,retry=decode_pages(data,[],aliases)
        self.assertFalse(good or missing)
        self.assertEqual(len(retry),1)

    def test_backup_is_a_consistent_sqlite_snapshot(self):
        backup=sqlite3.connect(self.path.with_name('reviews_before_fast.sqlite'))
        try:
            self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(backup.execute('SELECT COUNT(*) FROM prs').fetchone()[0],2)
        finally:backup.close()

    def test_canary_rejects_api_disagreement_before_replacing_data(self):
        data,_=response([(self.a,None)],{self.a:connection(self.a,[node(1)])})
        class Rest:
            def __init__(self,*a,**kw):pass
            def fetch(self,*args):
                n=node(2)
                return [{"id":2,"user":{"login":"copilot-pull-request-reviewer[bot]","type":"Bot"},
                         "body":n['body'],"submitted_at":n['submittedAt'],"commit_id":"a"*40,"state":"COMMENTED"}],{},'url'
        with patch('collect_reviews_graphql.GitHub',Rest):
            with self.assertRaises(StopCollection):preflight(self.db,FakeGraphQL([(data,[])]),'dummy')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],0)
        self.assertIsNone(self.db.execute("SELECT value FROM metadata WHERE key='fast_preflight'").fetchone())

    def test_canary_accepts_equivalent_native_fields_and_bot_login_forms(self):
        data,_=response([(self.a,None)],{self.a:connection(self.a,[node(5391382494)])})
        class Rest:
            def __init__(self,*a,**kw):pass
            def fetch(self,*args):
                n=node(5391382494)
                return [{"id":5391382494,"user":{"login":"copilot-pull-request-reviewer[bot]","type":"Bot"},
                         "body":n['body'],"submitted_at":"2026-09-05T01:00:00+00:00", "commit_id":"a"*40,"state":"COMMENTED"}],{},'url'
        with patch('collect_reviews_graphql.GitHub',Rest):preflight(self.db,FakeGraphQL([(data,[])]),'dummy')
        self.assertIsNotNone(self.db.execute("SELECT value FROM metadata WHERE key='fast_preflight'").fetchone())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],0)

    def test_one_bad_pr_is_reported_without_discarding_the_healthy_pr(self):
        import re
        class Dynamic:
            calls,last_cost,remaining=0,1,4000
            def fetch(self,query):
                self.calls+=1
                data={'r0':{'nameWithOwner':'example/project'}}; errors=[]
                for alias,number in re.findall(r'(p\d+):pullRequest\(number:(\d+)\)',query):
                    if int(number)==1:data['r0'][alias]=connection(('example/project',1),[node(1)])
                    else:
                        data['r0'][alias]=None
                        errors.append({'type':'INTERNAL','path':['r0',alias],'message':'temporary field failure'})
                return data,errors
        with patch('collect_reviews_graphql.time.sleep'):
            self.assertEqual(collect(self.db,Dynamic(),[self.a,self.b],20),2)
        states=dict(self.db.execute('SELECT pr,status FROM prs'))
        self.assertEqual(states,{1:'complete',2:'error'})

    def test_persistent_network_failure_stops_without_marking_data_complete(self):
        with patch('collect_reviews_graphql.time.sleep'):
            with self.assertRaises(StopCollection):
                collect(self.db,FakeGraphQL([RetryBatch('offline') for _ in range(6)]),[self.a,self.b],20)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM prs WHERE status='complete'").fetchone()[0],0)

    def test_batch_and_cursor_pagination_collect_all_prs(self):
        d1,_=response([(self.a,None),(self.b,None)],{
            self.a:connection(self.a,[node(1)],True,'A',2),self.b:connection(self.b,[node(2)])})
        d2,_=response([(self.a,'A')],{self.a:connection(self.a,[node(3)],False,'B',2)})
        client=FakeGraphQL([(d1,[]),(d2,[])])
        self.assertEqual(collect(self.db,client,[self.a,self.b],20),2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0],3)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM prs WHERE status='complete'").fetchone()[0],2)
        self.assertEqual(client.calls,2)


if __name__=='__main__':
    unittest.main()
