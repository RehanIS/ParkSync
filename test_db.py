import asyncio
import asyncpg
import os
import socket
from dotenv import load_dotenv

load_dotenv()

async def test():
    # Get the host from your URL or hardcode it
    host = "aws-0-ap-south-1.pooler.supabase.com"  # ← replace with YOUR exact pooler host from Supabase dashboard

    # Force IPv4 resolution
    try:
        addr_info = await asyncio.get_event_loop().getaddrinfo(
            host, 5432, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
        print("Resolved IPv4 addresses:", addr_info)
    except Exception as e:
        print("Resolution failed:", e)
        return

    # Now connect using the resolved info or just proceed (asyncpg will use getaddrinfo internally)
    conn = await asyncpg.connect(os.getenv("DATABASE_URL"))
    print("Connected successfully!")
    await conn.close()

asyncio.run(test())