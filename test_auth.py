import asyncio
from telegram_checker.backend.tdlib_binding.core import TDLibClient
import sys

async def main():
    client = TDLibClient()
    client.start()
    
    await client.send({
        "@type": "setTdlibParameters",
        "use_test_dc": False,
        "database_directory": "/tmp/tdlib_test_db",
        "use_file_database": False,
        "use_chat_info_database": False,
        "use_message_database": False,
        "api_id": 94575,
        "api_hash": "a3406de8d171bb422bb6ddf3bbd800e2",
        "system_language_code": "en",
        "device_model": "SM-S918B",
        "system_version": "SDK 34",
        "application_version": "10.14.5",
        "enable_storage_optimizer": True
    })
    
    await client.send({
        "@type": "checkDatabaseEncryptionKey",
        "encryption_key": ""
    })
    
    # Check immediately
    res1 = await client.send({"@type": "getAuthorizationState"})
    print("Immediate state:", res1.get("@type"))
    
    await asyncio.sleep(0.5)
    
    res2 = await client.send({"@type": "getAuthorizationState"})
    print("State after 0.5s:", res2.get("@type"))
    
    client.stop()

asyncio.run(main())
