import discord
import openai
import os
import asyncio
import tempfile
from datetime import datetime, date
from aiohttp import web

# ==================== โหลด Token จาก Environment Variables ====================
# IMPORTANT: ห้ามใส่ Token ตรงนี้เด็ดขาด!
# Render จะ inject ค่าให้อัตโนมัติจาก Environment Variables
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')

# ตรวจสอบว่ามีการตั้งค่าหรือไม่
if not DISCORD_TOKEN:
    raise ValueError("❌ ไม่พบ DISCORD_TOKEN ใน Environment Variables")
if not DEEPSEEK_API_KEY:
    raise ValueError("❌ ไม่พบ DEEPSEEK_API_KEY ใน Environment Variables")

# ==================== ตั้งค่า OpenAI Client ====================
client_openai = openai.OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1"
)

# ==================== ตั้งค่า Discord Intents ====================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)

# ==================== ระบบจำกัดการใช้งาน (Cooldown ต่อ user) ====================
cooldown_dict = {}
COOLDOWN_SECONDS = 15  # รอ 15 วินาทีระหว่างแต่ละคำขอของ user คนเดียวกัน

def check_cooldown(user_id):
    current_time = datetime.now().timestamp()
    if user_id in cooldown_dict:
        last_used = cooldown_dict[user_id]
        if current_time - last_used < COOLDOWN_SECONDS:
            return False, int(COOLDOWN_SECONDS - (current_time - last_used))
    cooldown_dict[user_id] = current_time
    return True, 0

# ==================== ระบบจำกัดจำนวนครั้งรวมต่อวัน (ป้องกันบิล DeepSeek บาน) ====================
MAX_DAILY_REQUESTS = int(os.getenv('MAX_DAILY_REQUESTS', '100'))  # ปรับได้ผ่าน env var

# ==================== จำกัดให้บอทตอบได้แค่ channel เดียว ====================
# https://discord.com/channels/<guild_id>/<channel_id> -> เอาเลขท้ายสุด (channel_id) มาใส่
ALLOWED_CHANNEL_ID = int(os.getenv('ALLOWED_CHANNEL_ID', '1522145630062117016'))
_daily_usage = {"date": date.today(), "count": 0}

def check_daily_limit():
    today = date.today()
    if _daily_usage["date"] != today:
        _daily_usage["date"] = today
        _daily_usage["count"] = 0
    if _daily_usage["count"] >= MAX_DAILY_REQUESTS:
        return False
    _daily_usage["count"] += 1
    return True

# ==================== จำกัดความยาว prompt (ป้องกันคนยิง prompt ยาวเกินจำเป็น) ====================
MAX_PROMPT_LENGTH = int(os.getenv('MAX_PROMPT_LENGTH', '1000'))

# ==================== ฟังก์ชันเรียก DeepSeek ====================
async def call_deepseek(prompt):
    try:
        response = await asyncio.to_thread(
            client_openai.chat.completions.create,
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": "คุณคือผู้ช่วยที่มีประโยชน์ ตอบอย่างสุภาพและเป็นมิตร ใช้ภาษาไทยให้เหมาะสม"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=300,  # ตั้งให้ประหยัด token/ค่าใช้จ่าย
            temperature=0.7
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"❌ เกิดข้อผิดพลาด: {str(e)}"

# ==================== ฟังก์ชันช่วยส่งคำตอบ (แก้ race condition ของไฟล์) ====================
async def send_answer(send_func, answer):
    """
    send_func: async callable ที่รับ text= หรือ file= (interaction.followup.send หรือ channel.send)
    ใช้ tempfile ที่ชื่อไม่ซ้ำกัน กัน 2 คนถามพร้อมกันแล้วไฟล์ทับกัน
    """
    if len(answer) > 2000:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            ) as f:
                f.write(answer)
                tmp_path = f.name
            await send_func(
                content="📄 คำตอบยาวเกินไป ส่งเป็นไฟล์ให้ครับ:",
                file=discord.File(tmp_path, filename="answer.txt")
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
    else:
        await send_func(content=answer)

# ==================== Event: Bot พร้อมใช้งาน ====================
@bot.event
async def on_ready():
    print(f'✅ บอท {bot.user} พร้อมใช้งานแล้ว!')
    print(f'📊 กำลังเชื่อมต่อกับ {len(bot.guilds)} เซิร์ฟเวอร์')

    # ซิงค์ Slash Command
    await tree.sync()
    print("✅ Slash Commands ซิงค์เรียบร้อย")

    # ตั้งสถานะให้ Bot
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name=f"/ask | {len(bot.guilds)} Servers"
        )
    )

# ==================== Slash Command: /ask ====================
@tree.command(name="ask", description="ถาม DeepSeek AI")
async def ask(interaction: discord.Interaction, *, คำถาม: str):
    """คำสั่ง /ask <ข้อความ>"""

    # ตรวจสอบว่าอยู่ใน channel ที่อนุญาตหรือไม่
    if interaction.channel_id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(
            f"🚫 คำสั่งนี้ใช้ได้เฉพาะใน <#{ALLOWED_CHANNEL_ID}> เท่านั้นครับ",
            ephemeral=True
        )
        return

    # ตรวจสอบ Cooldown ต่อ user
    can_use, wait_time = check_cooldown(interaction.user.id)
    if not can_use:
        await interaction.response.send_message(
            f"⏳ โปรดรอ {wait_time} วินาทีก่อนใช้คำสั่งนี้อีกครั้ง",
            ephemeral=True
        )
        return

    # ตรวจสอบโควตารวมต่อวัน
    if not check_daily_limit():
        await interaction.response.send_message(
            "🚫 วันนี้มีคนใช้บอทครบโควตาแล้ว กรุณาลองใหม่พรุ่งนี้ครับ",
            ephemeral=True
        )
        return

    # ตรวจสอบความยาว prompt
    if len(คำถาม) > MAX_PROMPT_LENGTH:
        await interaction.response.send_message(
            f"⚠️ ข้อความยาวเกินไป (สูงสุด {MAX_PROMPT_LENGTH} ตัวอักษร)",
            ephemeral=True
        )
        return

    # แสดงสถานะกำลังคิด
    await interaction.response.defer()

    try:
        answer = await call_deepseek(คำถาม)
        await send_answer(interaction.followup.send, answer)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {str(e)}")

# ==================== Event: รับข้อความในแชท ====================
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # ถ้ามีการ Mention หรือพิมพ์ !ai
    is_mention = bot.user in message.mentions
    is_command = message.content.startswith("!ai")
    if not (is_mention or is_command):
        return

    # ตรวจสอบว่าอยู่ใน channel ที่อนุญาตหรือไม่
    if message.channel.id != ALLOWED_CHANNEL_ID:
        await message.channel.send(
            f"🚫 คำสั่งนี้ใช้ได้เฉพาะใน <#{ALLOWED_CHANNEL_ID}> เท่านั้นครับ"
        )
        return

    # ตรวจสอบ Cooldown ต่อ user
    can_use, wait_time = check_cooldown(message.author.id)
    if not can_use:
        await message.channel.send(f"⏳ โปรดรอ {wait_time} วินาที")
        return

    # ตรวจสอบโควตารวมต่อวัน
    if not check_daily_limit():
        await message.channel.send("🚫 วันนี้มีคนใช้บอทครบโควตาแล้ว กรุณาลองใหม่พรุ่งนี้ครับ")
        return

    # ดึงข้อความจริง (แก้บั๊ก: เดิมลบ mention ได้แค่ตัวสุดท้ายถ้ามีหลาย mention)
    if is_command:
        prompt = message.content[4:].strip()
    else:
        prompt = message.content
        for mention in message.mentions:
            prompt = prompt.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
        prompt = prompt.strip()

    if not prompt:
        await message.channel.send("❓ กรุณาพิมพ์คำถามด้วยครับ เช่น `!ai สวัสดี`")
        return

    # ตรวจสอบความยาว prompt
    if len(prompt) > MAX_PROMPT_LENGTH:
        await message.channel.send(f"⚠️ ข้อความยาวเกินไป (สูงสุด {MAX_PROMPT_LENGTH} ตัวอักษร)")
        return

    # แสดงสถานะกำลังพิมพ์
    async with message.channel.typing():
        answer = await call_deepseek(prompt)
        await send_answer(message.channel.send, answer)

# ==================== HTTP server เล็กๆ ให้ Render มองว่าเป็น Web Service ====================
# Render free tier ต้อง bind กับ $PORT ถึงจะยอมรันเป็น Web Service (ฟรี)
# ถ้าไม่มี endpoint นี้ Render จะมองว่า service ไม่ตอบสนองและ deploy ไม่ผ่าน
async def handle_ping(request):
    return web.Response(text="✅ Discord bot is running")

async def start_webserver():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Web server (health check) กำลังฟังที่ port {port}")

# ==================== รัน Bot + Web server พร้อมกัน ====================
async def main():
    await start_webserver()
    async with bot:
        await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ ไม่สามารถรันบอทได้: {e}")
