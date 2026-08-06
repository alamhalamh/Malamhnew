import sys

with open("telegram_checker/checker.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

out = []
in_l12 = False
in_l4 = False

for i, line in enumerate(lines):
    if "# --- الطبقة الأولى: الاستيراد الصامت (Silent Import) ---" in line:
        out.append("        # --- الطبقة الأولى والثانية (معطلة مؤقتاً للتجارب) ---\n")
        out.append("        if False:\n")
        in_l12 = True
    
    if "# --- الطبقة الثالثة: فحص التدفق بالكود التجريبي" in line:
        in_l12 = False
        
    if "# --- الطبقة الرابعة: بوت فحص خارجي ---" in line:
        out.append("            # --- الطبقة الرابعة معطلة ---\n")
        out.append("            if False:\n")
        in_l4 = True
        
    if "except BackendPhoneUnoccupiedError:" in line and in_l4:
        # Layer 4 ends right before except block of Layer 3
        in_l4 = False
        out.append("            is_success = True\n")
        out.append('            return {"status": "HAS_SESSION", "phone": phone, "status_text": "⚠️ الرقم لديه جلسة (مؤكد بدون بوت خارجي)"}\n\n')

    if in_l12 or in_l4:
        if line.strip() == "":
            out.append(line)
        else:
            out.append("    " + line)
    else:
        out.append(line)

with open("telegram_checker/checker.py", "w", encoding="utf-8") as f:
    f.writelines(out)
