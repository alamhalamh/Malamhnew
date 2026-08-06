import asyncio
import asyncpg
import os

async def main():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL"))
    await conn.execute("CREATE TABLE IF NOT EXISTS test_t (id serial PRIMARY KEY)")
    try:
        await conn.fetch("INSERT INTO test_t (id) VALUES (9999) ON CONFLICT DO NOTHING")
    except Exception as e:
        print(f"EXCEPTION_TYPE: {type(e).__name__}")
        print(f"EXCEPTION_MODULE: {type(e).__module__}")
    await conn.close()

asyncio.run(main())
