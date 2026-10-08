"""Measured ages, ratio/sample semantics, collection and workspace isolation."""
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from app.channel_metrics import eligible_horizons, metrics_payload, ratios
from app.collector import Collector
from app.config import Settings
from app.postgres_db import PostgresDatabase
from tests.test_t46_overview_dates_chart import _make_app, _T46FakeDB

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def test_age_measurements_have_a_bounded_tolerance_and_no_lifetime_backfill():
    assert eligible_horizons(NOW, NOW + timedelta(hours=23,minutes=59)) == ()
    assert eligible_horizons(NOW, NOW + timedelta(hours=24)) == (24,)
    assert eligible_horizons(NOW, NOW + timedelta(hours=25)) == (24,)
    assert eligible_horizons(NOW, NOW + timedelta(hours=25,seconds=1)) == ()
    assert eligible_horizons(NOW, NOW + timedelta(days=100)) == ()


def test_rates_missingness_and_observation_dates_are_independent_of_current_cards():
    assert ratios(3000,195,2,10000) == {'er':6.5,'err':15}
    assert ratios(0,0,0,0) == {'er':None,'err':None}
    rows=[{'taken_at':NOW-timedelta(days=31),'subscribers':9000,'er':None},
          {'taken_at':NOW,'subscribers':10000,'er':6.5}]
    payload=metrics_payload(rows,'2026-09-01','2026-09-07')
    assert payload['current']['subscribers']==10000
    assert payload['previous']['subscribers']==9000
    assert payload['series']['subscribers']==[9000]
    assert payload['series']['er']==[None]
    assert metrics_payload(rows,'2027-01-01',None)['series']['labels']==[]
    gap=metrics_payload([rows[0],{**rows[1],'taken_at':rows[0]['taken_at']+timedelta(days=2)}])['series']
    assert gap['subscribers']==[9000,None,10000]
    assert gap['observed']==[True,False,True]


class MetricsDB(_T46FakeDB):
    async def channel_metrics(self, channel_id, from_date=None, to_date=None):
        return metrics_payload([{'taken_at':NOW,'subscribers':10000,'views24':500,'views48':700,'views72':800,'er':6.5,'err':15}],from_date,to_date) if channel_id==1 else metrics_payload([])


@pytest.mark.asyncio
async def test_authenticated_overview_keeps_legacy_cohort_and_new_current_metrics(tmp_path):
    app,_,_= _make_app(tmp_path,MetricsDB())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get('/api/overview?channel=1')).status_code in (303,401)
        await client.post('/login',data={'username':'admin','password':'pw'})
        page=await client.get('/?channel=1&from=2026-08-24&to=2026-09-06')
        assert page.status_code==200
        for marker in ['data-channel-metrics','Typical views horizon','Post Explorer','Apply dates','data-explorer-form','data-reader','Daily posts','Month']:
            assert marker in page.text
        data=(await client.get('/api/overview?channel=1&from=2026-08-24&to=2026-09-06')).json()
        assert data['kpis']['views']==3000
        assert data['channel_metrics']['current']['views24']==500
        assert data['channel_metrics']['series']['labels']==[]
        assert (await client.get('/api/overview?channel=99')).json()['channel_metrics']['current']['subscribers'] is None


@pytest.mark.asyncio
async def test_collector_refreshes_subscribers_and_targets_due_posts_outside_recent_scan():
    class DB:
        observations=[]
        horizons=[]
        snapshots=[]
        async def upsert_channel(self,*args,**kwargs):return 1
        async def pending_view_horizons(self,channel_id):return [{'id':9,'message_id':90}]
        async def record_post_horizon_views(self,post_id,views):self.horizons.append((post_id,views))
        async def add_snapshot_if_changed(self,*args):self.snapshots.append(args);return True
        async def capture_channel_metrics(self,*args):self.observations.append(args)
    class Client:
        async def get_entity(self,identifier):return SimpleNamespace(id=1,title='Synthetic')
        async def __call__(self,request):return SimpleNamespace(full_chat=SimpleNamespace(participants_count=1234))
        async def iter_messages(self,*args,**kwargs):
            for item in []:yield item
        async def get_messages(self,entity,ids):
            assert ids==[90]
            return [SimpleNamespace(id=90,views=0,forwards=1,replies=None,reactions=None)]
    db=DB();collector=Collector(Client(),db,Settings(_env_file=None,track_days=0))
    await collector.poll_channel({'id':1,'identifier':'@synthetic'})
    assert db.horizons==[(9,0)]
    assert db.snapshots==[(9,0,0,0,1)]
    assert db.observations==[(1,1234)]


@pytest.mark.asyncio
async def test_optional_subscriber_lookup_failure_does_not_stop_post_collection():
    class DB:
        count=None
        async def upsert_channel(self,*args,**kwargs):return 1
        async def capture_channel_metrics(self,channel_id,subscribers):self.count=subscribers
    class Client:
        async def get_entity(self,identifier):return SimpleNamespace(id=1,title='Synthetic')
        async def __call__(self,request):raise RuntimeError('synthetic lookup failure')
        async def iter_messages(self,*args,**kwargs):
            for item in []:yield item
    db=DB();collector=Collector(Client(),db,Settings(_env_file=None,track_days=0))
    assert (await collector.poll_channel({'id':1,'identifier':'@synthetic'}))[0]==0
    assert db.count is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_measured_age_coverage_rates_and_workspace_boundary():
    url=os.environ.get('TEST_POSTGRES_URL')
    if not url:pytest.skip('TEST_POSTGRES_URL is required')
    slug=f'metrics-{uuid.uuid4().hex[:10]}'
    db=PostgresDatabase(url,workspace_slug=slug)
    other=PostgresDatabase(url,workspace_slug=slug+'-other')
    await db.init_db(admin_username=slug)
    await other.init_db(admin_username=slug+'-other')
    try:
        channel=await db.add_channel('@synthetic_metrics')
        now=datetime.now(timezone.utc)
        first=await db.upsert_post(channel,1,now-timedelta(days=4),'Synthetic first')
        second=await db.upsert_post(channel,2,now-timedelta(days=10),'Synthetic second')
        await db.add_snapshot_if_changed(first,1000,10,50,5)
        await db.add_snapshot_if_changed(second,2000,20,100,10)
        for h,value in [(24,500),(48,700),(72,800)]:
            await db.record_post_horizon_views(first,value,now-timedelta(days=4)+timedelta(hours=h,minutes=15))
            await db.record_post_horizon_views(first,9999,now-timedelta(days=4)+timedelta(hours=h,minutes=30))
        # An archived lifetime counter and a too-late read must not create age history.
        await db.record_post_horizon_views(second,2000,now)
        await db.capture_channel_metrics(channel,10000)
        payload=await db.channel_metrics(channel)
        assert payload['current']=={'subscribers':10000,'views24':500,'views48':700,'views72':800,'er':6.5,'err':15}
        assert payload['coverage']['views24']=={'measured':1,'eligible':2}
        assert (await db.channel_metrics(channel,'2027-01-01',None))['series']['labels']==[]
        before=payload['subscribers_observed_at']
        await db.capture_channel_metrics(channel,None)
        assert (await db.channel_metrics(channel))['subscribers_observed_at']==before
        due=await db.upsert_post(channel,3,now-timedelta(hours=24,minutes=30),'Synthetic due')
        assert [p['id'] for p in await db.pending_view_horizons(channel)]==[due]
        assert (await other.channel_metrics(channel))['current']['subscribers'] is None
        assert await other.pending_view_horizons(channel)==[]
        await other.record_post_horizon_views(due,9999,now)
        await other.capture_channel_metrics(channel,9999)
        assert (await db.channel_metrics(channel))['current']['subscribers']==10000
    finally:
        for repo in (db,other):
            async with repo.sessions.session() as session:
                await session.execute(text('DELETE FROM workspaces WHERE id=:id'),{'id':repo.workspace_id})
                await session.commit()
            await repo.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_successful_collection_freshness_is_channel_workspace_and_active_scoped():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is required")
    slug = f"overview-fresh-{uuid.uuid4().hex[:10]}"
    db = PostgresDatabase(url, workspace_slug=slug)
    other = PostgresDatabase(url, workspace_slug=slug + "-other")
    await db.init_db(admin_username=slug)
    await other.init_db(admin_username=slug + "-other")
    try:
        first_channel = await db.add_channel("@synthetic_first")
        second_channel = await db.add_channel("@synthetic_second")
        other_channel = await other.add_channel("@synthetic_other")

        post = await db.upsert_post(first_channel, 1, datetime.now(timezone.utc) - timedelta(days=1), "Synthetic post")
        await db.add_snapshot_if_changed(post, 100, 10, 20, 5)
        last_counter_change = (await db.kpis(first_channel))["last_poll"]
        assert last_counter_change is not None

        first_job = await db.claim_collection_job(first_channel)
        assert first_job is not None
        await db.finish_collection_job(first_job, status="succeeded")
        first_finished = await db.last_successful_collection_at(first_channel)
        assert first_finished is not None
        assert await db.last_successful_collection_at(second_channel) is None

        second_job = await db.claim_collection_job(second_channel)
        assert second_job is not None
        await db.finish_collection_job(second_job, status="succeeded")
        second_finished = await db.last_successful_collection_at(second_channel)
        assert second_finished is not None and second_finished >= first_finished
        assert await db.last_successful_collection_at() == second_finished
        assert (await db.kpis(first_channel))["last_poll"] == last_counter_change

        failed_job = await db.claim_collection_job(first_channel)
        assert failed_job is not None
        await db.finish_collection_job(failed_job, status="failed", error="synthetic failure")
        running_job = await db.claim_collection_job(first_channel)
        assert running_job is not None
        assert await db.last_successful_collection_at(first_channel) == first_finished

        await db._execute(
            "UPDATE channels SET active=false WHERE workspace_id=:workspace_id AND id=:channel_id",
            {"channel_id": second_channel},
        )
        assert await db.last_successful_collection_at() == first_finished
        assert await db.last_successful_collection_at(second_channel) == second_finished
        other_job = await other.claim_collection_job(other_channel)
        assert other_job is not None
        await other.finish_collection_job(other_job, status="succeeded")
        assert await other.last_successful_collection_at(other_channel) is not None
        assert await db.last_successful_collection_at(other_channel) is None
        assert await db.last_successful_collection_at() == first_finished
    finally:
        for repo in (db, other):
            async with repo.sessions.session() as session:
                await session.execute(text("DELETE FROM workspaces WHERE id=:id"), {"id": repo.workspace_id})
                await session.commit()
            await repo.close()
