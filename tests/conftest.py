import aiohttp
import pytest_asyncio


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client_session:
        yield client_session
