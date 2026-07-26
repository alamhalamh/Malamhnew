import asyncio
import logging
from typing import Dict, Any, List, Optional
from .base import TelegramBackend
from .errors import *
from .tdlib_binding.core import TDLibClient

logger = logging.getLogger(__name__)

class TDLibBackend(TelegramBackend):
    def __init__(self, client: TDLibClient):
        self.client = client
        self._is_connected = False

    async def connect(self):
        if not self._is_connected:
            self._is_connected = True
            # دورة التحديثات تدار من الخارج أو من الـ Client نفسه

    async def disconnect(self):
        if self.client:
            self.client.stop()
        self._is_connected = False

    def is_connected(self) -> bool:
        return self._is_connected

    async def is_user_authorized(self) -> bool:
        for _ in range(30):
            res = await self.client.send({"@type": "getAuthorizationState"})
            state = res.get("@type", "")
            if state == "authorizationStateReady":
                return True
            if state in ["authorizationStateWaitPhoneNumber", "authorizationStateWaitCode", "authorizationStateWaitPassword", "authorizationStateClosed"]:
                return False
            await asyncio.sleep(0.1)
        return False

    async def get_me(self) -> Dict[str, Any]:
        res = await self.client.send({"@type": "getMe"})
        if res.get("@type") == "error":
            raise BackendError(res.get("message"))
        return {
            "id": res.get("id"),
            "first_name": res.get("first_name", ""),
            "username": res.get("usernames", {}).get("editable_username", "") if res.get("usernames") else ""
        }

    # ================= المصادقة (Auth) =================
    async def send_code_request(self, phone: str, force_sms: bool = False) -> Dict[str, Any]:
        res = await self.client.send({
            "@type": "setAuthenticationPhoneNumber",
            "phone_number": phone,
            "settings": {
                "@type": "phoneNumberAuthenticationSettings",
                "allow_flash_call": False,
                "is_current_phone_number": False,
                "allow_sms_retriever_api": False
            }
        })
        if res.get("@type") == "error":
            code = res.get("code")
            msg = res.get("message", "")
            if code == 429:
                raise BackendFloodWaitError(60) 
            raise BackendError(msg)
        return {"phone_code_hash": "tdlib_managed", "type": "tdlib"}

    async def resend_code_request(self, phone: str, phone_code_hash: str) -> Dict[str, Any]:
        res = await self.client.send({"@type": "resendAuthenticationCode"})
        if res.get("@type") == "error":
            raise BackendError(res.get("message"))
        return {"phone_code_hash": "tdlib_managed", "type": "tdlib"}

    async def sign_in_code(self, phone: str, code: str, phone_code_hash: str) -> Dict[str, Any]:
        res = await self.client.send({
            "@type": "checkAuthenticationCode",
            "code": code
        })
        if res.get("@type") == "error":
            msg = res.get("message", "")
            if "PHONE_CODE_EXPIRED" in msg:
                raise BackendCodeExpiredError()
            if "PHONE_CODE_INVALID" in msg:
                raise BackendCodeInvalidError()
            if "SESSION_PASSWORD_NEEDED" in msg:
                raise BackendSessionPasswordNeededError()
            raise BackendError(msg)
        return {"status": "SUCCESS"}

    async def sign_in_password(self, password: str) -> Dict[str, Any]:
        res = await self.client.send({
            "@type": "checkAuthenticationPassword",
            "password": password
        })
        if res.get("@type") == "error":
            raise BackendError(res.get("message"))
        return {"status": "SUCCESS"}

    async def cancel_code(self, phone: str, phone_code_hash: str):
        pass # يُدار عبر إغلاق الجلسة

    # ================= الفحص (Checking) =================
    async def import_contacts(self, phones: List[str]) -> Dict[str, Any]:
        contacts = []
        for p in phones:
            contacts.append({
                "@type": "contact",
                "phone_number": p,
                "first_name": "TempCheck",
                "last_name": "",
                "user_id": 0
            })
        
        res = await self.client.send({
            "@type": "importContacts",
            "contacts": contacts
        })
        
        if res.get("@type") == "error":
            msg = res.get("message", "")
            msg_upper = msg.upper()
            if res.get("code") == 429:
                raise BackendFloodWaitError(60)
            if "UNAUTHORIZED" in msg_upper:
                raise BackendSessionUnauthorizedError()
            raise BackendError(msg)
            
        user_ids = res.get("user_ids", [])
        return {
            "users": [{"id": uid} for uid in user_ids if uid > 0],
            "imported": [{"user_id": uid} for uid in user_ids if uid > 0]
        }

    async def delete_contacts(self, user_ids: List[int]):
        await self.client.send({
            "@type": "removeContacts",
            "user_ids": user_ids
        })

    async def resolve_phone(self, phone: str) -> Dict[str, Any]:
        res = await self.client.send({
            "@type": "searchUserByPhoneNumber",
            "phone_number": phone
        })
        if res.get("@type") == "error":
            code = res.get("code")
            msg = res.get("message", "")
            msg_upper = msg.upper()
            if code == 429:
                raise BackendFloodWaitError(60)
            if code == 404 or "NOT_FOUND" in msg_upper:
                raise BackendPhoneUnoccupiedError()
            if code == 403 or "PRIVACY" in msg_upper:
                raise BackendPrivacyError()
            if "BANNED" in msg_upper:
                raise BackendPhoneBannedError()
            if "UNAUTHORIZED" in msg_upper:
                raise BackendSessionUnauthorizedError()
            raise BackendError(msg)
            
        user_id = res.get("id")
        if user_id:
            return {"users": [{"id": user_id}]}
        return {"users": []}

    async def check_layer3_send_code(self, phone: str, api_id: int, api_hash: str) -> Dict[str, Any]:
        """
        تنفيذ الطبقة الثالثة (Layer 3) باستخدام "محرك محاكاة التطبيقات الرسمية"
        للتعامل بذكاء مع ردود تيليجرام للأرقام التي تفتقر للمتغير is_registered.
        """
        import uuid
        import os
        import shutil
        import random
        from telegram_checker.backend.tdlib_binding.core import TDLibClient
        
        # بنك هويات التطبيقات الرسمية للتمويه (Spoofing)
        PROFILES = [
            {
                "name": "Telegram Android Official",
                "api_id": 6, "api_hash": "eb06d4abfb49dc3eeb1aeb98ae0f581e",
                "device": random.choice(["Samsung Galaxy S23 Ultra", "Pixel 7 Pro", "Xiaomi 13 Pro"]), 
                "os": random.choice(["Android 13.0", "Android 14.0"]), 
                "app": "10.14.0"
            },
            {
                "name": "Telegram X",
                "api_id": 21724, "api_hash": "3e0cb5efcb524f5144b5849cb27b5fc0",
                "device": random.choice(["Samsung Galaxy S22", "OnePlus 11"]), 
                "os": "Android 13.0", 
                "app": "0.26.1.1660"
            },
            {
                "name": "Telegram iOS",
                "api_id": 9, "api_hash": "28a9b70b435ff20b3325ebef18115668",
                "device": random.choice(["iPhone 14 Pro Max", "iPhone 13"]), 
                "os": random.choice(["iOS 16.5", "iOS 17.0"]), 
                "app": "10.1.1"
            },
            {
                "name": "Worker API (Fallback)",
                "api_id": api_id, "api_hash": api_hash,
                "device": "Samsung Galaxy S21", 
                "os": "Android 12.0", 
                "app": "9.5.0"
            }
        ]
        
        # اختيار هويتين بشكل عشوائي مع ضمان أن الهوية الأخيرة تكون هى هوية الـ Worker الأساسية لتجنب الفشل الكامل
        selected_profiles = random.sample(PROFILES[:3], 1) + [PROFILES[3]]
        
        for attempt, profile in enumerate(selected_profiles, start=1):
            session_dir = f"temp_tdlib_session_{uuid.uuid4().hex[:8]}"
            client = TDLibClient()
            client.start()
            
            try:
                logger.info(f"[Layer 3 - TDLib] Attempt {attempt}: Spoofing {profile['name']} (Device: {profile['device']}) for {phone}")
                
                await client.send({
                    "@type": "setTdlibParameters",
                    "use_test_dc": False,
                    "database_directory": session_dir,
                    "use_file_database": False, 
                    "use_chat_info_database": False,
                    "use_message_database": False,
                    "api_id": profile["api_id"],
                    "api_hash": profile["api_hash"],
                    "system_language_code": random.choice(["en", "fr", "es", "de"]),
                    "device_model": profile["device"],
                    "system_version": profile["os"],
                    "application_version": profile["app"],
                    "enable_storage_optimizer": True
                })
                
                await client.send({"@type": "checkDatabaseEncryptionKey", "encryption_key": ""})
                
                if hasattr(self.client, "proxy_payload") and self.client.proxy_payload:
                    await client.send(self.client.proxy_payload)
                
                await asyncio.sleep(1.0)  # انتظار انتقال الحالة
                
                logger.info(f"[Layer 3 - TDLib] Sending setAuthenticationPhoneNumber for {phone}...")
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
                logger.info(f"[Layer 3 - TDLib] setAuthenticationPhoneNumber Raw Response: {res}")
                
                if res.get("@type") == "error":
                    msg = res.get("message", "")
                    msg_upper = msg.upper()
                    code = res.get("code")
                    if "BANNED" in msg_upper:
                        raise BackendPhoneBannedError()
                    elif "UNOCCUPIED" in msg_upper or "NOT_FOUND" in msg_upper or "INVALID" in msg_upper:
                        raise BackendPhoneUnoccupiedError()
                    elif "SESSION_PASSWORD_NEEDED" in msg_upper:
                        raise BackendSessionPasswordNeededError()
                    elif code == 429:
                        raise BackendFloodWaitError(60)
                    elif "API_ID_INVALID" in msg_upper or "APP_VERSION_OUTDATED" in msg_upper:
                        logger.warning(f"[Layer 3 - TDLib] Profile {profile['name']} rejected by Telegram. Falling back to next profile...")
                        continue
                    else:
                        raise BackendError(msg)
                
                await asyncio.sleep(1.5)
                
                logger.info(f"[Layer 3 - TDLib] Fetching getAuthorizationState for {phone}...")
                state_res = await client.send({"@type": "getAuthorizationState"})
                logger.info(f"[Layer 3 - TDLib] getAuthorizationState Raw Response: {state_res}")
                
                state_type = state_res.get("@type")
                
                if state_type == "authorizationStateWaitCode":
                    # بدلاً من الاعتماد على is_registered الوهمي، نقرأ طريقة وصول الكود
                    code_info = state_res.get("code_info", {})
                    code_type_obj = code_info.get("type", {})
                    code_type = code_type_obj.get("@type", "")
                    
                    logger.info(f"[Layer 3 - TDLib] Processed State: Code sent via {code_type} for {phone}")
                    
                    if code_type == "authenticationCodeTypeTelegramMessage":
                        logger.info(f"[Layer 3 - TDLib] Sent via TelegramMessage! Someone is logged in. Phone {phone} has SESSION.")
                        # نحاول إرسال الكود كرسالة SMS لنرى ما إذا كان سيقبل
                        logger.info(f"[Layer 3 - TDLib] Attempting to force SMS via resendAuthenticationCode for {phone}...")
                        sms_res = await client.send({"@type": "resendAuthenticationCode"})
                        logger.info(f"[Layer 3 - TDLib] resendAuthenticationCode Response: {sms_res}")
                        
                        if sms_res.get("@type") == "ok":
                            logger.info(f"[Layer 3 - TDLib] Forced SMS successfully! This means it's a ghost session or unregistered. Returning NO_SESSION.")
                            raise BackendPhoneUnoccupiedError()
                        else:
                            logger.info(f"[Layer 3 - TDLib] Could not force SMS. Returning HAS_SESSION.")
                            return {"status": "HAS_SESSION", "phone_code_hash": "tdlib_ephemeral"}
                            
                    elif code_type == "authenticationCodeTypeSms" or code_type == "authenticationCodeTypeCall":
                        logger.info(f"[Layer 3 - TDLib] Sent via SMS/Call. Safe to assume NO_SESSION for {phone}.")
                        raise BackendPhoneUnoccupiedError()
                    else:
                        is_reg = state_res.get("is_registered")
                        if is_reg == False:
                            logger.info(f"[Layer 3 - TDLib] is_registered is explicitly False. Phone {phone} is UNOCCUPIED.")
                            raise BackendPhoneUnoccupiedError()
                        else:
                            logger.warning(f"[Layer 3 - TDLib] Unknown code type: {code_type}. Returning HAS_SESSION.")
                            return {"status": "HAS_SESSION", "phone_code_hash": "tdlib_ephemeral"}
                            
                elif state_type == "authorizationStateWaitPassword":
                    logger.info(f"[Layer 3 - TDLib] State is WaitPassword. Phone {phone} has SESSION (2FA).")
                    raise BackendSessionPasswordNeededError()
                    
                logger.info(f"[Layer 3 - TDLib] Unhandled state: {state_type}. Assuming HAS_SESSION for {phone}.")
                return {"status": "HAS_SESSION", "phone_code_hash": "tdlib_ephemeral"}
                    
            finally:
                client.stop()
                if os.path.exists(session_dir):
                    shutil.rmtree(session_dir, ignore_errors=True)
                    
        raise BackendError("All spoofing profiles failed.")

    # ================= الرسائل =================
    async def send_message(self, username: str, text: str):
        search_res = await self.client.send({
            "@type": "searchPublicChat",
            "username": username.replace("@", "")
        })
        if search_res.get("@type") == "error":
            raise BackendError(search_res.get("message"))
            
        chat_id = search_res.get("id")
        res = await self.client.send({
            "@type": "sendMessage",
            "chat_id": chat_id,
            "input_message_content": {
                "@type": "inputMessageText",
                "text": {"@type": "formattedText", "text": text}
            }
        })
        if res.get("@type") == "error":
            raise BackendError(res.get("message"))

    async def get_messages(self, username: str, limit: int) -> List[Dict[str, Any]]:
        search_res = await self.client.send({
            "@type": "searchPublicChat",
            "username": username.replace("@", "")
        })
        if search_res.get("@type") == "error":
            raise BackendError(search_res.get("message"))
            
        chat_id = search_res.get("id")
        res = await self.client.send({
            "@type": "getChatHistory",
            "chat_id": chat_id,
            "from_message_id": 0,
            "offset": 0,
            "limit": limit,
            "only_local": False
        })
        
        if res.get("@type") == "error":
            raise BackendError(res.get("message"))
            
        messages = res.get("messages", [])
        formatted = []
        for msg in messages:
            content = msg.get("content", {})
            text = content.get("text", {}).get("text", "")
            formatted.append({
                "out": msg.get("is_outgoing", False),
                "date": msg.get("date", 0),
                "text": text
            })
        return formatted

    async def switch_dc(self, new_dc: int):
        pass # TDLib يدير الخوادم تلقائياً
