import asyncio
import os
import logging
import time
import traceback
import datetime

from telegram_checker.backend.errors import (
    BackendFloodWaitError, BackendPrivacyError, BackendPhoneBannedError,
    BackendSessionPasswordNeededError, BackendPhoneInvalidError,
    BackendPhoneUnoccupiedError, BackendPhoneMigrateError, BackendCodeInvalidError,
    BackendSessionUnauthorizedError, BackendError
)

from .telegram_client import telegram_client_manager, SessionUnauthorizedError
from .account_manager import account_manager
from .flood_manager import flood_manager
from proxy_infrastructure import proxy_manager

logger = logging.getLogger(__name__)

# =====================================================================
# Strategy Pattern: base abstract class and individual strategies
# =====================================================================

class SmartCheckStrategy:
    def __init__(self):
        self._shadowbanned_accounts = set()

    async def _check_via_external_bot(self, backend, phone, bot_username):
        """فحص الرقم عبر بوت فحص خارجي عبر رسائل تيليجرام المباشرة"""
        try:
            before_send = int(time.time())
            await backend.send_message(bot_username, phone)
            logger.info(f"[ExternalBot] Sent {phone} to {bot_username}, waiting for response...")

            for _ in range(45):  # انتظار حتى 45 ثانية
                await asyncio.sleep(1)
                messages = await backend.get_messages(bot_username, limit=3)
                for msg in messages:
                    if msg.get("out"):
                        continue
                    if msg.get("date", 0) >= before_send and '📊' in (msg.get("text") or ''):
                        reply = msg.get("text")
                        if '🔐' in reply:
                            logger.info(f"[ExternalBot] ✅ Result: REGISTERED (Phone: {phone})")
                            return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}
                        elif '✅' in reply:
                            logger.info(f"[ExternalBot] ✅ Result: NOT REGISTERED (Phone: {phone})")
                            return {"status": "NO_SESSION", "phone": phone, "status_text": "🆕 غير مسجل"}
                        elif '❌' in reply:
                            logger.info(f"[ExternalBot] ✅ Result: BANNED (Phone: {phone})")
                            return {"status": "BANNED", "phone": phone, "status_text": "📵 مـحـظـور"}
                        elif '🔴' in reply:
                            logger.warning(f"[ExternalBot] Bot returned ERROR for {phone}")
                            return None

            logger.warning(f"[ExternalBot] Timeout waiting for response (Phone: {phone})")
            return None
        except Exception as e:
            logger.error(f"[ExternalBot] Failed to communicate with bot {bot_username}: {type(e).__name__} - {e}")
            return None

    async def check_l1_l2(self, backend, phone, account):
        import database as db
        layer_results = {}

        # --- الطبقة الأولى: الاستيراد الصامت (Silent Import) ---
        logger.info(f"[Layer 1: Import] Silent Contact Import check for {phone}")
        try:
            import_res = await asyncio.wait_for(
                backend.import_contacts([phone]),
                timeout=8.0
            )
        
            # تنظيف قائمة جهات الاتصال فوراً
            users = import_res.get("users", [])
            imported = import_res.get("imported", [])
        
            if users:
                user_id = users[0].get("id")
                await backend.delete_contacts([user_id])
                logger.info(f"[Layer 1] User found directly! Registered. (Phone: {phone})")
                layer_results["layer1"] = "HAS_SESSION"
                return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}
        
            elif imported:
                imported_user_id = imported[0].get("user_id")
                await backend.delete_contacts([imported_user_id])
                logger.info(f"[Layer 1] Contact imported! Registered. (Phone: {phone})")
                layer_results["layer1"] = "HAS_SESSION"
                return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}

        except BackendPhoneMigrateError as e:
            # معالجة فورية لانتقال مركز البيانات
            logger.info(f"[Layer 1] Phone migrate detected to DC {e.new_dc}. Re-routing...")
            try:
                await telegram_client_manager.disconnect_client(account["id"])
                backend2 = await telegram_client_manager.get_client(account)
                await backend2.switch_dc(e.new_dc)
                await asyncio.sleep(0.5)
                return await self.check_l1_l2(backend2, phone, account)
            except Exception as migrate_error:
                logger.error(f"Migration error: {migrate_error}")
                return {"status": "ERROR", "phone": phone, "status_text": f"❌ فشل الانتقال لـ DC {e.new_dc}"}

        except BackendFloodWaitError as e:
            await flood_manager.set_flood(account["id"], e.seconds)
            return {
                "status": "FLOOD_WAIT",
                "seconds": e.seconds,
                "phone": phone,
                "status_text": f"🚫 حظر مؤقت {e.seconds} ثانية"
            }
        except BackendSessionUnauthorizedError:
            await account_manager.disable_account(account["id"])
            return {"status": "ACCOUNT_DISABLED", "phone": phone, "status_text": "❌ حساب الفاحص تالف وتم تعطيله"}
        except BackendError as e:
            logger.warning(f"[Layer 1] Backend error: {e}")
        except Exception as e:
            logger.warning(f"[Layer 1] Silent Phase generic error: {e}")

        # --- الطبقة الثانية: فحص الخصوصية والتحقق المباشر (ResolvePhone) ---
        logger.info(f"[Layer 2: ResolvePhone] Running ResolvePhoneRequest for {phone}")
        try:
            resolved = await backend.resolve_phone(phone)
            users = resolved.get("users", [])
            if users:
                logger.info(f"[Layer 2] User resolved successfully! Registered. (Phone: {phone})")
                layer_results["layer2"] = "HAS_SESSION"
                return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}
            else:
                logger.info(f"[Layer 2] ResolvePhone returned empty user. Moving to Layer 3...")

        except BackendPrivacyError:
            # مستخدم مسجل ولكن قام بتشديد إعدادات الخصوصية (دليل قاطع على وجود الحساب!)
            logger.info(f"[Layer 2] Privacy Restricted! Phone is Registered but hidden. (Phone: {phone})")
            layer_results["layer2"] = "HAS_SESSION"
            return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}

        except BackendPhoneUnoccupiedError:
            logger.info(f"[Layer 2] Phone unoccupied. Not registered. (Phone: {phone})")
            layer_results["layer2"] = "NO_SESSION"
            # السماح بالمرور لاختبار الفخ

        except BackendPhoneBannedError:
            logger.info(f"[Layer 2] Phone is banned. (Phone: {phone})")
            return {
                "status": "BANNED",
                "phone": phone,
                "status_text": "📵 مـحـظـور"
            }

        except BackendPhoneInvalidError:
            logger.info(f"[Layer 2] Phone invalid. (Phone: {phone})")
            layer_results["layer2"] = "NO_SESSION"

        except BackendFloodWaitError as e:
            await flood_manager.set_flood(account["id"], e.seconds)
            return {
                "status": "FLOOD_WAIT",
                "seconds": e.seconds,
                "phone": phone,
                "status_text": f"🚫 حظر مؤقت {e.seconds} ثانية"
            }
        except BackendSessionUnauthorizedError:
            await account_manager.disable_account(account["id"])
            return {"status": "ACCOUNT_DISABLED", "phone": phone, "status_text": "❌ حساب الفاحص تالف وتم تعطيله"}

        except Exception as e:
            logger.warning(f"[Layer 2] ResolvePhone error: {e}")

        # --- اختبار الفخ (Honeypot Test) لتأكيد حظر الظل ---
        honeypot_number = await db.get_setting("honeypot_number")
        if honeypot_number and layer_results.get("layer1") == "NO_SESSION" and layer_results.get("layer2") == "NO_SESSION":
            if phone != honeypot_number:
                if not hasattr(self, "_honeypot_cache"):
                    self._honeypot_cache = {}
            
                last_verified = self._honeypot_cache.get(account["id"], 0)
                if time.monotonic() - last_verified > 300: # 5 minutes
                    logger.info(f"[Honeypot] Testing account {account['id']} with honeypot {honeypot_number}...")
                    found_hp = False
                    try:
                        hp_res = await asyncio.wait_for(
                            backend.import_contacts([honeypot_number]),
                            timeout=6.0
                        )
                        hp_users = hp_res.get('users', [])
                        hp_imported = hp_res.get('imported', [])
                    
                        if hp_users:
                            found_hp = True
                            await backend.delete_contacts([hp_users[0].get('id')])
                        elif hp_imported:
                            found_hp = True
                            await backend.delete_contacts([hp_imported[0].get('user_id')])
                    
                        if not found_hp:
                            try:
                                hp_res2 = await backend.resolve_phone(honeypot_number)
                                if hp_res2.get('users'):
                                    found_hp = True
                            except BackendPrivacyError:
                                found_hp = True
                            except Exception:
                                pass
                            
                    except Exception as hp_err:
                        logger.warning(f"[Honeypot] Check error: {hp_err}")
                    
                    if not found_hp:
                        # الحساب فشل في اختبار الفخ، مما يعني أنه معطوب (Shadowbanned)
                        logger.error(f"[Honeypot] 🚨 Account {account['id']} FAILED the honeypot test! Marking as shadowbanned.")
                        return {"status": "SHADOWBANNED_RETRY", "phone": phone, "status_text": "🔄 إعادة الفحص (حساب معطوب)"}
                    else:
                        logger.info(f"[Honeypot] ✅ Account {account['id']} passed the honeypot test.")
                        self._honeypot_cache[account["id"]] = time.monotonic()

        return {"status": "NEEDS_L3", "phone": phone, "status_text": "🔍 يحتاج للطبقة 3"}

    async def check_l3(self, backend, phone, account):
        import database as db
        # --- الطبقة الثالثة: فحص التدفق بالكود التجريبي (send_code_request) ---
        logger.info(f"[Layer 3: SendCode] Running direct send_code_request for {phone}")
        is_success = False
        is_flood = False
        try:
            if not backend.is_connected():
                await backend.connect()

            result = await backend.check_layer3_send_code(
                phone=phone,
                api_id=int(account["api_id"]),
                api_hash=account["api_hash"]
            )
            
            # إلغاء الكود فوراً
            try:
                await backend.cancel_code(phone, result.get("phone_code_hash", ""))
            except Exception:
                pass
            
            logger.info(f"[Layer 3] Direct connection returned code. Deferring to Layer 4 (External Bot).")
            
            # --- الطبقة الرابعة: بوت فحص خارجي ---
            checker_bot = await db.get_setting("checker_bot_username")
            if checker_bot:
                logger.info(f"[Layer 4: ExternalBot] Checking {phone} via @{checker_bot}...")
            
                manager_account_id = await db.get_setting("external_checker_account_id")
                ext_backend = backend
            
                if manager_account_id and str(manager_account_id).isdigit():
                    manager_account_id = int(manager_account_id)
                    if account["id"] != manager_account_id:
                        accounts = await account_manager.get_all_accounts()
                        manager_account = next((acc for acc in accounts if acc["id"] == manager_account_id), None)
                        if manager_account and manager_account.get("is_active"):
                            try:
                                ext_backend = await telegram_client_manager.get_client(manager_account)
                                if not ext_backend.is_connected():
                                    await ext_backend.connect()
                                logger.info(f"[Layer 4: ExternalBot] Handed off to Manager Account ID: {manager_account_id}")
                            except Exception as e:
                                logger.error(f"[Layer 4: ExternalBot] Failed to get manager client: {e}. Falling back to current worker.")
                        else:
                            logger.warning(f"[Layer 4: ExternalBot] Manager account {manager_account_id} not found or inactive. Falling back to current worker.")
            
                ext_result = await self._check_via_external_bot(ext_backend, phone, checker_bot)
                if ext_result is not None:
                    return ext_result
                logger.warning(f"[Layer 4: ExternalBot] Failed to get clear response.")
                return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة (فشل البوت الخارجي)"}
            else:
                logger.warning(f"[Layer 4: ExternalBot] Not configured. Cannot determine accuracy.")
                return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة (بدون بوت خارجي)"}

        except BackendPhoneUnoccupiedError:
            logger.info(f"[Layer 3] Unoccupied error. Phone is Not Registered. (Phone: {phone})")
            return {"status": "NO_SESSION", "phone": phone, "status_text": "🆕 غير مسجل"}

        except BackendPhoneBannedError:
            logger.info(f"[Layer 3] Banned error. Phone is Banned. (Phone: {phone})")
            return {"status": "BANNED", "phone": phone, "status_text": "📵 مـحـظـور"}

        except BackendPhoneInvalidError:
            logger.info(f"[Layer 3] Invalid phone. (Phone: {phone})")
            return {"status": "UNKNOWN", "phone": phone, "status_text": "🔴 حالة غير معروفة"}

        except BackendFloodWaitError as e:
            await flood_manager.set_flood(account["id"], e.seconds)
            logger.warning(f"[Layer 3] FloodWait: {e.seconds} seconds on checker.")
            return {"status": "FLOOD_WAIT", "seconds": e.seconds, "phone": phone, "status_text": f"🚫 حظر مؤقت {e.seconds} ثانية"}

        except BackendSessionPasswordNeededError:
            logger.info(f"[Layer 3] Session password needed! Phone is Registered. (Phone: {phone})")
            return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة"}

        except BackendPhoneMigrateError as e:
            logger.info(f"[Layer 3] PhoneMigrateError to DC {e.new_dc}. Re-routing...")
            try:
                await telegram_client_manager.disconnect_client(account["id"])
                backend2 = await telegram_client_manager.get_client(account)
                await backend2.switch_dc(e.new_dc)
                await asyncio.sleep(0.5)
                return await self.check_l3(backend2, phone, account)
            except Exception as migrate_err:
                return {"status": "ERROR", "phone": phone, "status_text": f"❌ فشل الانتقال لـ DC {e.new_dc}"}

        except BackendSessionUnauthorizedError:
            await account_manager.disable_account(account["id"])
            return {"status": "ACCOUNT_DISABLED", "phone": phone, "status_text": "❌ حساب الفاحص تالف"}

        except Exception as e:
            logger.error(f"[Layer 3] Unexpected exception for {phone}: {e}")
            return {"status": "ERROR", "phone": phone, "status_text": f"⚙️ خطأ نظام: {e}"}


# =====================================================================
# Main Engine: TelegramCheckEngine
# =====================================================================

class TelegramCheckEngine:
    def __init__(self):
        self.strategy = SmartCheckStrategy()

    def is_shadowbanned(self, account_id):
        return account_id in self.strategy._shadowbanned_accounts

    def mark_shadowbanned(self, account_id):
        self.strategy._shadowbanned_accounts.add(account_id)

    async def _setup_backend(self, account, phone):
        try:
            backend = await telegram_client_manager.get_client(account)
            return backend
        except SessionUnauthorizedError:
            await account_manager.disable_account(account["id"])
            return {"status": "ACCOUNT_DISABLED", "phone": phone, "status_text": "❌ حساب الفاحص تالف وتم تعطيله"}
        except Exception as e:
            logger.error(f"Connection Failed: {e}")
            return {"status": "ERROR", "phone": phone, "status_text": f"❌ فشل الاتصال بالفاحص: {e}"}

    async def check_phone_l1_l2(self, account, phone):
        backend = await self._setup_backend(account, phone)
        if isinstance(backend, dict): return backend
        return await self.strategy.check_l1_l2(backend, phone, account)

    async def check_phone_l3(self, account, phone):
        backend = await self._setup_backend(account, phone)
        if isinstance(backend, dict): return backend
        return await self.strategy.check_l3(backend, phone, account)

# =====================================================================
# Legacy Adapter for Backward Compatibility
# =====================================================================

class TelegramChecker:
    def __init__(self):
        self.engine = TelegramCheckEngine()

    def is_shadowbanned(self, account_id):
        return self.engine.is_shadowbanned(account_id)

    def mark_shadowbanned(self, account_id):
        self.engine.mark_shadowbanned(account_id)

    async def _auto_recovery_loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                disabled_accounts = await account_manager.get_all_disabled_accounts()
                if disabled_accounts:
                    logger.info(f"[Auto-Recovery] Found {len(disabled_accounts)} disabled accounts. Testing recovery...")
                    for acc in disabled_accounts:
                        try:
                            backend = await telegram_client_manager.get_client(acc)
                            if await backend.is_user_authorized():
                                logger.info(f"[Auto-Recovery] Account {acc['phone']} is authorized! Recovering...")
                                await account_manager.enable_account(acc["id"])
                        except SessionUnauthorizedError:
                            pass
                        except Exception as e:
                            logger.warning(f"[Auto-Recovery] Failed recovery check for {acc['phone']}: {e}")
            except Exception as e:
                logger.error(f"[Auto-Recovery] Error in recovery loop: {e}")
            
            await asyncio.sleep(300)

    async def get_available_account(self):
        if not hasattr(self, "_recovery_task_started"):
            self._recovery_task_started = True
            asyncio.create_task(self._auto_recovery_loop())
        return await account_manager.get_available_account()

    async def check_phone_l1_l2(self, account, phone):
        return await self.engine.check_phone_l1_l2(account, phone)

    async def check_phone_l3(self, account, phone):
        return await self.engine.check_phone_l3(account, phone)

    async def check_phone(self, account, phone):
        # Fallback for old code if it still calls check_phone directly
        res = await self.check_phone_l1_l2(account, phone)
        if res.get("status") == "NEEDS_L3":
            res = await self.check_phone_l3(account, phone)
        return res

    async def check_numbers(self, phones, callback=None):
        results = []
        for phone in phones:
            account = await self.wait_for_account()
            result = await self.check_phone(account, phone)
            
            if result["status"] in ["FLOOD_WAIT", "ACCOUNT_DISABLED", "SHADOWBANNED_RETRY"]:
                account_retry = await self.wait_for_account()
                result = await self.check_phone(account_retry, phone)
                
            results.append(result)
            if callback:
                await callback(result)
        return results


class BatchChecker:
    def __init__(self, checker):
        self.checker = checker

    async def worker(self, account, queue, callback=None, active_workers=None):
        import database as db
        try:
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                    
                phone = item["phone"]
                layer = item["layer"]
                
                is_sb = self.checker.is_shadowbanned(account["id"])
                
                if is_sb and layer == 1:
                    # Account is shadowbanned, cannot do Layer 1
                    # Put the item back into the queue so a healthy worker can grab it
                    await queue.put(item)
                    queue.task_done()
                    # Sleep slightly to avoid a tight busy loop if only shadowbanned accounts are free
                    await asyncio.sleep(1.5)
                    continue
                
                t_start = time.perf_counter()
                
                # Check cache first for Layer 1 calls to avoid redundant checks
                if layer == 1:
                    cached = await db.get_cached_number(phone)
                    if cached:
                        if callback: await callback(cached)
                        queue.task_done()
                        continue
                
                logger.info(f"Starting check for {phone} using checker {account.get('id')} (Layer {layer})")

                if layer == 1:
                    result = await self.checker.check_phone_l1_l2(account, phone)
                    
                    if result["status"] == "NEEDS_L3":
                        # Needs Layer 3 check, put back in queue with layer=3
                        await queue.put({"phone": phone, "layer": 3})
                    elif result["status"] == "SHADOWBANNED_RETRY":
                        # Current account is busted, mark it and retry phone in layer 1
                        self.checker.mark_shadowbanned(account["id"])
                        await queue.put({"phone": phone, "layer": 1})
                    elif result["status"] in ["FLOOD_WAIT", "FLOOD"]:
                        seconds = result.get("seconds", 60)
                        await flood_manager.set_flood(account["id"], seconds)
                        await queue.put(item)
                        queue.task_done()
                        break
                    elif result["status"] in ["ACCOUNT_DISABLED", "CHECKER_BANNED"]:
                        await queue.put(item)
                        queue.task_done()
                        break
                    else:
                        # Final result
                        if result.get("status") in ["HAS_SESSION", "NO_SESSION", "BANNED"]:
                            await db.save_cached_number(result.get("phone", phone), result["status"], result.get("status_text", ""))
                        if callback: await callback(result)

                elif layer == 3:
                    result = await self.checker.check_phone_l3(account, phone)
                    
                    if result["status"] in ["FLOOD_WAIT", "FLOOD"]:
                        seconds = result.get("seconds", 60)
                        await flood_manager.set_flood(account["id"], seconds)
                        await queue.put(item)
                        queue.task_done()
                        break
                    elif result["status"] in ["ACCOUNT_DISABLED", "CHECKER_BANNED"]:
                        await queue.put(item)
                        queue.task_done()
                        break
                    else:
                        if result.get("status") in ["HAS_SESSION", "NO_SESSION", "BANNED"]:
                            await db.save_cached_number(result.get("phone", phone), result["status"], result.get("status_text", ""))
                        if callback: await callback(result)
                        
                # Increment usage count safely
                if result and result.get("status") not in ["FLOOD_WAIT", "ACCOUNT_DISABLED", "ERROR", "NEEDS_L3", "SHADOWBANNED_RETRY"]:
                    try:
                        await flood_manager.account_used(account["id"])
                    except Exception as ue:
                        pass
                        
                t_end = time.perf_counter()
                logger.info(f"End check (Layer {layer}) for {phone}. Execution time: {t_end - t_start:.4f}s")
                queue.task_done()

        finally:
            if active_workers is not None:
                async with active_workers["lock"]:
                    active_workers["count"] -= 1
                    is_last = active_workers["count"] <= 0

                if is_last:
                    drained = 0
                    while not queue.empty():
                        try:
                            queue.get_nowait()
                            queue.task_done()
                            drained += 1
                        except asyncio.QueueEmpty:
                            break

                    if drained:
                        logger.warning(
                            f"BatchChecker: تم تفريغ {drained} مهمة من الطابور بدون إكمالها."
                        )

    async def run(self, phones, callback=None):
        queue = asyncio.Queue()

        for phone in phones:
            await queue.put({"phone": phone, "layer": 1})

        accounts = await account_manager.get_all_accounts()

        workers = []
        active_workers = {"count": 0, "lock": asyncio.Lock()}

        for account in accounts:
            if await flood_manager.is_flooded(account["id"]):
                continue

            active_workers["count"] += 1

            task = asyncio.create_task(
                self.worker(account, queue, callback, active_workers)
            )
            workers.append(task)

        if not workers:
            logger.warning("BatchChecker: لا توجد حسابات فاحصة متاحة.")
            return False

        await queue.join()

        for _ in workers:
            await queue.put(None)

        await asyncio.gather(*workers, return_exceptions=True)

        return True


telegram_checker = TelegramChecker()
batch_checker = BatchChecker(telegram_checker)
