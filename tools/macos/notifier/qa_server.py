"""Synthetic localhost-only QA server; never loads production config or Telegram."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from aiohttp import web

from app.config import Settings
from app.db.session import init_db
from app.inbox import web as inbox_web
from app.inbox.service import InboxError, InboxService, conversation_json


class FakeClient:
    def is_connected(self):
        return True


async def main():
    with tempfile.TemporaryDirectory(prefix='notifier-qa-') as directory:
        root = Path(directory)
        settings = Settings(_env_file=None, database_url=f"sqlite:///{root / 'qa.db'}")
        service = InboxService(FakeClient(), init_db(settings), lambda: set(), root / 'cache', 1)
        static = root / 'static'
        static.mkdir()
        for file in inbox_web.STATIC.iterdir():
            (static / file.name).write_text(file.read_text().replace('127.0.0.1:8788',
                                                                    '127.0.0.1:8878'))
        inbox_web.STATIC = static

        async def history(key):
            return {'conversation': conversation_json(service.require(key)), 'messages': []}

        async def send(*args, **kwargs):
            raise InboxError('Synthetic QA: sending is disabled.', 422)

        async def emit(request):
            body = await request.json()
            row = service.store.activate(peer_id='-100123', thread_id=0, is_forum=False,
                title='Synthetic QA', trigger_id=int(body['id']),
                preview='Synthetic notifier check',
                reason='mention_only')
            return web.json_response({'conversation_id': row.id})

        @web.middleware
        async def qa_csp(request, handler):
            response = await handler(request)
            if 'Content-Security-Policy' in response.headers:
                response.headers['Content-Security-Policy'] = response.headers[
                    'Content-Security-Policy'].replace('127.0.0.1:8788', '127.0.0.1:8878')
            return response

        service.history = history
        service.send = send
        app = inbox_web.create_app(service, port=8877)
        app.middlewares.insert(0, qa_csp)
        app.router.add_post('/qa/event', emit)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, '127.0.0.1', 8877).start()
        print('Synthetic QA: http://127.0.0.1:8877', flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
