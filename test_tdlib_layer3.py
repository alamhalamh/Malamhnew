import asyncio
import uuid
import os
import shutil
import sys
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from telegram_checker.backend.tdlib_binding.core import TDLibClient

async def main():
    phone = "+66929313754"
    session_dir = f"temp_tdlib_session_{uuid.uuid4().hex[:8]}"
    client = TDLibClient()
    
    updates = []
    def on_update(data):
        updates.append(data)
        if data.get("@type") == "updateAuthorizationState":
            print(">>> UPDATE STATE:", data)
            
    client.start(update_handler=on_update)
    
    try:
        await client.send({
            "@type": "setTdlibParameters",
            "use_test_dc": False,
            "database_directory": session_dir,
            "use_file_database": False, 
            "use_chat_info_database": False,
            "use_message_database": False,
            "api_id": 32507194,
            "api_hash": "885aed505372434518892a7e0f7fccc1",
            "system_language_code": "en",
            "device_model": "Test",
            "application_version": "1.0",
            "enable_storage_optimizer": True
        })
        
        await client.send({
            "@type": "checkDatabaseEncryptionKey",
            "encryption_key": ""
        })
        
        await asyncio.sleep(2)
        print("Sending setAuthenticationPhoneNumber...")
        
        res = await client.send({
            "@type": "setAuthenticationPhoneNumber",
            "phone_number": phone,
            "settings": {
                "@type": "phoneNumberAuthenticationSettings",
                "allow_flash_call": False,
                "is_current_phone_number": False,
                "allow_sms_retriever_api": False
            }
        })
        
        print("API RESPONSE:", res)
        await asyncio.sleep(2)
        
    finally:
        client.stop()
        if os.path.exists(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)

if __name__ == "__main__":
    asyncio.run(main())
