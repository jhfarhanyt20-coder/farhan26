"""
get_session_playwright.py
===========================
আগের browser_cookie3-based স্ক্রিপ্টটা Windows-এর নতুন Chrome/Edge cookie
এনক্রিপশনের সাথে কাজ করছিল না। এই স্ক্রিপ্টটা সেই সমস্যা পুরোপুরি এড়িয়ে যায়:

এটা নিজে একটা আসল, visible Chromium ব্রাউজার উইন্ডো খুলবে (Playwright দিয়ে,
আপনার bot-এ যেটা আগে থেকেই ইনস্টল করা আছে)। আপনি সেখানে normal ভাবে
Quotex-এ ম্যানুয়ালি লগইন করবেন। লগইন সফল হয়ে ট্রেডিং পেজে পৌঁছালে, স্ক্রিপ্ট
নিজে থেকেই সেই ব্রাউজার সেশনের কুকি ধরে নিয়ে token বের করে session.json-এ
লিখে দেবে।

চালানোর নিয়ম:
    python get_session_playwright.py

তারপর যে Chromium উইন্ডো খুলবে সেখানে ম্যানুয়ালি লগইন করুন — বাকিটা
স্ক্রিপ্ট নিজেই সামলে নেবে।
"""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from config import DEFAULT_EMAIL

HOST = "qxbroker.com"


async def main():
    async with async_playwright() as p:
        print("⏳ Chromium ব্রাউজার খোলা হচ্ছে...")
        
        browser = await p.chromium.launch(
            headless=False,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--window-size=1280,720',
            ]
        )
        
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        )
        
        page = await context.new_page()
        
        # Basic anti-detection
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        await page.goto(f"https://{HOST}/en/sign-in")

        print("\n" + "=" * 60)
        print("👉 এখন খোলা ব্রাউজার উইন্ডোতে ম্যানুয়ালি Quotex-এ লগইন করুন।")
        print("   লগইন সফল হয়ে ট্রেডিং পেজে (trade) পৌঁছালে")
        print("   এই স্ক্রিপ্ট automatically detect করে নেবে।")
        print("=" * 60 + "\n")

        # ট্রেডিং পেজে পৌঁছানো পর্যন্ত অপেক্ষা করা (সর্বোচ্চ ৫ মিনিট)
        try:
            await page.wait_for_url("**/trade**", timeout=300000)
        except Exception:
            print("❌ ৫ মিনিটের মধ্যে ট্রেড পেজে পৌঁছানো যায়নি। আবার চেষ্টা করুন।")
            await browser.close()
            return

        print("✅ লগইন সফল detect করা হয়েছে! সেশন তথ্য সংগ্রহ করা হচ্ছে...")

        # কুকি সংগ্রহ - সব ডোমেইন থেকে
        cookies = await context.cookies()
        print(f"📝 মোট কুকি পাওয়া গেছে: {len(cookies)}")
        
        # সব কুকি প্রিন্ট করে দেখি (ডিবাগিং এর জন্য)
        for c in cookies:
            print(f"   🍪 {c['name']}: {c['domain']}")
        
        # Quotex এর কুকি ফিল্টার
        relevant = [c for c in cookies if HOST in c["domain"] or 'quotex' in c["domain"] or 'qxbroker' in c["domain"]]
        
        if not relevant:
            # যদি কোনো কুকি না পাওয়া যায়, সব কুকি নিয়ে নিই
            print("⚠️ নির্দিষ্ট ডোমেইনের কুকি পাওয়া যায়নি, সব কুকি নিচ্ছি...")
            relevant = cookies
        
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in relevant)
        print(f"📋 কুকি স্ট্রিং তৈরি করা হয়েছে: {cookie_str[:100]}...")  # প্রথম 100 অক্ষর দেখানো

        user_agent = await page.evaluate("() => navigator.userAgent")

        # পেজের ভেতর থেকেই digest API কল করা হচ্ছে
        token = None
        try:
            resp = await page.evaluate(
                """async () => {
                    const r = await fetch('/api/v1/cabinets/digest', {
                        credentials: 'include'
                    });
                    if (!r.ok) return null;
                    const j = await r.json();
                    return j.data ? j.data.token : null;
                }"""
            )
            token = resp
            print(f"🔑 Token পাওয়া গেছে: {token[:50] if token else 'None'}...")
        except Exception as e:
            print(f"⚠️ digest API কল করতে সমস্যা: {e}")

        # যদি token না পাওয়া যায়, localStorage থেকেও চেক করি
        if not token:
            try:
                token = await page.evaluate("() => localStorage.getItem('token')")
                print(f"🔑 localStorage থেকে token: {token[:50] if token else 'None'}...")
            except:
                pass

        await browser.close()

        if not cookie_str:
            print("❌ কুকি সংগ্রহ করা যায়নি। আবার চেষ্টা করুন।")
            return

        if not token:
            print("❌ Token সংগ্রহ করা যায়নি। আবার চেষ্টা করুন।")
            return

        session_path = Path("session.json")
        all_sessions = {}
        if session_path.exists():
            try:
                all_sessions = json.loads(session_path.read_text())
            except json.JSONDecodeError:
                pass

        all_sessions[DEFAULT_EMAIL] = {
            "cookies": cookie_str,
            "token": token,
            "user_agent": user_agent,
        }
        session_path.write_text(json.dumps(all_sessions, indent=4))

        print("\n✅ session.json লেখা হয়েছে (cookies + token পাওয়া গেছে)।")
        print("   এবার চালান: python main.py")


if __name__ == "__main__":
    asyncio.run(main())