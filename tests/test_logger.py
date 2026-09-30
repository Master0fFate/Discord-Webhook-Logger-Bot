import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace
import webhooklogger as w

ENV = {'DISCORD_TOKEN': 'test', 'DISCORD_WEBHOOK_URL': 'https://discord.com/api/webhooks/123/test', 'LOG_GUILD_IDS': '1', 'LOG_CHANNEL_IDS': '2'}

class ConfigTests(unittest.TestCase):
    def test_explicit_scope(self):
        with self.assertRaises(ValueError): w.Config.load({**ENV, 'LOG_CHANNEL_IDS': ''})
        self.assertTrue(w.Config.load({**ENV, 'LOG_ALL_CHANNELS': 'true'}).all_channels)
    def test_secrets_not_in_repr(self):
        self.assertNotIn('https://', repr(w.Config.load(ENV)))
    def test_webhook_validation(self):
        for url in ['http://discord.com/api/webhooks/1/a', 'https://evil.test/api/webhooks/1/a', ENV['DISCORD_WEBHOOK_URL']+'?thread_id=1']:
            with self.assertRaises(ValueError): w.Config.load({**ENV, 'DISCORD_WEBHOOK_URL': url})
    def test_render_current_forms_and_no_mentions(self):
        payload = w.render('MESSAGE_CREATE', {'id':'4','guild_id':'1','channel_id':'2','content':'@everyone','message_snapshots':[{'message':{'content':'forwarded'}}], 'poll':{'question':{'text':'vote?'}}, 'sticker_items':[{'id':'9'}], 'components':[{'type':17}], 'type':999})
        self.assertEqual(payload['allowed_mentions']['parse'], [])
        for text in ['forwarded','vote?','999','sticker_items','components']:
            self.assertIn(text, json.dumps(payload))
    def test_partial_update_and_unknown_delete(self):
        self.assertIn('omitted fields', str(w.render('MESSAGE_UPDATE', {'embeds':[]})))
        self.assertIn('cannot be fetched', str(w.render('MESSAGE_DELETE', {})))
    def test_maximal_message(self):
        result = w.render('MESSAGE_CREATE', {key: 'x'*20000 for key in w.MESSAGE_KEYS})
        self.assertLessEqual(len(result['embeds'][0]['description']),4096)

class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status, self.body, self.headers = status, body or {}, headers or {}
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self): return self.body

class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_429_500_success(self):
        session = Mock(); session.post.side_effect=[Response(429, {'retry_after':1.25}),Response(500),Response(200, {'id':'8'})]
        sleep = AsyncMock(); sender=w.Sender(session,'https://example.invalid',sleep)
        self.assertEqual((await sender.send({}))['id'],'8')
        self.assertEqual([x.args[0] for x in sleep.await_args_list],[1.25,2])
    async def test_retry_after_not_shortened(self):
        self.assertEqual(w.Sender.delay(900,1),900)
    async def test_permanent_failure_circuit_breaker(self):
        session=Mock(); session.post.return_value=Response(404)
        sender=w.Sender(session,'unused',AsyncMock())
        await sender.send({}); await sender.send({})
        self.assertEqual(session.post.call_count,1)
    async def test_retry_budget(self):
        session=Mock(); session.post.return_value=Response(500)
        self.assertIsNone(await w.Sender(session,'unused',AsyncMock()).send({}))
        self.assertEqual(session.post.call_count,5)

class EventTests(unittest.IsolatedAsyncioTestCase):
    def make_logger(self, **overrides):
        config=w.Config.load({**ENV, **overrides})
        logger=w.Logger(config)
        logger.get_channel=Mock(return_value=SimpleNamespace(name='thread', parent_id=2, parent=SimpleNamespace(name='forum')))
        logger.capture_queue=asyncio.Queue(maxsize=1)
        logger.sender=SimpleNamespace(send=AsyncMock(return_value={'id':'9','channel_id':'8','guild_id':'1'}))
        logger.session=Mock()
        return logger
    async def test_thread_parent_scope_and_exclusion(self):
        logger=self.make_logger()
        self.assertTrue(logger.scope({'guild_id':'1','channel_id':'3'}))
        self.assertFalse(logger.scope({'guild_id':'4','channel_id':'3'}))
        logger=self.make_logger(LOG_EXCLUDE_CHANNEL_IDS='2')
        self.assertFalse(logger.scope({'guild_id':'1','channel_id':'3'}))
    async def test_destination_excluded(self):
        logger=self.make_logger(); logger.destination_id=3
        self.assertFalse(logger.scope({'guild_id':'1','channel_id':'3'}))
    async def test_raw_reactions_uncached(self):
        logger=self.make_logger()
        for event in ('MESSAGE_REACTION_ADD','MESSAGE_REACTION_REMOVE','MESSAGE_REACTION_REMOVE_ALL','MESSAGE_REACTION_REMOVE_EMOJI'):
            await logger.handle_event({'t':event,'d':{'guild_id':'1','channel_id':'3','message_id':'4','emoji':{'name':'🔥'}}})
            received,data,future=logger.queue.get_nowait()
            await logger.process(received,data,future)
        self.assertEqual(logger.sender.send.await_count,4)
    async def test_skip_bots_webhooks_and_bounded_queue(self):
        logger=self.make_logger(QUEUE_SIZE='1')
        data={'guild_id':'1','channel_id':'3','id':'4'}
        await logger.handle_event({'t':'MESSAGE_CREATE','d':{**data,'webhook_id':'7'}})
        await logger.handle_event({'t':'MESSAGE_CREATE','d':{**data,'author':{'bot':True}}})
        self.assertTrue(logger.queue.empty())
        data['id']='99'
        await logger.handle_event({'t':'MESSAGE_CREATE','d':data})
        await logger.handle_event({'t':'MESSAGE_CREATE','d':data})
        self.assertEqual(logger.dropped,1)
    async def test_capture_admitted_before_delivery(self):
        logger=self.make_logger()
        await logger.handle_event({'t':'MESSAGE_CREATE','d':{'guild_id':'1','channel_id':'3','id':'4','attachments':[{'id':'5','url':'https://cdn.discordapp.com/attachments/1/2/f.png'}]}})
        self.assertEqual(logger.capture_queue.qsize(),1)
        logger.sender.send.assert_not_called()
    async def test_capture_admission_expiry(self):
        logger=self.make_logger(); future=asyncio.get_running_loop().create_future()
        logger.capture_queue.put_nowait(([],future,0))
        worker=asyncio.create_task(logger.capture())
        result=await future
        self.assertIn('expired',result[1][0])
        worker.cancel(); await asyncio.gather(worker,return_exceptions=True)
    async def test_partial_merge_bulk_delete_and_cache_bound(self):
        logger=self.make_logger(CACHE_SIZE='1',ARCHIVE_ATTACHMENTS='false')
        data={'id':'4','guild_id':'1','channel_id':'3','content':'original','attachments':[{'filename':'file'}]}
        await logger.process('MESSAGE_CREATE',data)
        await logger.process('MESSAGE_UPDATE',{'id':'4','guild_id':'1','channel_id':'3','embeds':[]})
        self.assertEqual(logger.cache[('3','4')]['content'],'original')
        await logger.process('MESSAGE_DELETE_BULK',{'guild_id':'1','channel_id':'3','ids':['4','5']})
        self.assertFalse(logger.cache)
        self.assertIn('original',str(logger.sender.send.await_args_list[-2]))

if __name__=='__main__': unittest.main()

class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    make_logger = EventTests.make_logger
    async def test_cross_guild_archive_link_and_delete_after_capture(self):
        import tempfile
        from pathlib import Path
        from archive import ArchivedFile
        logger=self.make_logger()
        logger.destination_guild_id=777
        logger.sender.send.return_value={'id':'9','channel_id':'8'}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'captured'; path.write_bytes(b'preserved image')
            future=asyncio.get_running_loop().create_future()
            future.set_result(([ArchivedFile(path,'image.png','image/png',15)],[]))
            await logger.process('MESSAGE_CREATE',{'guild_id':'1','channel_id':'3','id':'4'},future)
            self.assertFalse(path.exists())
            self.assertEqual(logger.links[('3','4')],'https://discord.com/channels/777/8/9')
            await logger.process('MESSAGE_DELETE',{'guild_id':'1','channel_id':'3','id':'4'})
            self.assertIn('https://discord.com/channels/777/8/9',str(logger.sender.send.await_args))
    async def test_known_bot_partial_update_filtered_unknown_reported(self):
        logger=self.make_logger()
        await logger.handle_event({'t':'MESSAGE_CREATE','d':{'guild_id':'1','channel_id':'3','id':'4','author':{'bot':True}}})
        await logger.handle_event({'t':'MESSAGE_UPDATE','d':{'guild_id':'1','channel_id':'3','id':'4','content':'skip'}})
        self.assertTrue(logger.queue.empty())
        await logger.handle_event({'t':'MESSAGE_UPDATE','d':{'guild_id':'1','channel_id':'3','id':'5','content':'unknown'}})
        self.assertEqual(logger.queue.qsize(),1)
    async def test_cleanup_failure_does_not_abandon_other_files(self):
        from unittest.mock import patch
        logger=self.make_logger()
        with patch('webhooklogger.cleanup_attachments',side_effect=OSError):
            w.cleanup([])
    async def test_http_200_bad_receipt_does_not_duplicate(self):
        response=Response(); response.json=AsyncMock(side_effect=ValueError)
        session=Mock(); session.post.return_value=response
        await w.Sender(session,'unused',AsyncMock()).send({})
        self.assertEqual(session.post.call_count,1)

class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_discord_gateway_dispatches_raw_receive(self):
        import discord
        logger=EventTests.make_logger(self)
        calls=[]
        websocket=discord.gateway.DiscordWebSocket.__new__(discord.gateway.DiscordWebSocket)
        websocket._dispatch=lambda event, data: calls.append((event,data))
        websocket.log_receive=websocket.debug_log_receive
        websocket.shard_id=None
        websocket._keep_alive=None
        websocket._discord_parsers={}
        websocket._dispatch_listeners=[]
        raw=json.dumps({'op':0,'s':1,'t':'MESSAGE_CREATE','d':{'id':'4','guild_id':'1','channel_id':'3','content':'gateway regression'}})
        await websocket.received_message(raw)
        self.assertEqual(calls[0][0], 'socket_raw_receive')
        await logger.on_socket_raw_receive(calls[0][1])
        self.assertEqual(logger.queue.qsize(),1)

class TransportIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_gzip_receipt_and_multipart_retry_rewinds_file(self):
        import gzip
        import tempfile
        from pathlib import Path
        import aiohttp
        from aiohttp import web
        from archive import ArchivedFile
        received=[]
        async def handler(request):
            reader=await request.multipart()
            payload=await reader.next()
            received.append(json.loads(await payload.text()))
            upload=await reader.next()
            self.assertEqual(bytes(await upload.read()),b'captured bytes')
            if len(received)==1:
                return web.json_response({'retry_after':0},status=429)
            return web.Response(body=gzip.compress(b'{"id":"9","channel_id":"8"}'),content_type='application/json',headers={'Content-Encoding':'gzip'})
        app=web.Application(); app.router.add_post('/',handler)
        runner=web.AppRunner(app); await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
        port=site._server.sockets[0].getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'test'; path.write_bytes(b'captured bytes')
                async with aiohttp.ClientSession() as session:
                    result=await w.Sender(session,f'http://127.0.0.1:{port}/',AsyncMock()).send(w.render('MESSAGE_CREATE',{'content':'@everyone'}),[ArchivedFile(path,'safe.png','image/png',14)])
                self.assertEqual(result['id'],'9')
                self.assertEqual(len(received),2)
                self.assertEqual(received[0]['allowed_mentions']['parse'],[])
        finally:
            await runner.cleanup()

class MetadataPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_content_discards_api_exception_content(self):
        logger=EventTests.make_logger(self,MESSAGE_CONTENT_INTENT='false')
        await logger.handle_event({'t':'MESSAGE_CREATE','d':{'guild_id':'1','channel_id':'3','id':'4','content':'exception content','attachments':[{'url':'https://cdn.discordapp.com/attachments/1/2/a'}],'message_snapshots':[{'message':{'content':'private'}}]}})
        event,data,future=logger.queue.get_nowait()
        self.assertNotIn('content',data)
        self.assertNotIn('message_snapshots',data)
        self.assertIsNone(future)
